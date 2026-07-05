"""Behave steps for tests/bdd/refresh/park-category-eligibility.feature.

Pure-function scenarios: category resolution (`resolve_category`,
`resolve_venue_display`) and eligibility (`evaluate`) take Google/BestTime
type + name directly and need no Redis/DB harness, so these steps call the
app modules straight rather than going through environment.py's FastAPI app.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.models.venue_category import resolve_category, resolve_venue_display
from app.services.venue_eligibility import evaluate


@given('a venue named "{name}" with Google type "{google_type}"')
def step_venue_with_google_type(context, name, google_type):
    context.venue_name = name
    context.google_type = google_type
    context.besttime_type = None


@given('a venue named "{name}" with BestTime type "{besttime_type}" and no Google type')
def step_venue_with_besttime_type(context, name, besttime_type):
    context.venue_name = name
    context.besttime_type = besttime_type
    context.google_type = None


@when("the venue is evaluated for category and eligibility")
def step_evaluate_category_and_eligibility(context):
    context.resolved_category = resolve_category(
        google_type=context.google_type,
        besttime_type=context.besttime_type,
        venue_name=context.venue_name,
    )
    context.eligibility_result = evaluate(
        context.venue_name,
        besttime_type=context.besttime_type,
        google_type=context.google_type,
    )


@when("the venue display is resolved")
def step_resolve_venue_display(context):
    context.venue_display = resolve_venue_display(
        google_type=context.google_type,
        besttime_type=context.besttime_type,
        venue_name=context.venue_name,
    )


@then('the resolved category must be "{category}"')
def step_check_resolved_category(context, category):
    assert context.resolved_category == category, (
        f"expected category {category!r}, got {context.resolved_category!r}"
    )


@then("the venue must be eligible for serving")
def step_check_eligible(context):
    assert context.eligibility_result.eligible, (
        f"expected eligible, got reason={context.eligibility_result.reason!r}"
    )


@then('the venue must be rejected with reason "{reason}"')
def step_check_rejected_with_reason(context, reason):
    assert not context.eligibility_result.eligible, "expected the venue to be rejected"
    assert context.eligibility_result.reason == reason, (
        f"expected reason {reason!r}, got {context.eligibility_result.reason!r}"
    )


@then('the venue must not be rejected with reason "{reason}"')
def step_check_not_rejected_with_reason(context, reason):
    assert context.eligibility_result.reason != reason, (
        f"expected reason to differ from {reason!r}, got {context.eligibility_result.reason!r}"
    )


@then('the venue type label must be "{label}"')
def step_check_label(context, label):
    assert context.venue_display["label"] == label, context.venue_display


@then('the venue type emoji must be "{emoji}"')
def step_check_emoji(context, emoji):
    assert context.venue_display["emoji"] == emoji, context.venue_display


@then('the venue type color must be "{color}"')
def step_check_color(context, color):
    assert context.venue_display["color"] == color, context.venue_display
