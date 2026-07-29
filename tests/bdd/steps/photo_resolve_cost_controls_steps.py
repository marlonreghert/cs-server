"""Behave steps for tests/bdd/enrichment/photo-resolve-cost-controls.feature.

Cost controls layered onto POST /internal/venues/{id}/photos/resolve:
`max_photos` (buy only what will be displayed), `force` (the dead-URL repair
path), the partial-cache upgrade rule, and the 24h fresh-cache TTL default.

Reuses the pure data/state helpers from on_demand_venue_photos_steps
(_photos_spec, FRESH_KEY) but installs its OWN Google mock transport, because
these scenarios assert on the exact NUMBER of billed media calls made per
resolve — a dimension the shared installer does not track — and on
Prometheus counters (VENUE_PHOTO_RESOLVE_TOTAL result labels,
VENUE_PHOTOS_FETCHED_TOTAL) that persist for the whole `behave` process, so
every metric assertion here is a before/after delta captured around the call.
"""
from __future__ import annotations

import json

import httpx
from behave import given, when, then  # type: ignore[import-untyped]

from app.config import settings
from app.dao.redis_venue_dao import ADMIN_CONFIG_FRESH_PHOTOS_TTL_KEY
from app.metrics import VENUE_PHOTO_RESOLVE_TOTAL, VENUE_PHOTOS_FETCHED_TOTAL
from app.models.vibe_attributes import VibeAttributes

from tests.bdd.steps.on_demand_venue_photos_steps import FRESH_KEY, _photos_spec


# ── helpers ───────────────────────────────────────────────────────────────────
def _metric_value(counter, **labels):
    m = counter.labels(**labels) if labels else counter
    return m._value.get()


def _fresh_list_for(context, venue_id):
    raw = context.fake_redis.get(FRESH_KEY.format(venue_id))
    return None if raw is None else json.loads(raw)


def _install_counting_transport(context, photos_spec, *, raise_error=False, forbid=False):
    """Point the venue's GooglePlacesAPIClient at a deterministic MockTransport
    that also COUNTS media calls on context.media_call_count, reset to 0 on
    every install. Mirrors on_demand_venue_photos_steps._install_google_transport
    (same assertions: API key header-only, skipHttpRedirect on the media call)
    plus the call counter these cost-control scenarios need.
    """
    context.media_call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        if forbid:
            raise AssertionError(f"Google must not be called, but was: {request.url}")
        assert "key" not in request.url.params, f"API key leaked into URL: {request.url}"
        assert request.headers.get("X-Goog-Api-Key"), "missing X-Goog-Api-Key header"
        path = request.url.path
        if path.endswith("/media"):
            context.media_call_count += 1
            assert request.url.params.get("skipHttpRedirect") == "true", (
                f"media call missing skipHttpRedirect=true: {request.url}"
            )
            photo_name = path[len("/v1/"):-len("/media")]
            token = photo_name.replace("/", "_")
            return httpx.Response(
                200,
                json={
                    "name": photo_name,
                    "photoUri": f"https://lh3.googleusercontent.com/{token}=w800",
                },
            )
        # Otherwise it is the Place Details (photos field mask) call.
        if raise_error:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        photos = []
        for spec in photos_spec:
            entry = {"name": spec["name"]}
            if spec.get("author") is not None:
                entry["authorAttributions"] = [{"displayName": spec["author"]}]
            photos.append(entry)
        return httpx.Response(200, json={"photos": photos})

    context.google_places_client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5.0
    )


def _seed_cache(context, venue_id, n):
    """Write n synthetic, already-resolved photos straight into the fresh
    cache, bypassing Google — simulates pre-existing cache state. The url
    scheme ("seeded_...") is deliberately distinguishable from anything the
    counting transport resolves ("...googleusercontent.com/places_CJpid...")
    so a later assertion can tell a freshly-resolved list apart from a
    surviving seeded one."""
    photos = [
        {"url": f"https://lh3.googleusercontent.com/seeded_{venue_id}_{i}", "author_name": None}
        for i in range(n)
    ]
    context.photo_enrichment_service.venue_dao.set_venue_photos_fresh(venue_id, photos)
    context.seeded_photos = photos
    return photos


def _resolve(context, venue_id, *, max_photos=None, force=False):
    context.venue_id = venue_id
    # Snapshot BEFORE the call: these Prometheus counters persist for the
    # whole behave process (accumulate across scenarios/features), so every
    # Then-step below asserts a DELTA, never an absolute value.
    context._fetched_before = _metric_value(VENUE_PHOTOS_FETCHED_TOTAL)
    context._cache_hit_before = _metric_value(VENUE_PHOTO_RESOLVE_TOTAL, result="cache_hit")
    context._upgraded_before = _metric_value(VENUE_PHOTO_RESOLVE_TOTAL, result="upgraded")
    params = {}
    if max_photos is not None:
        params["max_photos"] = max_photos
    if force:
        params["force"] = "true"
    context.response = context.client.post(
        f"/internal/venues/{venue_id}/photos/resolve", params=params
    )
    return context.response


# ── Background ────────────────────────────────────────────────────────────────
@given("the Google Places photo service is configured")
def step_google_configured(context):
    # A generous default pool: enough for any max_photos this feature ever
    # requests (bounded by settings.photos_per_venue). Scenarios that need a
    # specific Google behaviour (failure, a different url set, forbidding any
    # call) install their own transport afterward, overriding this default.
    _install_counting_transport(context, _photos_spec(settings.photos_per_venue))


@given('the venue "{venue_id}" has a stored google_place_id')
def step_venue_has_place_id(context, venue_id):
    context.venue_id = venue_id
    context.repository.set_vibe_attributes(
        VibeAttributes(venue_id=venue_id, google_place_id="places/CJpid")
    )


@given('the venue "{venue_id}" has no stored google_place_id')
def step_venue_no_place_id(context, venue_id):
    context.venue_id = venue_id
    # Absence is the natural state for an unseeded id. Forbid any Google call
    # to prove the no-place-id short-circuit never reaches the network.
    _install_counting_transport(context, [], forbid=True)


# ── Given: cache state ───────────────────────────────────────────────────────
@given('no photos are cached for "{venue_id}"')
def step_none_cached(context, venue_id):
    context.venue_id = venue_id
    # A fresh fakeredis instance per scenario already has no key.


@given('{n:d} photo is cached for "{venue_id}"')
@given('{n:d} photos are cached for "{venue_id}"')
def step_n_cached(context, n, venue_id):
    context.venue_id = venue_id
    _seed_cache(context, venue_id, n)


@given('an empty photo list is cached for "{venue_id}"')
def step_empty_cached(context, venue_id):
    context.venue_id = venue_id
    context.photo_enrichment_service.venue_dao.set_venue_photos_fresh(venue_id, [])
    context.seeded_photos = []


# ── Given: Google behaviour overrides ────────────────────────────────────────
@given("the Google photo details call fails")
def step_google_details_fails(context):
    _install_counting_transport(context, [], raise_error=True)


@given('Google returns different photo urls for "{venue_id}"')
def step_google_returns_different(context, venue_id):
    _install_counting_transport(context, _photos_spec(5))


# ── Given: Redis/admin state ─────────────────────────────────────────────────
@given("the photo cache cannot be read")
def step_cache_read_fails(context):
    def _raise(*_a, **_k):
        raise RuntimeError("simulated Redis read failure")

    context.photo_enrichment_service.venue_dao.get_venue_photos_fresh = _raise


@given("no admin override for the fresh photo cache TTL")
def step_no_ttl_override(context):
    context.fake_redis.delete(ADMIN_CONFIG_FRESH_PHOTOS_TTL_KEY)


@given("the admin fresh photo cache TTL is set to {hours:d} hours")
def step_ttl_override_set(context, hours):
    context.fake_redis.set(ADMIN_CONFIG_FRESH_PHOTOS_TTL_KEY, json.dumps(hours))


# ── When ──────────────────────────────────────────────────────────────────────
@when('photos are resolved for "{venue_id}" with max_photos {max_photos:d}')
def step_resolve_with_max_photos(context, venue_id, max_photos):
    _resolve(context, venue_id, max_photos=max_photos)


@when('photos are resolved for "{venue_id}" without max_photos')
def step_resolve_without_max_photos(context, venue_id):
    _resolve(context, venue_id)


@when(
    'photos are resolved for "{venue_id}" with max_photos {max_photos:d} '
    "forcing a re-resolve"
)
def step_resolve_with_max_photos_forced(context, venue_id, max_photos):
    _resolve(context, venue_id, max_photos=max_photos, force=True)


@when('photos are resolved for "{venue_id}" forcing a re-resolve')
def step_resolve_forced(context, venue_id):
    _resolve(context, venue_id, force=True)


@when("the photo pre-bake job is triggered through the admin trigger endpoint")
def step_trigger_pre_bake(context):
    context.response = context.client.post("/admin/trigger/photos")


# ── Then: media call counts ──────────────────────────────────────────────────
@then("exactly {n:d} photo media call must be made")
def step_exactly_n_calls(context, n):
    assert context.media_call_count == n, (
        f"expected exactly {n} media call(s), got {context.media_call_count}"
    )


@then("{n:d} photo media call must be made")
@then("{n:d} photo media calls must be made")
def step_n_calls(context, n):
    assert context.media_call_count == n, (
        f"expected {n} media call(s), got {context.media_call_count}"
    )


@then("no photo media call must be made")
def step_no_calls(context):
    assert context.media_call_count == 0, (
        f"expected no media calls, got {context.media_call_count}"
    )


@then("photo media calls must be made")
def step_at_least_one_call(context):
    assert context.media_call_count >= 1, "expected at least one media call, got none"


@then("the number of photo media calls must equal the configured photos per venue")
def step_calls_equal_configured(context):
    assert context.media_call_count == settings.photos_per_venue, (
        f"expected {settings.photos_per_venue} media calls, got {context.media_call_count}"
    )


# ── Then: response body ──────────────────────────────────────────────────────
@then("the response must contain {n:d} photo")
@then("the response must contain {n:d} photos")
def step_response_contains_n(context, n):
    body = context.response.json()
    photos = body.get("venue_photos", [])
    assert len(photos) == n, f"expected {n} photo(s), got {photos}"


@then("the response must contain no photos")
def step_response_contains_none(context):
    body = context.response.json()
    assert body.get("venue_photos") == [], f"expected no photos, got {body}"


# ── Then: cache contents ─────────────────────────────────────────────────────
@then('the cached photo list for "{venue_id}" must hold {n:d} entry')
@then('the cached photo list for "{venue_id}" must hold {n:d} entries')
def step_cache_holds_n(context, venue_id, n):
    cached = _fresh_list_for(context, venue_id)
    assert cached is not None and len(cached) == n, f"expected {n} cached, got {cached}"


@then('the cached photo list for "{venue_id}" must hold that many entries')
def step_cache_holds_configured_many(context, venue_id):
    cached = _fresh_list_for(context, venue_id)
    assert cached is not None and len(cached) == settings.photos_per_venue, (
        f"expected {settings.photos_per_venue} cached, got {cached}"
    )


@then('the previously cached photo list for "{venue_id}" must be unchanged')
def step_cache_unchanged(context, venue_id):
    cached = _fresh_list_for(context, venue_id)
    assert cached == context.seeded_photos, (
        f"cache was mutated despite a Google failure: "
        f"before={context.seeded_photos} after={cached}"
    )


@then('an empty photo list must be cached for "{venue_id}"')
def step_empty_list_cached(context, venue_id):
    cached = _fresh_list_for(context, venue_id)
    assert cached == [], f"expected an empty cached list, got {cached}"


@then('nothing must be cached for "{venue_id}"')
def step_nothing_cached(context, venue_id):
    assert context.fake_redis.get(FRESH_KEY.format(venue_id)) is None, (
        "expected no cache entry to be written"
    )


@then('the cached photo list for "{venue_id}" must hold the newly resolved urls')
def step_cache_holds_new_urls(context, venue_id):
    cached = _fresh_list_for(context, venue_id)
    assert cached is not None and len(cached) > 0, (
        f"expected the forced resolve to have written photos, got {cached}"
    )
    seeded_urls = {p["url"] for p in (context.seeded_photos or [])}
    cached_urls = {p["url"] for p in cached}
    assert cached_urls.isdisjoint(seeded_urls), (
        f"cache still holds the old seeded urls after a forced re-resolve: {cached}"
    )


# ── Then: TTL ─────────────────────────────────────────────────────────────────
@then('the cached photo list for "{venue_id}" must expire in {hours:d} hours')
def step_cache_ttl_hours(context, venue_id, hours):
    ttl = context.fake_redis.ttl(FRESH_KEY.format(venue_id))
    expected = hours * 3600
    # Tight window (not just an upper bound): a stale 6h default must fail
    # the 24h scenario, and vice versa for the admin-override scenario.
    assert expected - 10 <= ttl <= expected, (
        f"expected TTL within 10s of {expected}s ({hours}h), got {ttl}s"
    )


# ── Then: metrics ─────────────────────────────────────────────────────────────
@then("the photos-fetched counter must increment by {n:d}")
def step_fetched_counter_incremented(context, n):
    after = _metric_value(VENUE_PHOTOS_FETCHED_TOTAL)
    delta = after - context._fetched_before
    assert delta == n, f"expected VENUE_PHOTOS_FETCHED_TOTAL +{n}, got +{delta}"


@then("the resolve outcome must be recorded as a cache hit")
def step_outcome_cache_hit(context):
    after = _metric_value(VENUE_PHOTO_RESOLVE_TOTAL, result="cache_hit")
    delta = after - context._cache_hit_before
    assert delta == 1, f"expected result=cache_hit +1, got +{delta}"


@then("the resolve outcome must be recorded as an upgrade")
def step_outcome_upgraded(context):
    after = _metric_value(VENUE_PHOTO_RESOLVE_TOTAL, result="upgraded")
    delta = after - context._upgraded_before
    assert delta == 1, f"expected result=upgraded +1, got +{delta}"


# ── Then: HTTP status ─────────────────────────────────────────────────────────
@then("the request must not fail")
def step_request_not_failed(context):
    assert context.response.status_code == 200, (
        f"expected 200, got {context.response.status_code}: {context.response.text}"
    )


@then("the request must be rejected as invalid")
def step_rejected_invalid(context):
    assert context.response.status_code == 422, (
        f"expected 422, got {context.response.status_code}: {context.response.text}"
    )


@then("the request must be rejected as not found")
def step_rejected_not_found(context):
    assert context.response.status_code == 404, (
        f"expected 404, got {context.response.status_code}: {context.response.text}"
    )
