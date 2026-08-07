"""Behave steps for tests/bdd/enrichment/date-resolution-correctness.feature.

See plans/260807_date-resolution-correctness.md. The date-parsing scenarios
call app.services.event_date_resolver.resolve_event_datetime directly — this
module is pure (no I/O, no wall clock), so no fake HTTP/DAO harness is needed
for them, unlike every other enrichment feature file.

The two "extraction_failed" scenarios DO need the review-queue harness, and
deliberately reuse it rather than building a third copy:
  - `the review queue is requested` is registered by
    instagram_promoter_events_steps.py.
  - `that event is in the queue` is registered by
    review_queue_completeness_steps.py.
Redefining either here would raise behave's AmbiguousStep. This file calls
those modules' own helpers (`_insert_event`, `_reset_context`,
`_build_admin_events_app`) instead, the same cross-file reuse pattern
review_queue_completeness_steps.py itself documents using.

Two Then steps are worded slightly off the feature file's most obvious phrasing
("the resolved event date is ..." / "the resolved event is marked recurring")
specifically to avoid AmbiguousStep against instagram_event_extraction_steps.py,
which already owns the literal "the event starts on 15 August 2026" /
"the event starts on 5 August" / "the event is marked recurring" steps for its
own (differently-shaped) scenarios. Renaming here, not weakening either
assertion, is the fix — see .claude/skills/execute-feature's collision
guidance.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from behave import given, then, when  # type: ignore[import-untyped]

import tests.bdd.steps.instagram_promoter_events_steps as ipe
from app.services.event_date_resolver import resolve_event_datetime
from app.services.event_extraction_service import (
    REVIEW_REASON_EXTRACTION_FAILED,
    STATUS_EXTRACTION_FAILED,
)
from tests.bdd.steps.review_queue_completeness_steps import _insert_event

RECIFE = ZoneInfo("America/Recife")

_MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _post_timestamp(month_name: str, year: int) -> datetime:
    """One fixed, deterministic anchor per month: day 3, 20:00 Recife time.
    Not day 1 — the disagreement/corroboration scenarios need an anchor whose
    own weekday differs from the day the flyer names, and day 3 of both
    August and January 2026 does, reproducing the plan's own measured
    evidence (the "Sábado • 05/SET" case's real pre-fix wrong answer,
    2026-08-08, only comes from a Monday anchor — 2026-08-03 is one)."""
    return datetime(year, _MONTH_NUMBERS[month_name.lower()], 3, 20, 0, tzinfo=RECIFE)


# ── Given ─────────────────────────────────────────────────────────────────
@given('a flyer date of "{date_text}" on a post from {month} {year:d}')
def step_given_flyer_date_on_post(context, date_text, month, year):
    context.edr_date_text = date_text
    context.edr_post_ts = _post_timestamp(month, year)


@given("a flyer date whose weekday does not fall on its explicit date")
def step_given_mismatched_weekday_and_date(context):
    # 5 September 2026 is a Saturday (pinned by the plan's own "Sábado •
    # 05/SET" evidence, corroborated in an earlier scenario in this same
    # feature). Stating "Domingo" (Sunday) beside that same explicit date is
    # a genuine disagreement, not an artifact of anchor choice.
    context.edr_date_text = "Domingo • 05/SET"
    context.edr_post_ts = _post_timestamp("august", 2026)
    context.edr_expected_explicit_date = date(2026, 9, 5)


@given('a caption mentioning "{word}" with no day number beside it')
def step_given_caption_mentioning_word_no_day(context, word):
    context.edr_date_text = (
        f"Em breve soltamos os detalhes completos, aguardem {word} chegar por aqui"
    )
    context.edr_post_ts = _post_timestamp("august", 2026)


@given("a venue-post event whose extraction failed")
def step_given_venue_post_extraction_failed(context):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(
        context, source_kind="venue_post", status=STATUS_EXTRACTION_FAILED,
        review_reason=REVIEW_REASON_EXTRACTION_FAILED,
    )
    ipe._build_admin_events_app(context)


@given("a promoter-post event whose extraction failed")
def step_given_promoter_post_extraction_failed(context):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(
        context, source_kind="promoter_post", status=STATUS_EXTRACTION_FAILED,
        location_resolution=None, review_reason=REVIEW_REASON_EXTRACTION_FAILED,
    )
    ipe._build_admin_events_app(context)


# ── When ──────────────────────────────────────────────────────────────────
@when("the event date is resolved")
def step_when_the_event_date_is_resolved(context):
    context.edr_resolved = resolve_event_datetime(
        date_text=context.edr_date_text, time_text=None, post_timestamp=context.edr_post_ts,
    )


# ── Then ──────────────────────────────────────────────────────────────────
@then("the resolved event date is {day:d} {month} {year:d}")
def step_then_event_starts_on(context, day, month, year):
    resolved = context.edr_resolved
    assert resolved.starts_at is not None, resolved
    expected = date(year, _MONTH_NUMBERS[month.lower()], day)
    assert resolved.starts_at.date() == expected, resolved.starts_at


@then("the event is not dated from the weekday alone")
def step_then_not_dated_from_weekday_alone(context):
    resolved = context.edr_resolved
    assert resolved.needs_review is False, resolved
    assert resolved.review_reason is None, resolved


@then("the event has no start date")
def step_then_event_has_no_start_date(context):
    assert context.edr_resolved.starts_at is None, context.edr_resolved


@then("the event is flagged for review")
def step_then_event_is_flagged_for_review(context):
    assert context.edr_resolved.needs_review is True, context.edr_resolved


@then("the event starts on the Saturday after the post")
def step_then_starts_on_saturday_after_post(context):
    resolved = context.edr_resolved
    assert resolved.starts_at is not None, resolved
    assert resolved.starts_at.date().weekday() == 5, resolved.starts_at
    assert resolved.starts_at.date() >= context.edr_post_ts.date(), resolved.starts_at


@then("the resolved event is marked recurring")
def step_then_event_marked_recurring(context):
    assert context.edr_resolved.is_recurring is True, context.edr_resolved


@then("the event starts on the Thursday after the post")
def step_then_starts_on_thursday_after_post(context):
    resolved = context.edr_resolved
    assert resolved.starts_at is not None, resolved
    assert resolved.starts_at.date().weekday() == 3, resolved.starts_at
    assert resolved.starts_at.date() >= context.edr_post_ts.date(), resolved.starts_at


@then("the event starts on the explicit date")
def step_then_starts_on_explicit_date(context):
    resolved = context.edr_resolved
    assert resolved.starts_at is not None, resolved
    assert resolved.starts_at.date() == context.edr_expected_explicit_date, resolved.starts_at


@then('the event is flagged with the reason "{reason}"')
def step_then_flagged_with_reason(context, reason):
    resolved = context.edr_resolved
    assert resolved.needs_review is True, resolved
    assert resolved.review_reason == reason, resolved.review_reason
