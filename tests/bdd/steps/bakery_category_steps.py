"""Behave steps for tests/bdd/api/bakery-category.feature.

Resolution scenarios drive resolve_venue_display directly (the serve-time seam).
The admin-map scenario reuses the category-map steps (save + subsequent GET),
which drive the real /admin/venues/category-map routes.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import venue_category as vc

CROISSANT = "\U0001F950"  # 🥐


@given('a venue whose google primary type is "{gtype}"')
def step_bakery_venue(context, gtype):
    context.bakery_google_type = gtype


@when("that venue's display is resolved")
def step_resolve_display(context):
    context.bakery_display = vc.resolve_venue_display(
        google_type=context.bakery_google_type
    )


@then('the resolved category is "{cat}"')
def step_resolved_category(context, cat):
    got = context.bakery_display["category"]
    assert got == cat, f"expected category {cat}, got {got}"


@then('the resolved label is "{label}"')
def step_resolved_label(context, label):
    got = context.bakery_display["label"]
    assert got == label, f"expected label {label}, got {got}"


@then("the resolved emoji is the croissant emoji")
def step_resolved_emoji(context):
    got = context.bakery_display["emoji"]
    assert got == CROISSANT, f"expected croissant emoji, got {got!r}"


@then('the resolved granular label is "{label}"')
def step_resolved_granular_label(context, label):
    got = context.bakery_display["granular_label"]
    assert got == label, f"expected granular label {label}, got {got}"
