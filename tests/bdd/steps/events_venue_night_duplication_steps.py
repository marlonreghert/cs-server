"""Behave steps for
tests/bdd/enrichment/events-venue-night-duplication.feature.

See plans/260912_events-venue-night-duplication.md. Three independent defects
produce one symptom, and this file drives all three apart from each other:

  - Defect 2 (attribution) runs the REAL `EventExtractionService` over the
    `context.ee_*` harness `instagram_event_extraction_steps.py` already
    builds — a real `VenueRepository` over the in-memory RDS fake, a fake
    archived-post source and a programmable fake OpenAI client. That harness
    is reused as plain function calls, the same reuse pattern
    `event_ticket_info_and_attractions_steps.py` and
    `one_event_many_posts_steps.py` already establish for sibling features.
    Only a REAL extraction can prove where a freshly-extracted row lands.

  - Defects 1 and 3 (the merge bar, the recurring window) run the merge pass
    over rows seeded directly through the DAO, on the `context.dedup_*`
    harness `event_dedup_fuzzy_title_steps.py` already builds — those
    scenarios are about `merge_touched_events`, not about extraction, and
    re-running a whole extraction for each would test the wrong thing.

Both harnesses are per-scenario and independent; a scenario touches only the
one its own steps use.

The Background's "the event extraction pipeline is configured for a known
venue" and "the candidate window is 8 hours" steps are NOT redefined here —
behave's step registry is global and both texts are already bound (in
`event_ticket_info_and_attractions_steps.py` and
`event_dedup_fuzzy_title_steps.py` respectively). Reusing them is this
repo's own documented convention for a step text two features share.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_attribution_dispute import (
    ADMIN_CONFIG_DISPUTE_ACTION_KEY,
    ADMIN_CONFIG_DISPUTE_WITHHOLD_KEY,
    REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE,
)
from app.services.event_reconciliation import new_event_id
from app.services.event_venue_resolution import (
    METHOD_HANDLE_MENTION,
    METHOD_NEIGHBOURHOOD_MATCH,
)
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _ensure_context as _ensure_ee_context,
    _run_extraction,
)

RECIFE = ZoneInfo("America/Recife")
RECIFE_LAT, RECIFE_LNG = -8.05, -34.88

_POSTING_HANDLE = "beerdock_recife"
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

_ADDRESSES = {
    "Boa Viagem": "Av. Cons. Aguiar, 1000 - Boa Viagem, Recife - PE",
    "Casa Forte": "Av. Rui Barbosa, 500 - Casa Forte, Recife - PE",
    "an unrelated address": "R. do Sol, 7 - Santo Antônio, Recife - PE",
}


def _venue_id_for(context, name: str) -> str:
    return context.vnd_venues[name]


def _ensure(context) -> None:
    _ensure_ee_context(context)
    if not hasattr(context, "vnd_venues"):
        context.vnd_venues = {}
        context.vnd_seq = 0
        context.vnd_metric_before = {}
        context.vnd_backfill_report = None


def _link_metric(method: str, result: str) -> float:
    return REGISTRY.get_sample_value(
        "event_venue_link_total", {"method": method, "result": result},
    ) or 0.0


# ── Background: the catalog and the handle ────────────────────────────────
@given('the venue catalog carries "{name}" at an address in {area}')
def step_given_catalog_carries_venue_at(context, name, area):
    _ensure(context)
    context.vnd_seq += 1
    venue_id = f"vnd_venue_{context.vnd_seq}"
    context.ee_dao.upsert_venue(Venue(
        venue_id=venue_id, venue_name=name,
        venue_address=_ADDRESSES.get(area, f"{area}, Recife - PE"),
        venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))
    context.vnd_venues[name] = venue_id


@given('the venue catalog carries "{name}" at {area}')
def step_given_catalog_carries_venue_at_bare(context, name, area):
    step_given_catalog_carries_venue_at(context, name, area)


@given('the Instagram handle "{handle}" maps only to "{name}"')
def step_given_handle_maps_only_to(context, handle, name):
    _ensure(context)
    venue_id = _venue_id_for(context, name)
    context.ee_dao.set_venue_instagram(
        VenueInstagram(venue_id=venue_id, instagram_handle=handle, status="found")
    )
    context.ee_venue_id = venue_id
    context.ee_handle = handle
    # `mode: venue_ids` rather than the event-candidate tier walk: this
    # feature is about WHERE a post's events land, never about which venues
    # qualify for extraction in the first place.
    context.ee_run_config = {
        "eligibility": {"mode": "venue_ids", "venue_ids": venue_id},
    }


@given('"{name}" is reachable by the handle "{handle}"')
def step_given_venue_reachable_by_handle(context, name, handle):
    _ensure(context)
    context.ee_dao.set_venue_instagram(
        VenueInstagram(venue_id=_venue_id_for(context, name), instagram_handle=handle, status="found")
    )


# ── Defect 2: config ──────────────────────────────────────────────────────
@given('the attribution dispute action is "{action}"')
def step_given_dispute_action(context, action):
    _ensure(context)
    _dispute_redis(context).set(ADMIN_CONFIG_DISPUTE_ACTION_KEY, json.dumps(action))


@given("the attribution dispute withholding is enabled")
def step_given_dispute_withholding_enabled(context):
    _ensure(context)
    _dispute_redis(context).set(ADMIN_CONFIG_DISPUTE_WITHHOLD_KEY, json.dumps(True))


def _dispute_redis(context):
    """The extraction service reads its admin config through the Redis client
    it was constructed with. `instagram_event_extraction_steps._reset_context`
    builds the service without one (every scenario there predates admin
    config), so one is attached here on first use — the SAME `fakeredis`
    stand-in every other admin-config scenario in this repo uses."""
    import fakeredis

    if getattr(context.ee_service, "redis_client", None) is None:
        context.ee_service.redis_client = fakeredis.FakeRedis(decode_responses=True)
    return context.ee_service.redis_client


# ── Defect 2: the post ────────────────────────────────────────────────────
def _extract_post(context, *, location_text=None, caption=None, shortcode=None, title="SAMBINHA"):
    from tests.bdd.steps.instagram_event_extraction_steps import _extraction_json

    context.vnd_seq += 1
    shortcode = shortcode or f"vnd_post_{context.vnd_seq}"
    _add_post(
        context, shortcode,
        caption=caption or "Ingressos abertos! Vem pro role.",
        timestamp=_NOW - timedelta(days=1),
    )
    context.ee_openai.program(_extraction_json(
        title=title, date_text="12/09", time_text="21h",
        location_text=location_text, confidence=0.9,
    ))
    context.ee_now = _NOW
    _dispute_redis(context)  # make sure the service reads real config, not None
    _run_extraction(context)
    context.vnd_event = context.ee_dao.get_event_by_source(context.ee_handle, shortcode)
    assert context.vnd_event is not None, f"no event stored for {shortcode}"
    return context.vnd_event


@when('a post from "{handle}" announces an event whose location text is "{location_text}"')
def step_when_post_announces_with_location_text(context, handle, location_text):
    context.vnd_metric_before = {
        (m, "disputed"): _link_metric(m, "disputed")
        for m in (METHOD_HANDLE_MENTION, METHOD_NEIGHBOURHOOD_MATCH)
    }
    _extract_post(context, location_text=location_text)


# Registered BEFORE the plain "no location text" variant below: behave's
# default parser makes `{handle}` greedy, so the shorter pattern would
# otherwise also match THIS text and the registration is refused as
# ambiguous. Most specific first.
@when('a post from "{handle}" whose caption mentions "{mention}" announces an event with no location text')
def step_when_post_caption_mentions(context, handle, mention):
    _extract_post(context, location_text=None, caption=f"Hoje tem festa com {mention}!")


@when('a post from "{handle}" announces an event with no location text')
def step_when_post_announces_without_location_text(context, handle):
    context.vnd_metric_before = {
        (m, "disputed"): _link_metric(m, "disputed")
        for m in (METHOD_HANDLE_MENTION, METHOD_NEIGHBOURHOOD_MATCH)
    }
    _extract_post(context, location_text=None)


# ── Defect 2: the operator's own correction ───────────────────────────────
@given('an operator has corrected the venue of a stored event to "{name}"')
def step_given_operator_corrected_venue(context, name):
    _ensure(context)
    context.vnd_seq += 1
    context.vnd_operator_shortcode = f"vnd_operator_{context.vnd_seq}"
    event = _extract_post(
        context, location_text=None, shortcode=context.vnd_operator_shortcode,
    )
    # Exactly what POST /admin/events/{id}/link writes, plus the
    # operator_edited_fields a PATCH records — either alone must stop an
    # automatic path from moving this row.
    context.ee_dao.update_event(event["event_id"], {
        "venue_id": _venue_id_for(context, name), "location_resolution": "manual",
        "linked_by": "manual", "linked_at": _NOW,
        "operator_edited_fields": ["venue_id"],
    })


@when('that post is re-extracted with the location text "{location_text}"')
def step_when_that_post_is_re_extracted(context, location_text):
    _extract_post(
        context, location_text=location_text, shortcode=context.vnd_operator_shortcode,
    )


# ── Defect 2: assertions ──────────────────────────────────────────────────
def _reload(context) -> dict:
    return context.ee_dao.get_event(context.vnd_event["event_id"])


@then('the event stays attributed to "{name}"')
def step_then_event_stays_attributed(context, name):
    row = _reload(context)
    assert row["venue_id"] == _venue_id_for(context, name), (row["venue_id"], row["venue_name"])


@then('the event is attributed to "{name}"')
def step_then_event_is_attributed(context, name):
    step_then_event_stays_attributed(context, name)


@then('the event carries the review reason "{reason}"')
def step_then_event_carries_review_reason(context, reason):
    row = _reload(context)
    reasons = (row.get("review_reason") or "").split("; ")
    assert reason in reasons, row.get("review_reason")


@then("the event carries no review reason")
def step_then_event_carries_no_review_reason(context):
    row = _reload(context)
    assert not row.get("review_reason"), row.get("review_reason")


@then("the event carries no dispute review reason")
def step_then_event_carries_no_dispute_reason(context):
    row = _reload(context)
    reasons = (row.get("review_reason") or "").split("; ")
    assert REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE not in reasons, row.get("review_reason")


@then('the event offers "{name}" as a ranked venue candidate')
def step_then_event_offers_candidate(context, name):
    candidates = context.ee_dao.list_event_venue_link_candidates(context.vnd_event["event_id"])
    venue_ids = [c["venue_id"] for c in candidates]
    assert _venue_id_for(context, name) in venue_ids, candidates


@then("an attribution dispute is counted for the handle-mention method")
def step_then_dispute_counted_handle_mention(context):
    before = context.vnd_metric_before.get((METHOD_HANDLE_MENTION, "disputed"), 0.0)
    assert _link_metric(METHOD_HANDLE_MENTION, "disputed") > before


@then("no attribution dispute is counted")
def step_then_no_dispute_counted(context):
    for (method, result), before in context.vnd_metric_before.items():
        assert _link_metric(method, result) == before, (method, result)


@then("the event records that its venue came from a neighbourhood match")
def step_then_event_records_neighbourhood_match(context):
    row = _reload(context)
    assert row.get("linked_by") == METHOD_NEIGHBOURHOOD_MATCH, row.get("linked_by")
    assert row.get("location_resolution") == "auto", row.get("location_resolution")


@then("the event is accepted without review")
def step_then_event_accepted_without_review(context):
    row = _reload(context)
    assert row["status"] == "accepted", (row["status"], row.get("review_reason"))


# "the event is accepted" is already bound (review_gate_and_date_vocabulary_
# steps.py), so these two are worded "the disputed event ..." in the feature
# file — the same distinguishing-wording convention
# event_ticket_info_and_attractions_steps.py's docstring documents for
# exactly this situation.
@then("the disputed event is still accepted")
def step_then_disputed_event_accepted(context):
    row = _reload(context)
    assert row["status"] == "accepted", (row["status"], row.get("review_reason"))


@then("the disputed event is awaiting review")
def step_then_disputed_event_awaiting_review(context):
    row = _reload(context)
    assert row["status"] == "pending_review", (row["status"], row.get("review_reason"))


def _is_selectable(context) -> bool:
    from app.services.event_projection_selection import is_selectable

    row = _reload(context)
    return is_selectable(row, now=_NOW)


@then("the event is selectable for the serving projection")
def step_then_event_selectable(context):
    assert _is_selectable(context), _reload(context)


@then("the event is not selectable for the serving projection")
def step_then_event_not_selectable(context):
    assert not _is_selectable(context), _reload(context)


@then('the handle "{handle}" appears in the venue-acquisition backlog')
def step_then_handle_in_acquisition_backlog(context, handle):
    from app.services.event_dedup_backlog import collect_dedup_backlog

    backlog = collect_dedup_backlog(context.ee_dao)
    texts = " ".join(d.location_text for d in backlog.attribution_disputes)
    assert handle in texts, backlog.attribution_disputes


# ── Defect 2: the historical repair ───────────────────────────────────────
def _seed_stored_event(context, venue_name: str, location_text: str, *, title="SAMBINHA") -> str:
    """A row exactly as the fixed venue-post path stores one: attributed to
    the posting venue, with NO link columns at all, and the event's own
    extracted `location_text` frozen on its source's `raw_extraction`."""
    _ensure(context)
    context.vnd_seq += 1
    event_id = new_event_id()
    context.ee_dao.insert_event({
        "event_id": event_id, "venue_id": _venue_id_for(context, venue_name),
        "starts_at": datetime(2026, 9, 12, 21, 0, tzinfo=RECIFE),
        "title": title, "post_type": "event", "status": "accepted",
        "confidence": 0.9, "lineup": [],
        "source_kind": "venue_post", "source_handle": _POSTING_HANDLE,
        "source_shortcode": f"vnd_stored_{context.vnd_seq}",
        "first_seen_at": _NOW, "last_seen_at": _NOW,
        "raw_extraction": {"location_text": location_text},
    })
    context.vnd_stored_ids = getattr(context, "vnd_stored_ids", []) + [event_id]
    context.vnd_event = context.ee_dao.get_event(event_id)
    return event_id


@given('a stored event at "{venue_name}" whose recorded location text is "{location_text}"')
def step_given_stored_event_with_location_text(context, venue_name, location_text):
    _seed_stored_event(context, venue_name, location_text)


@given('two stored events at "{venue_name}" whose recorded location text is "{location_text}" and whose titles are identical')
def step_given_two_stored_events_same_title(context, venue_name, location_text):
    _seed_stored_event(context, venue_name, location_text, title="SAMBINHA")
    _seed_stored_event(context, venue_name, location_text, title="SAMBINHA")


def _run_disputed_backfill(context, *, apply: bool):
    from scripts.backfill_event_venue_links import (
        MODE_DISPUTED_LOCATION_TEXT, run_backfill,
    )

    context.vnd_backfill_report = run_backfill(
        context.ee_dao, apply=apply, now=_NOW, mode=MODE_DISPUTED_LOCATION_TEXT,
    )
    return context.vnd_backfill_report


@when("the disputed-location-text backfill runs with apply")
def step_when_disputed_backfill_apply(context):
    _run_disputed_backfill(context, apply=True)


@when("the disputed-location-text backfill runs without apply")
def step_when_disputed_backfill_dry_run(context):
    _run_disputed_backfill(context, apply=False)


@when("the disputed-location-text backfill runs with apply twice")
def step_when_disputed_backfill_apply_twice(context):
    _run_disputed_backfill(context, apply=True)
    context.vnd_second_report = _run_disputed_backfill(context, apply=True)


@then("the event's title and start time are unchanged")
def step_then_title_and_start_unchanged(context):
    before = context.vnd_event
    after = context.ee_dao.get_event(before["event_id"])
    assert after["title"] == before["title"], (before["title"], after["title"])
    assert after["starts_at"] == before["starts_at"], (before["starts_at"], after["starts_at"])


@then("the report names the event and its proposed venue")
def step_then_report_names_event_and_proposed_venue(context):
    rows = [r for r in context.vnd_backfill_report.rows if r.action == "repoint"]
    assert rows, context.vnd_backfill_report.rows
    assert any(r.event_id == context.vnd_event["event_id"] for r in rows), rows
    assert any(r.venue_name_after for r in rows), rows


@then("the event is still attributed to \"{name}\"")
def step_then_event_still_attributed(context, name):
    row = context.ee_dao.get_event(context.vnd_event["event_id"])
    assert row["venue_id"] == _venue_id_for(context, name), row["venue_id"]


@then("the second run repairs nothing")
def step_then_second_run_repairs_nothing(context):
    report = context.vnd_second_report
    assert report.repointed == 0, report.rows
    assert report.flagged == 0, report.rows


@then('both events are attributed to "{name}"')
def step_then_both_events_attributed(context, name):
    venue_id = _venue_id_for(context, name)
    for event_id in context.vnd_stored_ids:
        row = context.ee_dao.get_event(event_id)
        assert row["venue_id"] == venue_id, (event_id, row["venue_id"])


@then("both events still exist as separate rows")
def step_then_both_events_separate(context):
    rows = [context.ee_dao.get_event(eid) for eid in context.vnd_stored_ids]
    assert all(r is not None for r in rows), rows
    assert all(r.get("status") != "superseded" for r in rows), rows
    assert len({r["event_id"] for r in rows}) == 2, rows
