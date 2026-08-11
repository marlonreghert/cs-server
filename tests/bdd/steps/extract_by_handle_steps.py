"""Behave steps for tests/bdd/enrichment/extract-by-handle.feature.

See plans/260811_extract-by-handle.md. Drives the REAL `EventExtractionService`
over the SAME `context.ee_*` harness `instagram_event_extraction_steps.py`
already builds — its Background step "the event extraction pipeline is
configured for a known venue" is reused UNMODIFIED (already registered by
`event_ticket_info_and_attractions_steps.py`, the SAME reuse pattern every
sibling enrichment feature in this suite follows), and `_FakePostSource` is
extended THERE (additively — `posts_by_handle_archive` + `posts_for_handle`)
rather than forked here, so every scenario in this file exercises the real
`parse_event_extraction_config`/`EventExtractionService.run()` orchestration
for `mode="handles"` with zero live S3/OpenAI/Apify calls.

One step text ("that post is extracted once", the feature file's original
wording) collides VERBATIM with an existing `@then` in
`stream_dedupe_and_venue_attribution_steps.py` (bound to a completely
different harness, `context.ic_openai`) — an AmbiguousStep collision. Reworded
in the feature file to "that archived post is extracted only once", the same
distinguishing-wording convention `event_ticket_info_and_attractions_steps.py`
and `date_correctness_and_path_parity_steps.py` already document for this
exact situation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.services.event_identity import compute_source_event_key
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_handle_post,
    _add_post,
    _extraction_json,
    _run_extraction,
)

_TS = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _rows_for(context, shortcode: str) -> list[dict]:
    return context.ee_dao.list_events_by_source(context.ee_handle, shortcode)


def _live_rows_for(context, shortcode: str) -> list[dict]:
    return [r for r in _rows_for(context, shortcode) if r["status"] != "superseded"]


# ── Given: where the posts live ────────────────────────────────────────────────
@given("a handle whose posts are archived under that handle")
def step_given_handle_posts_archived_under_that_handle(context):
    _add_handle_post(context, "ebh_handle_only")
    context.ee_openai.program(_extraction_json(date_text="15/08/2026", time_text="20h"))


@given("a handle whose posts are archived under its venue")
def step_given_handle_posts_archived_under_its_venue(context):
    _add_post(context, "ebh_venue_only")
    context.ee_openai.program(_extraction_json(date_text="15/08/2026", time_text="20h"))


@given("a handle with posts archived under both its handle and its venue")
def step_given_handle_posts_span_both_layouts(context):
    _add_post(context, "ebh_span_venue")
    _add_handle_post(context, "ebh_span_handle")
    context.ee_openai.program(_extraction_json(title="Venue Post", date_text="15/08/2026", time_text="20h"))
    context.ee_openai.program(_extraction_json(title="Handle Post", date_text="16/08/2026", time_text="20h"))


@given("a post archived under both a handle and a venue")
def step_given_post_archived_under_both_a_handle_and_a_venue(context):
    shortcode = "ebh_both_prefixes"
    _add_post(context, shortcode, timestamp=_TS)
    _add_handle_post(context, shortcode, timestamp=_TS)
    context.ee_openai.program(_extraction_json(date_text="15/08/2026", time_text="20h"))


@given("a handle with no archived posts")
def step_given_handle_with_no_archived_posts(context):
    pass  # nothing added to either bucket for context.ee_handle


@given("a venue whose posts are archived under that venue")
def step_given_a_venue_whose_posts_are_archived_under_that_venue(context):
    _add_post(context, "ebh_venue_mode")
    context.ee_openai.program(_extraction_json(date_text="15/08/2026", time_text="20h"))


# ── Given: the supersession trap ───────────────────────────────────────────────
@given("an archived post whose event was stored under superseded date rules")
def step_given_post_whose_event_stored_under_superseded_date_rules(context):
    # Simulates a row a resolver from BEFORE plans/260810_post-kind-and-post-
    # extraction-attribution.md §D would have produced for this exact post: a
    # weekday-RANGE recurrence ("de segunda a sexta") that the OLD resolver
    # never read at all (it matched only "toda"/"todo"), so it landed dateless
    # and flagged for review. The archived post itself is unchanged; only the
    # RULES applied to it have moved on.
    shortcode = "ebh_old_rules"
    _add_handle_post(
        context, shortcode, caption="De segunda a sexta tem happy hour! Ingressos na entrada.",
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),  # Monday
    )
    context.ee_dao.insert_event({
        "event_id": "evt_ebh_stale_rules", "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": shortcode,
        "source_event_key": compute_source_event_key("Happy Hour", None),
        "status": "pending_review", "review_reason": "missing_date",
        "title": "Happy Hour", "starts_at": None,
        "raw_extraction": {"time_known": False},
    })
    context.ee_openai.program(_extraction_json(
        title="Happy Hour", date_text="de segunda a sexta", time_text="18h",
        is_recurring=True, recurrence_text="de segunda a sexta",
    ))


@given("an archived post whose event was stored with a wrong date")
def step_given_post_whose_event_stored_with_wrong_date(context):
    shortcode = "ebh_wrong_date"
    _add_handle_post(context, shortcode, timestamp=_TS)
    wrong_starts_at = datetime(2027, 8, 15, 20, 0, tzinfo=timezone.utc)
    context.ee_dao.insert_event({
        "event_id": "evt_ebh_wrong_date", "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": shortcode,
        "source_event_key": compute_source_event_key("Show Errado", wrong_starts_at),
        "status": "pending_review", "title": "Show Errado", "starts_at": wrong_starts_at,
        "raw_extraction": {"time_known": True},
    })
    context.ee_stale_event_id = "evt_ebh_wrong_date"
    context.ee_openai.program(_extraction_json(title="Show Errado", date_text="15/08/2026", time_text="20h"))


@given("an archived post whose event has an operator-edited field")
def step_given_post_whose_event_has_operator_edited_field(context):
    shortcode = "ebh_curated"
    _add_handle_post(context, shortcode, timestamp=_TS)
    starts_at = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    context.ee_dao.insert_event({
        "event_id": "evt_ebh_curated", "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": shortcode,
        "source_event_key": compute_source_event_key("Original Title", starts_at),
        "status": "confirmed", "title": "Título Curado Pelo Operador", "starts_at": starts_at,
        "operator_edited_fields": ["title"],
        "raw_extraction": {"time_known": True},
    })
    context.ee_curated_event_id = "evt_ebh_curated"
    # The model still says what it originally said -- the operator's edit is
    # what must hold, not a fresh answer that happens to differ.
    context.ee_openai.program(_extraction_json(title="Original Title", date_text="15/08/2026", time_text="20h"))


# ── When ────────────────────────────────────────────────────────────────────────
@when("extraction runs for that handle")
def step_when_extraction_runs_for_that_handle(context):
    _run_extraction(context, eligibility={"mode": "handles", "handles": context.ee_handle})


@when("extraction runs for that venue")
def step_when_extraction_runs_for_that_venue(context):
    _run_extraction(context, eligibility={"mode": "venue_ids", "venue_ids": context.ee_venue_id})


# ── Then ──────────────────────────────────────────────────────────────────────
@then("those posts are extracted")
def step_then_those_posts_are_extracted(context):
    assert context.ee_result is not None
    assert context.ee_result["qualifying_posts"] >= 1, context.ee_result
    events = context.ee_dao.list_events(venue_id=context.ee_venue_id)
    assert len(events) >= 1, "expected at least one stored event"


@then("every one of those posts is extracted")
def step_then_every_one_of_those_posts_is_extracted(context):
    row_venue = context.ee_dao.get_event_by_source(context.ee_handle, "ebh_span_venue")
    row_handle = context.ee_dao.get_event_by_source(context.ee_handle, "ebh_span_handle")
    assert row_venue is not None, "the venue-prefixed post was not extracted"
    assert row_handle is not None, "the handle-prefixed post was not extracted"


@then("that archived post is extracted only once")
def step_then_that_archived_post_is_extracted_only_once(context):
    assert context.ee_openai.calls == 1, context.ee_openai.calls
    rows = _rows_for(context, "ebh_both_prefixes")
    assert len(rows) == 1, rows


@then("the run reports that nothing was archived for it")
def step_then_run_reports_nothing_archived(context):
    assert context.ee_result is not None
    reports = context.ee_result["handles"]
    assert len(reports) == 1, reports
    assert reports[0]["handle"] == context.ee_handle
    assert reports[0]["outcome"] == "nothing_archived", reports


@then("the run does not fail")
def step_then_run_does_not_fail(context):
    # Reaching this step at all proves no exception propagated out of
    # `run()` — behave would have errored the scenario otherwise. The
    # explicit shape check is the meaningful assertion: a clean result dict,
    # not an error payload standing in for one.
    assert context.ee_result is not None
    assert isinstance(context.ee_result.get("outcomes"), dict)


@then("no posts are fetched from the crawler")
def step_then_no_posts_fetched_from_the_crawler(context):
    # EventExtractionService has no Apify/crawler-shaped dependency at all
    # (see tests/test_extract_by_handle.py's structural + poisoned-client
    # tests for the regression-proof version of this guarantee) -- the fake
    # post source used here reads only from the two archived-post buckets,
    # never from a live fetch, so a successful run is itself the proof.
    assert context.ee_result is not None
    assert context.ee_result["qualifying_posts"] >= 1


@then("the event's date follows the current rules")
def step_then_event_date_follows_current_rules(context):
    # NOT get_event_by_source: the stale, dateless seed row and the fresh,
    # resolved one are BOTH stored for this shortcode once the identity
    # changes (§B) — get_event_by_source returns at most one row and is not
    # guaranteed to be the live one. The live (non-superseded) row is the
    # one that must reflect the current rules.
    live = _live_rows_for(context, "ebh_old_rules")
    assert len(live) == 1, live
    row = live[0]
    assert row["starts_at"] is not None, "the current resolver must have resolved a date"
    assert row["is_recurring"] is True
    assert row["recurrence_text"] == "de segunda a sexta"


@then("the corrected event is live")
def step_then_the_corrected_event_is_live(context):
    live = _live_rows_for(context, "ebh_wrong_date")
    assert len(live) == 1, live
    assert live[0]["starts_at"].year == 2026, live[0]


@then("the event with the wrong date is superseded")
def step_then_event_with_wrong_date_is_superseded(context):
    row = context.ee_dao.get_event(context.ee_stale_event_id)
    assert row["status"] == "superseded", row


@then("that field keeps the operator's value")
def step_then_field_keeps_the_operators_value(context):
    row = context.ee_dao.get_event(context.ee_curated_event_id)
    assert row["title"] == "Título Curado Pelo Operador", row
    assert row["status"] == "confirmed", row
    rows = _rows_for(context, "ebh_curated")
    assert len(rows) == 1, rows  # never duplicated
