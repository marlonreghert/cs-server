"""Behave steps for tests/bdd/api/admin-category-map.feature.

Admin endpoint scenarios drive the real GET/POST /admin/venues/category-map
routes through the TestClient (the harness wires the real AdminConfigService +
fakeredis + fake RDS store onto the shared container). Serving scenarios drive
the exact serve-time seam venue_handler uses per venue: load_category_map(redis)
+ resolve_venue_display(..., google_map, besttime_map) — proving a remap changes
the served category with no venue re-fetch.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import venue_category as vc
from app.services.admin_config_service import ADMIN_CONFIG_PREFIX

CATEGORY_MAP_KEY = "venue_category_map"
CATEGORY_MAP_REDIS_KEY = f"{ADMIN_CONFIG_PREFIX}{CATEGORY_MAP_KEY}"
BASE = "/admin/venues/category-map"


def _serve_category(context, google_type, besttime_type):
    """Resolve a venue's category the way venue_handler does at serve time:
    read the effective map from Redis, then resolve with it injected."""
    assert hasattr(vc, "load_category_map"), "load_category_map not yet implemented"
    google_map, besttime_map = vc.load_category_map(context.fake_redis)
    return vc.resolve_venue_display(
        google_type=google_type,
        besttime_type=besttime_type,
        google_map=google_map,
        besttime_map=besttime_map,
    )["category"]


# ── given ─────────────────────────────────────────────────────────────────────
@given("no venue category-map override is stored")
def step_no_override(context):
    try:
        context.admin_config_service.delete(CATEGORY_MAP_KEY)
    except Exception:
        pass
    context.fake_redis.delete(CATEGORY_MAP_REDIS_KEY)
    context.response = None


@given('a stored venue whose google primary type is "{gtype}" and besttime type is absent')
def step_stored_venue(context, gtype):
    context.serve_google_type = gtype
    context.serve_besttime_type = None


@given('the served category for that venue is "{cat}"')
def step_baseline_category(context, cat):
    got = vc.resolve_venue_display(
        google_type=context.serve_google_type,
        besttime_type=context.serve_besttime_type,
    )["category"]
    assert got == cat, f"baseline expected {cat}, got {got}"


@given("the venue category-map override is stored as malformed data")
def step_malformed_override(context):
    context.fake_redis.set(CATEGORY_MAP_REDIS_KEY, "not-json{{{")


# ── when ──────────────────────────────────────────────────────────────────────
@when("the operator requests GET /admin/venues/category-map")
def step_get_map(context):
    context.response = context.client.get(BASE)


@when('the operator saves a google type mapping of "{gtype}" to "{cat}"')
def step_post_google(context, gtype, cat):
    context.response = context.client.post(
        BASE, json={"google": {gtype: cat}, "besttime": {}}
    )


@when(
    'the operator saves both a google mapping "{gtype}" to "{gcat}" '
    'and a besttime mapping "{bttype}" to "{btcat}"'
)
def step_post_both(context, gtype, gcat, bttype, btcat):
    context.response = context.client.post(
        BASE, json={"google": {gtype: gcat}, "besttime": {bttype: btcat}}
    )


@when("nearby venues are served")
def step_served(context):
    context.served_category = _serve_category(
        context, context.serve_google_type, context.serve_besttime_type
    )


@when('nearby venues are served for a venue whose google primary type is "{gtype}"')
def step_served_for(context, gtype):
    context.serve_google_type = gtype
    context.serve_besttime_type = None
    context.served_category = _serve_category(context, gtype, None)


# ── then ──────────────────────────────────────────────────────────────────────
@then('the response maps google type "{gtype}" to category "{cat}"')
def step_resp_google(context, gtype, cat):
    body = context.response.json()
    got = body.get("google", {}).get(gtype)
    assert got == cat, f"expected google[{gtype}]={cat}, got {got}"


@then('the response maps besttime type "{bttype}" to category "{cat}"')
def step_resp_besttime(context, bttype, cat):
    body = context.response.json()
    got = body.get("besttime", {}).get(bttype)
    assert got == cat, f"expected besttime[{bttype}]={cat}, got {got}"


@then('a subsequent GET /admin/venues/category-map maps google type "{gtype}" to category "{cat}"')
def step_subsequent_google(context, gtype, cat):
    r = context.client.get(BASE)
    assert r.status_code == 200, r.text
    got = r.json().get("google", {}).get(gtype)
    assert got == cat, f"expected google[{gtype}]={cat}, got {got}"


@then('the subsequent GET still maps google type "{gtype}" to category "{cat}"')
def step_subsequent_still_google(context, gtype, cat):
    r = context.client.get(BASE)
    got = r.json().get("google", {}).get(gtype)
    assert got == cat, f"expected google[{gtype}]={cat}, got {got}"


@then('the subsequent GET maps besttime type "{bttype}" to category "{cat}"')
def step_subsequent_besttime(context, bttype, cat):
    r = context.client.get(BASE)
    got = r.json().get("besttime", {}).get(bttype)
    assert got == cat, f"expected besttime[{bttype}]={cat}, got {got}"


@then('a subsequent GET /admin/venues/category-map does not map google type "{gtype}"')
def step_subsequent_absent(context, gtype):
    r = context.client.get(BASE)
    assert gtype not in r.json().get("google", {}), f"{gtype} unexpectedly present"


@then('that venue is served with category "{cat}"')
def step_served_category(context, cat):
    got = getattr(context, "served_category", None)
    if got is None:
        got = _serve_category(
            context, context.serve_google_type, context.serve_besttime_type
        )
    assert got == cat, f"served category expected {cat}, got {got}"
