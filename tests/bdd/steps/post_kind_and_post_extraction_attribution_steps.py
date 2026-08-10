"""Behave steps for
tests/bdd/enrichment/post-kind-and-post-extraction-attribution.feature.

See plans/260810_post-kind-and-post-extraction-attribution.md. Three
independent things this file drives, mirrored by the feature file's own
three sections:

  - §A/§B (what a post is, what reaches a human): the REAL
    `EventExtractionService`, over the SAME `context.ee_*` harness
    `instagram_event_extraction_steps.py` already built — its Background
    step ("the event extraction pipeline is configured for a known venue",
    defined in event_ticket_info_and_attractions_steps.py) and its
    `_add_post`/`_run_extraction`/`_stored_event` helpers are reused
    verbatim, the same pattern every sibling extraction feature file in this
    suite already follows. Re-scoped mid-execution (see the plan's own
    note): a non-event post produces NO `events.event` row at all — this
    file asserts absence, a metric counter, and a log line, never a
    persisted `kind` column (there isn't one).

  - §C (attribution after extraction): the REAL `ScheduledInstagramCrawl-
    Service`/`InstagramCrawlChainer`/`PromoterCrawlService`, over the SAME
    `context.ic_*` harness `scheduled_incremental_instagram_crawl_steps.py`
    built and `stream_dedupe_and_venue_attribution_steps.py` already reuses
    for a shared-handle fixture (`context.sdva_*`) — reused here rather
    than a third copy of the same two-venue-handle setup.

  - §D (recurrence): `app.services.event_date_resolver.resolve_event_
    datetime` called directly — it is a pure function, so no harness beyond
    a post timestamp is needed.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.services.event_date_resolver import REASON_MISSING_DATE, resolve_event_datetime
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _ensure_context as _ee_ensure_context,
    _extraction_json,
    _stored_event,
)
from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import (
    _extraction_json as _ic_extraction_json,
    _post,
    _program_posts,
    _run as _ic_run,
)
from tests.bdd.steps.stream_dedupe_and_venue_attribution_steps import (
    _BRANCH_CAPTION,
    _NEITHER_CAPTION,
    _VENUE_B_NAME,
)

RECIFE = ZoneInfo("America/Recife")


def _counter_total(name: str) -> float:
    """Sum every label combination of a Counter — used where the scenario
    cares whether ANY sample under this name changed, not one specific
    label tuple (e.g. "no venue resolution was attempted at all"). Safe
    across a shared, cumulative REGISTRY: only the DELTA within one
    scenario's own before/after snapshot is ever asserted, never an
    absolute value another scenario's run could have contributed to."""
    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != name:
            continue
        for sample in metric.samples:
            if sample.name == f"{name}_total":
                total += sample.value
    return total


def _kind_metric(outcome: str, kind: str) -> float:
    return REGISTRY.get_sample_value(
        "event_extraction_posts_total", {"outcome": outcome, "kind": kind},
    ) or 0.0


# ── §A/§B: what a post is, what reaches a human ──────────────────────────
@given("a post announcing a dish available on weekdays between set hours")
def step_given_weekday_lunch_special(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_menu_post")
    context.pk_kind_before = _kind_metric("not_an_event", "menu")
    context.ee_openai.program(_extraction_json(
        kind="menu", title="Especial do dia", date_text=None, time_text=None,
    ))


@given("a post announcing a discount for a group of customers")
def step_given_group_discount_offer(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_promo_post")
    context.pk_kind_before = _kind_metric("not_an_event", "promotion")
    context.ee_openai.program(_extraction_json(
        kind="promotion", title="Happy hour em grupo", date_text=None, time_text=None,
    ))


@given("a post announcing a DJ night on a stated date")
def step_given_dj_night(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_event_post")
    context.ee_openai.program(_extraction_json(
        kind="event", title="Noite do DJ", date_text="15/08", time_text="22h",
    ))


@given("a post showing a dish with no offer and no date")
def step_given_plain_food_photo(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_food_post")
    context.pk_kind_before = _kind_metric("not_an_event", "food")
    context.ee_openai.program(_extraction_json(
        kind="food", title=None, date_text=None, time_text=None,
    ))


@given("a post announcing a show on a stated date with a drinks offer")
def step_given_event_with_drinks_offer(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_event_with_offer_post")
    context.ee_openai.program(_extraction_json(
        kind="event", title="Show + open bar", date_text="20/08", time_text="23h",
    ))


@given("a post whose extraction response omits a kind")
def step_given_post_omits_kind(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_missing_kind_post")
    # No `kind` override at all: `_extraction_json`'s payload never sets one
    # — this is a response shaped exactly like the model left the field out.
    context.ee_openai.program(_extraction_json())


@given("a post whose extraction response names a kind this pipeline does not know")
def step_given_post_names_unrecognised_kind(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_unknown_kind_post")
    context.ee_openai.program(_extraction_json(kind="giveaway"))


@then("the post is recorded as an event")
def step_then_post_recorded_as_event(context):
    # _stored_event raises its own assertion (with the handle/shortcode) if
    # no row exists — exactly the property this step is checking.
    _stored_event(context)


@then("the extraction is counted as a menu item")
def step_then_counted_as_menu(context):
    after = _kind_metric("not_an_event", "menu")
    assert after - context.pk_kind_before == 1.0, (context.pk_kind_before, after)


@then("the extraction is counted as a promotion")
def step_then_counted_as_promotion(context):
    after = _kind_metric("not_an_event", "promotion")
    assert after - context.pk_kind_before == 1.0, (context.pk_kind_before, after)


@then("the extraction is counted as food")
def step_then_counted_as_food(context):
    after = _kind_metric("not_an_event", "food")
    assert after - context.pk_kind_before == 1.0, (context.pk_kind_before, after)


@then("no event is recorded for the post")
def step_then_no_event_recorded(context):
    row = context.ee_dao.get_event_by_source(context.ee_handle, context.ee_last_shortcode)
    assert row is None, row


@when("the review queue is listed")
def step_when_review_queue_listed(context):
    context.pk_queue = context.ee_dao.list_events_awaiting_decision() or []


@then("no event for that post is in the queue")
def step_then_no_event_for_post_in_queue(context):
    matches = [
        r for r in context.pk_queue
        if r.get("source_handle") == context.ee_handle
        and r.get("source_shortcode") == context.ee_last_shortcode
    ]
    assert matches == [], matches


@when("every event is listed")
def step_when_every_event_listed(context):
    context.pk_all_events = context.ee_dao.list_events() or []


@then("no event for that post is in the list")
def step_then_no_event_for_post_in_list(context):
    matches = [
        r for r in context.pk_all_events
        if r.get("source_handle") == context.ee_handle
        and r.get("source_shortcode") == context.ee_last_shortcode
    ]
    assert matches == [], matches


@then("that post's event is present in the review queue")
def step_then_posts_event_present_in_queue(context):
    row = _stored_event(context)
    awaiting_ids = {
        r["event_id"] for r in (context.ee_dao.list_events_awaiting_decision() or [])
    }
    assert row["event_id"] in awaiting_ids, (row, awaiting_ids)


# ── §C: attributing a venue after extraction ─────────────────────────────
# "a crawl target whose handle belongs to two venues" / "...to one venue"
# and "the event awaits a human decision" / "the event is attributed to no
# venue" are ALREADY registered by stream_dedupe_and_venue_attribution_
# steps.py (behave's step registry is global across every steps/*.py file
# loaded) — reused verbatim rather than redefined, which would collide.
@given("an extracted post whose location text names one of them")
def step_given_post_location_text_names_one(context):
    _program_posts(context, context.ic_handle, "posts", [
        _post(context.sdva_shortcode, "2026-08-05T20:00:00.000Z", caption=_NEITHER_CAPTION),
    ])
    context.ic_openai._responses = []
    context.ic_openai.program(_ic_extraction_json(location_text=_VENUE_B_NAME))
    context.pk_expected_venue_id = context.sdva_venue_b_id


@given("an extracted post with no location text whose caption names one venue")
def step_given_post_no_location_text_caption_names_one(context):
    _program_posts(context, context.ic_handle, "posts", [
        _post(context.sdva_shortcode, "2026-08-05T20:00:00.000Z", caption=_BRANCH_CAPTION),
    ])
    context.ic_openai._responses = []
    context.ic_openai.program(_ic_extraction_json(location_text=None))
    context.pk_expected_venue_id = context.sdva_venue_b_id


@given("an extracted post naming neither venue")
def step_given_post_naming_neither(context):
    _program_posts(context, context.ic_handle, "posts", [
        _post(context.sdva_shortcode, "2026-08-05T20:00:00.000Z", caption=_NEITHER_CAPTION),
    ])
    context.ic_openai._responses = []
    context.ic_openai.program(_ic_extraction_json(location_text=None))


@when("the post is attributed")
def step_when_the_post_is_attributed(context):
    context.pk_link_total_before = _counter_total("event_venue_link_total")
    context.ic_last_report = _ic_run(context.ic_service.run_target(context.ic_handle))


def _pk_sole_event(context) -> dict:
    rows = context.ic_dao.list_events_by_source(context.ic_handle, context.sdva_shortcode)
    assert len(rows) == 1, rows
    return rows[0]


@then("the event is attributed to that venue")
def step_then_attributed_to_that_venue(context):
    expected = getattr(context, "pk_expected_venue_id", None) or context.ic_venue_ids[-1]
    row = _pk_sole_event(context)
    assert row["venue_id"] == expected, (row, expected)


@then("no venue resolution is attempted")
def step_then_no_venue_resolution_attempted(context):
    after = _counter_total("event_venue_link_total")
    assert after == context.pk_link_total_before, (context.pk_link_total_before, after)


@then("the post's images are archived under the handle")
def step_then_archived_under_handle(context):
    assert context.ic_media_store.images, context.ic_media_store.images
    for key in context.ic_media_store.images:
        assert f"promoter={context.ic_handle}/" in key, key


# ── §D: recurring programming ─────────────────────────────────────────────
_PK_ANCHOR = datetime(2026, 8, 3, 20, 0, tzinfo=RECIFE)  # a Monday


@given("an extracted event recurring from one weekday to another")
def step_given_weekday_range_recurrence(context):
    context.pk_date_text = None
    context.pk_time_text = "22h"
    context.pk_is_recurring = True
    context.pk_recurrence_text = "de segunda a sexta"


@given("an extracted event recurring on two named weekdays")
def step_given_weekday_list_recurrence(context):
    context.pk_date_text = None
    context.pk_time_text = "22h"
    context.pk_is_recurring = True
    context.pk_recurrence_text = "sextas e sábados"


@given("an extracted event whose recurrence phrase cannot be parsed")
def step_given_unparseable_recurrence(context):
    context.pk_date_text = None
    context.pk_time_text = "22h"
    context.pk_is_recurring = True
    context.pk_recurrence_text = "semanalmente"


@given("an extracted event naming a weekday that is not recurring")
def step_given_one_off_weekday_not_recurring(context):
    context.pk_date_text = "sábado"
    context.pk_time_text = "22h"
    context.pk_is_recurring = False
    context.pk_recurrence_text = None


@when("its date is resolved")
def step_when_its_date_is_resolved(context):
    context.pk_resolved = resolve_event_datetime(
        date_text=context.pk_date_text, time_text=context.pk_time_text,
        post_timestamp=_PK_ANCHOR,
        is_recurring=context.pk_is_recurring, recurrence_text=context.pk_recurrence_text,
    )


@then("it resolves to the next matching day")
def step_then_resolves_to_next_matching_day(context):
    assert context.pk_resolved.starts_at is not None, context.pk_resolved


@then("it is not flagged as missing a date")
def step_then_not_flagged_missing_date(context):
    assert context.pk_resolved.needs_review is False, context.pk_resolved


@then("it has no date")
def step_then_has_no_date(context):
    assert context.pk_resolved.starts_at is None, context.pk_resolved


@then("it is flagged as missing a date")
def step_then_flagged_missing_date(context):
    assert context.pk_resolved.needs_review is True, context.pk_resolved
    assert context.pk_resolved.review_reason == REASON_MISSING_DATE, context.pk_resolved


@then("the recurrence reading is not applied")
def step_then_recurrence_reading_not_applied(context):
    assert context.pk_resolved.is_recurring is False, context.pk_resolved
