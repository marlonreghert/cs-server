"""Behave steps for tests/bdd/enrichment/backfill-misattributed-links.feature.

See plans/260812_backfill-misattributed-links.md.

Drives `scripts.backfill_event_venue_links.run_backfill` directly against an
in-memory RDS fake (tests/rds_fake.py) wrapped in the SAME `VenueRepository`
production wiring uses — no Apify/S3/OpenAI client exists anywhere in this
harness, so nothing here CAN spend, by construction (see the "Refuse to
spend anything" scenario's steps at the bottom).

Fixture rows are inserted directly via `venue_dao.insert_event` to represent
what the OLD, pre-fix precedence already wrote to `events.post_item` —
`linked_by='handle_mention'` even for what was really a caption-level match
— never by running a promoter crawl, since this feature repairs already-
persisted history, not a live extraction.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_identity import compute_source_event_key
from app.services.event_venue_resolution import (
    METHOD_HANDLE_MENTION,
    RESOLUTION_AUTO,
    RESOLUTION_MANUAL,
)
from scripts.backfill_event_venue_links import (
    ArithmeticImbalance,
    DependencyNotLanded,
    Report,
    SKIP_CONFIRMED,
    SKIP_EXTRACTION_FAILED,
    SKIP_MANUAL_LINK,
    SKIP_OPERATOR_EDITED_VENUE,
    SKIP_SUPERSEDED,
    WriteAffectedNoRows,
    check_balance,
    run_backfill,
)
from tests.rds_fake import InMemoryRdsVenueStore

_DEFAULT_STARTS_AT = datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc)
_PROMOTER_HANDLE = "oquetemhojeemnatal"


# ── harness setup ────────────────────────────────────────────────────────────
def _ensure_bfl(context) -> None:
    if getattr(context, "bfl_dao", None) is not None:
        return
    context.bfl_store = InMemoryRdsVenueStore()
    context.bfl_dao = VenueRepository(client=None, rds_store=context.bfl_store)
    context.bfl_venues_by_name: dict[str, str] = {}
    context.bfl_item_ids: list[str] = []
    context.bfl_item_counter = 0
    context.bfl_report = None
    context.bfl_exception = None


def _add_venue(context, name: str, handle: "str | None" = None) -> str:
    _ensure_bfl(context)
    slug = handle or name.lower().replace(" ", "_")
    venue_id = f"v_{slug}"
    context.bfl_dao.upsert_venue(Venue(
        venue_id=venue_id, venue_name=name, venue_lat=-8.05, venue_lng=-34.88,
        venue_address="",
    ))
    if handle:
        context.bfl_dao.set_venue_instagram(
            VenueInstagram(venue_id=venue_id, instagram_handle=handle, status="found")
        )
    context.bfl_venues_by_name[name] = venue_id
    return venue_id


def _insert_item(
    context, *, venue_name: "str | None", status: str = "accepted",
    location_resolution: "str | None" = RESOLUTION_AUTO,
    linked_by: "str | None" = METHOD_HANDLE_MENTION,
    review_reason: "str | None" = None, operator_edited_fields=None,
    location_text: "str | None" = None, starts_at=_DEFAULT_STARTS_AT,
    confidence: "float | None" = 0.9, title: "str | None" = None,
    source_shortcode: "str | None" = None,
) -> str:
    _ensure_bfl(context)
    context.bfl_item_counter += 1
    n = context.bfl_item_counter
    event_id = f"evt_bfl_{n:04d}"
    title = title or f"Festa {n}"
    shortcode = source_shortcode or f"bfl_post_{n}"
    venue_id = context.bfl_venues_by_name[venue_name] if venue_name else None
    key = compute_source_event_key(title, starts_at)

    fields = {
        "event_id": event_id, "venue_id": venue_id, "starts_at": starts_at,
        "title": title, "status": status, "review_reason": review_reason,
        "location_resolution": location_resolution, "location_confidence": 1.0 if venue_id else None,
        "linked_by": linked_by, "linked_at": datetime.now(timezone.utc) if venue_id else None,
        "operator_edited_fields": operator_edited_fields,
        "confidence": confidence, "location_text": location_text,
        "source_kind": "promoter_post", "source_handle": _PROMOTER_HANDLE,
        "source_shortcode": shortcode, "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": key, "source_event_index": 1,
        "raw_extraction": {"location_text": location_text},
        "first_seen_at": datetime.now(timezone.utc), "last_seen_at": datetime.now(timezone.utc),
    }
    context.bfl_dao.insert_event(fields)
    context.bfl_item_ids.append(event_id)
    return event_id


def _last_item(context) -> str:
    return context.bfl_item_ids[-1]


def _set_extraction_location(context, text: "str | None", item_id: "str | None" = None) -> None:
    item_id = item_id or _last_item(context)
    context.bfl_dao.update_event(item_id, {"raw_extraction": {"location_text": text}})


def _row(context, item_id: "str | None" = None) -> dict:
    item_id = item_id or _last_item(context)
    row = context.bfl_dao.get_event(item_id)
    assert row is not None, item_id
    return row


# ── Background ────────────────────────────────────────────────────────────────
@given("the per-event venue attribution fix has landed")
def step_given_fix_has_landed(context):
    _ensure_bfl(context)
    import app.services.event_venue_resolution as evr
    assert hasattr(evr, "METHOD_CAPTION_HANDLE_MENTION"), "fixture assumption broken"


@given('the venue catalog holds "{name}" with Instagram handle "{handle}"')
def step_given_catalog_holds(context, name, handle):
    _add_venue(context, name, handle)


# ── item creation ─────────────────────────────────────────────────────────────
@given('an accepted item linked to "{venue_name}" by a caption mention')
def step_given_accepted_item(context, venue_name):
    _insert_item(context, venue_name=venue_name, status="accepted")


@given('a confirmed item linked to "{venue_name}" by a caption mention')
def step_given_confirmed_item(context, venue_name):
    _insert_item(context, venue_name=venue_name, status="confirmed")


@given('a manually linked item linked to "{venue_name}"')
def step_given_manually_linked_item(context, venue_name):
    # Historical inconsistency, deliberately: a row can carry linked_by=
    # 'handle_mention' from an earlier auto-link AND location_resolution=
    # 'manual' from a later operator override that never reset linked_by —
    # this is exactly the shape plan §B's skip table must refuse to touch,
    # so the fixture constructs it directly rather than relaying it through
    # whichever endpoint would set it in production.
    _insert_item(
        context, venue_name=venue_name, status="accepted",
        location_resolution=RESOLUTION_MANUAL,
    )


@given('two accepted items linked to "{venue_name}" by a caption mention')
def step_given_two_accepted_items(context, venue_name):
    _insert_item(context, venue_name=venue_name, status="accepted")
    _insert_item(context, venue_name=venue_name, status="accepted")


@given('a superseded item linked to "{venue_name}" by a caption mention')
def step_given_superseded_item(context, venue_name):
    _insert_item(context, venue_name=venue_name, status="superseded")


@given("an extraction-failed item with no content identity")
def step_given_extraction_failed_item(context):
    # linked_by is forced to handle_mention here purely so selection reaches
    # this row at all — a real extraction_failed placeholder has no
    # source_event_key and is never attributed, but the defensive skip rule
    # (plan §B) must hold even for a row that reached this state some other,
    # buggier way.
    _ensure_bfl(context)
    context.bfl_item_counter += 1
    n = context.bfl_item_counter
    event_id = f"evt_bfl_{n:04d}"
    shortcode = f"bfl_post_{n}"
    fields = {
        "event_id": event_id, "venue_id": None, "starts_at": None,
        "title": None, "status": "extraction_failed", "review_reason": None,
        "location_resolution": None, "location_confidence": None,
        "linked_by": METHOD_HANDLE_MENTION, "linked_at": None,
        "operator_edited_fields": None, "confidence": None, "location_text": None,
        "source_kind": "promoter_post", "source_handle": _PROMOTER_HANDLE,
        "source_shortcode": shortcode, "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": None, "source_event_index": 1,
        "raw_extraction": None,
        "first_seen_at": datetime.now(timezone.utc), "last_seen_at": datetime.now(timezone.utc),
    }
    context.bfl_dao.insert_event(fields)
    context.bfl_item_ids.append(event_id)


@given('a pending item linked to "{venue_name}" by a caption mention')
def step_given_pending_item(context, venue_name):
    _insert_item(context, venue_name=venue_name, status="pending_review", review_reason="needs_review")


# ── item mutation ─────────────────────────────────────────────────────────────
@given('its stored extraction states the location "{text}"')
def step_given_stored_extraction_location(context, text):
    _set_extraction_location(context, text)


@given("its stored extraction states no location at all")
def step_given_no_stored_location(context):
    _set_extraction_location(context, None)


@given("its stored extraction states a venue that is not in the catalog")
def step_given_extraction_states_unknown_venue(context):
    _set_extraction_location(context, "@donanapubnatal")


@given('no venue in the catalog has the handle "{handle}"')
def step_given_no_venue_has_handle(context, handle):
    _ensure_bfl(context)
    assert handle not in (context.bfl_dao.list_instagram_handles() or {}).values(), handle


@given("an operator has edited the item's venue")
def step_given_operator_edited_venue(context):
    context.bfl_dao.update_event(_last_item(context), {"operator_edited_fields": ["venue_id"]})


@given('an operator has corrected the item\'s location text to "{text}"')
def step_given_operator_corrected_location_text(context, text):
    context.bfl_dao.update_event(_last_item(context), {
        "operator_edited_fields": ["location_text"], "location_text": text,
    })


@given("the item is queued because its date could not be read")
def step_given_queued_missing_date(context):
    context.bfl_dao.update_event(_last_item(context), {
        "review_reason": "missing_date", "starts_at": None,
    })


@given("the item is queued only because its venue could not be resolved")
def step_given_queued_unresolved_venue(context):
    context.bfl_dao.update_event(_last_item(context), {"review_reason": "unresolved_venue"})


@given("the item has a resolved date and confidence above the floor")
def step_given_resolved_date_high_confidence(context):
    context.bfl_dao.update_event(_last_item(context), {
        "starts_at": _DEFAULT_STARTS_AT, "confidence": 0.9,
    })


@given("the item has confidence below the floor")
def step_given_low_confidence(context):
    context.bfl_dao.update_event(_last_item(context), {"confidence": 0.1})


@given("both stored extractions state the location \"{text}\"")
def step_given_both_stored_extractions(context, text):
    _set_extraction_location(context, text, context.bfl_item_ids[-2])
    _set_extraction_location(context, text, context.bfl_item_ids[-1])


@given("both items share a date and a title")
def step_given_both_items_share_date_title(context):
    shared_title = "Noite Compartilhada"
    context.bfl_dao.update_event(context.bfl_item_ids[-2], {
        "title": shared_title,
        "source_event_key": compute_source_event_key(shared_title, _DEFAULT_STARTS_AT),
    })
    context.bfl_dao.update_event(context.bfl_item_ids[-1], {
        "title": shared_title,
        "source_event_key": compute_source_event_key(shared_title, _DEFAULT_STARTS_AT),
    })


@given("another item from the same account shares its date and title and is linked to a venue")
def step_given_sibling_shares_identity_and_is_linked(context):
    detached_item = _last_item(context)
    detached_row = _row(context, detached_item)
    sibling_venue = _add_venue(context, "Sibling Venue", "siblingvenuehandle")
    context.bfl_item_counter += 1
    n = context.bfl_item_counter
    event_id = f"evt_bfl_{n:04d}"
    shortcode = f"bfl_post_{n}"
    fields = {
        "event_id": event_id, "venue_id": sibling_venue, "starts_at": detached_row["starts_at"],
        "title": detached_row["title"], "status": "accepted", "review_reason": None,
        "location_resolution": RESOLUTION_AUTO, "location_confidence": 1.0,
        "linked_by": "name_match", "linked_at": datetime.now(timezone.utc),
        "operator_edited_fields": None, "confidence": 0.9, "location_text": "Sibling Venue",
        "source_kind": "promoter_post", "source_handle": _PROMOTER_HANDLE,
        "source_shortcode": shortcode, "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": compute_source_event_key(detached_row["title"], detached_row["starts_at"]),
        "source_event_index": 1, "raw_extraction": {"location_text": "Sibling Venue"},
        "first_seen_at": datetime.now(timezone.utc), "last_seen_at": datetime.now(timezone.utc),
    }
    context.bfl_dao.insert_event(fields)
    context.bfl_item_ids.append(event_id)
    context.bfl_sibling_item = event_id
    context.bfl_detached_item = detached_item


# ── roundup / bulk fixtures ────────────────────────────────────────────────────
@given('a roundup post whose twenty items are all linked to "{venue_name}" by a caption mention')
def step_given_roundup_twenty_items(context, venue_name):
    _ensure_bfl(context)
    shortcode = "bfl_roundup_post"
    context.bfl_roundup_venues = []
    context.bfl_roundup_items = []
    for i in range(20):
        name = f"Roundup Venue {i}"
        handle = f"roundupvenue{i}"
        _add_venue(context, name, handle)
        context.bfl_roundup_venues.append((name, handle))
        item_id = _insert_item(
            context, venue_name=venue_name, status="accepted",
            title=f"Evento {i}", source_shortcode=shortcode,
        )
        context.bfl_roundup_items.append(item_id)


@given("each item's stored extraction states a different venue handle")
def step_given_each_item_states_different_handle(context):
    for item_id, (_name, handle) in zip(context.bfl_roundup_items, context.bfl_roundup_venues):
        _set_extraction_location(context, f"@{handle}", item_id)


@given("a roundup post whose items are all linked by a caption mention")
def step_given_roundup_items_linked_by_caption(context):
    _ensure_bfl(context)
    _add_venue(context, "Wrong Venue For Roundup", "wrongvenueroundup")
    shortcode = "bfl_spend_post"
    for i in range(3):
        item_id = _insert_item(
            context, venue_name="Wrong Venue For Roundup", status="accepted",
            title=f"Item {i}", source_shortcode=shortcode,
        )
        _set_extraction_location(context, f"@unknownhandle{i}", item_id)


@given("twelve accepted items linked by a caption mention whose stated venues are not in the catalog")
def step_given_twelve_items_unknown_venues(context):
    _ensure_bfl(context)
    _add_venue(context, "Wrong Venue For Backlog", "wrongvenuebacklog")
    # A skewed distribution -- one handle named by five items, one by three,
    # the rest named once each -- so "ordered by that number" is a genuine
    # assertion, not one that would pass on an accidental tie.
    handles = (
        ["popularvenue"] * 5 + ["secondvenue"] * 3
        + [f"raregem{i}" for i in range(4)]
    )
    assert len(handles) == 12, handles
    for i, handle in enumerate(handles):
        item_id = _insert_item(
            context, venue_name="Wrong Venue For Backlog", status="accepted",
            title=f"Backlog Item {i}",
        )
        _set_extraction_location(context, f"@{handle}", item_id)


# ── running the backfill ───────────────────────────────────────────────────────
def _run(context, *, apply: bool, since_id=None) -> None:
    """Shared by this feature AND `backfill-source-provenance.feature`: both
    use the literal Gherkin text "the backfill runs with/without apply[,
    again]", and Behave allows only ONE registration of an identical step
    pattern across the whole `tests/bdd/steps/` directory — a second
    `@when("the backfill runs with apply")` elsewhere raises AmbiguousStep.
    Rather than inventing different wording for what is, in both features,
    the same sentence about the same kind of operator script, this function
    dispatches on which fixture the scenario actually built:
    `context.bsp_dao` (tests/bdd/steps/backfill_source_provenance_steps.py)
    or the `context.bfl_dao` this module itself builds — a scenario only
    ever constructs one of the two, never both.
    """
    if getattr(context, "bsp_dao", None) is not None:
        from tests.bdd.steps.backfill_source_provenance_steps import run_bsp_backfill

        run_bsp_backfill(context, apply=apply, since_id=since_id)
        return
    _ensure_bfl(context)
    context.bfl_exception = None
    try:
        context.bfl_report = run_backfill(context.bfl_dao, apply=apply, since_id=since_id)
    except Exception as exc:  # noqa: BLE001 - captured for Then steps to inspect
        context.bfl_exception = exc
        context.bfl_report = getattr(exc, "report", None)


@when("the backfill runs with apply")
def step_when_backfill_runs_with_apply(context):
    _run(context, apply=True)


@when("the backfill runs without apply")
def step_when_backfill_runs_without_apply(context):
    _run(context, apply=False)


@given("the backfill has already run with apply")
def step_given_backfill_already_ran(context):
    _run(context, apply=True)
    assert context.bfl_exception is None, context.bfl_exception


@when("the backfill runs with apply again")
def step_when_backfill_runs_with_apply_again(context):
    _run(context, apply=True)


@when("the backfill is started")
def step_when_backfill_is_started(context):
    _run(context, apply=False)


@given("a backfill run whose per-outcome counts do not sum to the rows it selected")
def step_given_imbalanced_report(context):
    _ensure_bfl(context)
    context.bfl_report = Report(selected=5, repointed=1, detached=1, unchanged=1)


@when("the backfill finishes")
def step_when_backfill_finishes(context):
    context.bfl_exception = None
    try:
        check_balance(context.bfl_report)
    except ArithmeticImbalance as exc:
        context.bfl_exception = exc


@given("the update of that item affects no rows")
def step_given_update_affects_no_rows(context):
    _ensure_bfl(context)
    original = context.bfl_store.update_event

    def _always_none(event_id, fields):
        return None

    context.bfl_store.update_event = _always_none
    context.add_cleanup(lambda: setattr(context.bfl_store, "update_event", original))


@given("the per-event venue attribution fix has not landed")
def step_given_fix_not_landed(context):
    _ensure_bfl(context)
    import app.services.event_venue_resolution as evr

    original = evr.METHOD_CAPTION_HANDLE_MENTION
    delattr(evr, "METHOD_CAPTION_HANDLE_MENTION")

    def _restore():
        evr.METHOD_CAPTION_HANDLE_MENTION = original

    context.add_cleanup(_restore)

    # Proves "before reading any item": a DB read would raise through this
    # spy, so a passing scenario means DependencyNotLanded fired first.
    def _list_events_spy(*a, **kw):
        raise AssertionError("run_backfill read events before checking the dependency guard")

    context.bfl_store.list_events = _list_events_spy


# ── assertions ─────────────────────────────────────────────────────────────────
@then('the item is linked to "{venue_name}"')
def step_then_item_linked_to(context, venue_name):
    row = _row(context)
    expected = context.bfl_venues_by_name[venue_name]
    assert row["venue_id"] == expected, (venue_name, row)


@then('the item is not linked to "{venue_name}"')
def step_then_item_not_linked_to(context, venue_name):
    row = _row(context)
    unwanted = context.bfl_venues_by_name[venue_name]
    assert row["venue_id"] != unwanted, row


@then('the item is still linked to "{venue_name}"')
def step_then_item_still_linked_to(context, venue_name):
    step_then_item_linked_to(context, venue_name)


@then("the item records that the link came from its own stated location")
def step_then_link_from_own_location(context):
    row = _row(context)
    assert row["linked_by"] == METHOD_HANDLE_MENTION, row


@then("each item is linked to the venue its own stored location names")
def step_then_each_item_linked_to_own_venue(context):
    for item_id, (name, _handle) in zip(context.bfl_roundup_items, context.bfl_roundup_venues):
        row = _row(context, item_id)
        expected = context.bfl_venues_by_name[name]
        assert row["venue_id"] == expected, (name, row)


@then("no two of those items share a venue unless their stored locations agree")
def step_then_no_unwanted_shared_venues(context):
    venue_ids = [_row(context, i)["venue_id"] for i in context.bfl_roundup_items]
    assert len(set(venue_ids)) == len(venue_ids), venue_ids


@then("the item has no venue")
def step_then_item_has_no_venue(context):
    row = _row(context)
    assert row["venue_id"] is None, row


@then("the detached item has no venue")
def step_then_detached_item_has_no_venue(context):
    row = _row(context, context.bfl_detached_item)
    assert row["venue_id"] is None, row


@then("the item is queued with the reason that its venue is not in the catalog")
def step_then_queued_venue_not_in_catalog(context):
    row = _row(context)
    assert "venue_not_in_catalog" in (row.get("review_reason") or ""), row


@then("the item is not queued with the reason that its venue is not in the catalog")
def step_then_not_queued_venue_not_in_catalog(context):
    row = _row(context)
    assert "venue_not_in_catalog" not in (row.get("review_reason") or ""), row


@then("the item is queued with the reason that its venue could not be resolved")
def step_then_queued_unresolved(context):
    row = _row(context)
    assert "unresolved_venue" in (row.get("review_reason") or ""), row


@then("the item is not queued with the reason that its venue could not be resolved")
def step_then_not_queued_unresolved(context):
    row = _row(context)
    assert "unresolved_venue" not in (row.get("review_reason") or ""), row


@then("the item is still confirmed")
def step_then_still_confirmed(context):
    assert _row(context)["status"] == "confirmed", _row(context)


@then("the item's location resolution is still manual")
def step_then_location_resolution_still_manual(context):
    assert _row(context)["location_resolution"] == RESOLUTION_MANUAL, _row(context)


@then("the item's location resolution is automatic")
def step_then_location_resolution_automatic(context):
    assert _row(context)["location_resolution"] == RESOLUTION_AUTO, _row(context)


@then("the item is still superseded")
def step_then_still_superseded(context):
    assert _row(context)["status"] == "superseded", _row(context)


@then("the item is unchanged")
def step_then_item_unchanged(context):
    row = _row(context)
    assert row["status"] == "extraction_failed", row
    assert row["venue_id"] is None, row


@then("the item is still pending review")
def step_then_still_pending_review(context):
    assert _row(context)["status"] == "pending_review", _row(context)


@then("the item is still queued because its date could not be read")
def step_then_still_queued_missing_date(context):
    row = _row(context)
    assert "missing_date" in (row.get("review_reason") or ""), row


@then("the item is accepted")
def step_then_item_accepted(context):
    assert _row(context)["status"] == "accepted", _row(context)


@then("the item has no review reason")
def step_then_no_review_reason(context):
    row = _row(context)
    assert not row.get("review_reason"), row


@then('the report counts the item as skipped because {reason_phrase}')
def step_then_report_counts_skipped(context, reason_phrase):
    mapping = {
        "an operator confirmed it": SKIP_CONFIRMED,
        "an operator linked it": SKIP_MANUAL_LINK,
        "an operator edited its venue": SKIP_OPERATOR_EDITED_VENUE,
        "it was superseded": SKIP_SUPERSEDED,
        "its extraction failed": SKIP_EXTRACTION_FAILED,
    }
    reason = mapping[reason_phrase]
    report = context.bfl_report
    assert report is not None, context.bfl_exception
    assert report.skipped_by_reason.get(reason, 0) >= 1, report.skipped_by_reason


@then("the report names the item, its current venue and the venue it would move to")
def step_then_report_names_item_and_venues(context):
    report = context.bfl_report
    item_id = _last_item(context)
    decision = next(d for d in report.rows if d.event_id == item_id)
    assert decision.venue_name_before == "Teatro Riachuelo", decision
    assert decision.venue_name_after == "Sempre Rock Bar", decision


@then("the report counts no changes")
def step_then_report_counts_no_changes(context):
    report = context.bfl_report
    assert report.changed_count == 0, report


@then('both items are linked to "{venue_name}"')
def step_then_both_items_linked_to(context, venue_name):
    expected = context.bfl_venues_by_name[venue_name]
    for item_id in context.bfl_item_ids[-2:]:
        row = _row(context, item_id)
        assert row["venue_id"] == expected, (item_id, row)


@then("both items still exist")
def step_then_both_items_still_exist(context):
    for item_id in context.bfl_item_ids[-2:]:
        assert context.bfl_dao.get_event(item_id) is not None, item_id


@then("the report names the pair as a venue identity collision")
def step_then_report_names_venue_identity_collision(context):
    ids = set(context.bfl_item_ids[-2:])
    report = context.bfl_report
    found = any(ids <= set(group_ids) for _identity, group_ids in report.venue_identity_collisions)
    assert found, (ids, report.venue_identity_collisions)


@then("the report names the pair as a handle identity collision")
def step_then_report_names_handle_identity_collision(context):
    report = context.bfl_report
    found = any(
        detached_id == context.bfl_detached_item and sibling_id == context.bfl_sibling_item
        for detached_id, sibling_id, _identity in report.handle_identity_collisions
    )
    assert found, (context.bfl_detached_item, context.bfl_sibling_item, report.handle_identity_collisions)


@then("the report lists each stated handle with the number of items it would recover")
def step_then_report_lists_handles(context):
    report = context.bfl_report
    backlog = dict(report.venue_acquisition_backlog)
    assert backlog.get("popularvenue") == 5, backlog
    assert backlog.get("secondvenue") == 3, backlog
    assert sum(backlog.values()) == 12, backlog


@then("the list is ordered by that number")
def step_then_backlog_ordered(context):
    counts = [count for _handle, count in context.bfl_report.venue_acquisition_backlog]
    assert counts == sorted(counts, reverse=True), counts


@then("the backfill exits with a failure")
def step_then_backfill_exits_with_failure(context):
    assert context.bfl_exception is not None, "expected the backfill to raise"


@then("the report says the totals did not balance")
def step_then_report_says_imbalance(context):
    assert "did not balance" in str(context.bfl_exception), context.bfl_exception


@then("the report names the item whose write did nothing")
def step_then_report_names_failed_write(context):
    assert isinstance(context.bfl_exception, WriteAffectedNoRows), context.bfl_exception
    assert context.bfl_exception.event_id == _last_item(context), context.bfl_exception


@then("the backfill exits with a failure before reading any item")
def step_then_exits_before_reading(context):
    assert isinstance(context.bfl_exception, DependencyNotLanded), context.bfl_exception


@then("no item is changed")
def step_then_no_item_changed(context):
    # Nothing was ever created in this scenario -- the dependency guard
    # fired before any fixture row could even matter. Presence of the
    # DependencyNotLanded exception (checked by the previous step) already
    # proves this; this step exists for the Gherkin to read naturally.
    assert context.bfl_item_ids == [], context.bfl_item_ids


@then("no Apify request is made")
def step_then_no_apify_request(context):
    _assert_script_never_imports(["apify", "instagram_client"])


@then("no OpenAI request is made")
def step_then_no_openai_request(context):
    _assert_script_never_imports(["openai"])


@then("no archived post is read from storage")
def step_then_no_archive_read(context):
    _assert_script_never_imports(["boto3", "s3", "media_store", "media_archive", "downloader"])


def _assert_script_never_imports(forbidden_substrings: list) -> None:
    """Static proof, not a mock: parse the script's OWN import statements
    (never its prose — the module's docstring legitimately names "Apify"
    while explaining that it never calls it) and assert none names a
    network client. A script that genuinely cannot import such a client
    cannot possibly call one at runtime."""
    import ast
    import inspect

    import scripts.backfill_event_venue_links as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append(node.module)

    lowered = [name.lower() for name in imported_names]
    for token in forbidden_substrings:
        assert not any(token in name for name in lowered), (
            f"script imports something matching {token!r}: {imported_names}"
        )
