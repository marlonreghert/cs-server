"""Behave steps for
tests/bdd/enrichment/post-kind-and-post-extraction-attribution.feature.

See plans/260810_post-kind-and-post-extraction-attribution.md. Three
independent things this file drives, mirrored by the feature file's own
sections:

  - §A (what a post is — the missing/unrecognised-kind edge cases only): the
    REAL `EventExtractionService`, over the SAME `context.ee_*` harness
    `instagram_event_extraction_steps.py` already built — its Background
    step ("the event extraction pipeline is configured for a known venue",
    defined in event_ticket_info_and_attractions_steps.py) and its
    `_add_post`/`_run_extraction`/`_stored_event` helpers are reused
    verbatim, the same pattern every sibling extraction feature file in this
    suite already follows. plans/260811_post-items-and-categories.md
    retired the "a non-event produces NO row" behaviour this file used to
    assert (§A/§B originally) — that coverage moved to
    tests/bdd/enrichment/post-items-and-categories.feature. `_kind_label_
    total` below (outcome-agnostic: a menu item is persisted now, not
    dropped as "not_an_event") is imported by `date_correctness_and_path_
    parity_steps.py`'s "Count the kind split on the shared-handle path"
    scenario, which reuses the SAME "the extraction is counted as a menu
    item" Then step defined here.

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


def _kind_label_total(kind: str) -> float:
    """Sum of `event_extraction_posts_total{kind=<kind>}` across EVERY
    outcome. plans/260811_post-items-and-categories.md: an item's `kind` no
    longer determines a single fixed outcome (a menu item can now be
    "accepted", queued for a missing date, etc., exactly like an event can)
    — so a scenario that only cares "was this kind reported at all" must sum
    across outcomes rather than pin one."""
    total = 0.0
    for metric in REGISTRY.collect():
        # prometheus_client strips a declared name's trailing "_total" for
        # `metric.name` and re-appends it on the individual `sample.name` —
        # verified directly against a live Counter rather than assumed (see
        # `_counter_total` above, which gets this wrong for a "_total"-
        # suffixed declared name; not touched here, out of this plan's
        # scope).
        if metric.name != "event_extraction_posts":
            continue
        for sample in metric.samples:
            if sample.name == "event_extraction_posts_total" and (
                sample.labels.get("kind") == kind
            ):
                total += sample.value
    return total


# ── §A: the missing/unrecognised-kind edge cases ─────────────────────────
@given("a post announcing a DJ night on a stated date")
def step_given_dj_night(context):
    _ee_ensure_context(context)
    _add_post(context, "pk_event_post")
    context.ee_openai.program(_extraction_json(
        kind="event", title="Noite do DJ", date_text="15/08", time_text="22h",
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
    # plans/260811_post-items-and-categories.md: a menu-kind post is now
    # persisted (accepted, or queued for its own reasons) rather than
    # dropped as "not_an_event" — this only asserts the `kind` LABEL landed,
    # regardless of which outcome the item resolved to. Reused by
    # date_correctness_and_path_parity_steps.py's "Count the kind split on
    # the shared-handle path" scenario (see this module's docstring).
    after = _kind_label_total("menu")
    assert after - context.pk_kind_before == 1.0, (context.pk_kind_before, after)


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
