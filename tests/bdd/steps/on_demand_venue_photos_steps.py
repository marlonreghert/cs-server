"""Behave steps for tests/bdd/api/on-demand-venue-photos.feature.

On-demand venue photo resolution: cs-server resolves a single venue's Google
Places photos on demand and caches FRESH, KEYLESS CDN URLs under a short-TTL
Redis key it alone writes, serving them through an internal resolve endpoint and
degrading to an empty list without ever caching or serving a stale/dead/
key-bearing URL.

Google is faked deterministically with an httpx.MockTransport injected into the
real GooglePlacesAPIClient, so the REAL keyless mechanism runs end-to-end (Place
Details -> per-photo media call with skipHttpRedirect + X-Goog-Api-Key header ->
keyless photoUri). No live network calls.
"""
from __future__ import annotations

import json

import httpx
from behave import given, when, then  # type: ignore[import-untyped]

from app.config import settings
from app.models.vibe_attributes import VibeAttributes

FRESH_KEY = "venue_photos_fresh_v1:{}"
LEGACY_KEY = "venue_photos_v1:{}"


# ── helpers ───────────────────────────────────────────────────────────────────
def _override_setting(context, name, value):
    """Set a global setting for the scenario, remembering the original so
    environment.after_scenario can restore it (no cross-scenario leakage)."""
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)


def _install_google_transport(context, photos_spec, *, raise_error=False, forbid=False):
    """Point the venue's GooglePlacesAPIClient at a deterministic MockTransport.

    photos_spec: list of {"name": <photo resource name>, "author": <str|None>}.
    raise_error: the Place Details call returns 500 (a resolution failure).
    forbid: any call fails the test (proves Google is never hit, e.g. no place_id).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if forbid:
            raise AssertionError(f"Google must not be called, but was: {request.url}")
        # The API key must travel in the header only — never in the URL/query.
        assert "key" not in request.url.params, f"API key leaked into URL: {request.url}"
        assert request.headers.get("X-Goog-Api-Key"), "missing X-Goog-Api-Key header"
        path = request.url.path
        if path.endswith("/media"):
            # Keyless media endpoint: must request skipHttpRedirect and return a
            # bare googleusercontent.com photoUri (no key).
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


def _photos_spec(n):
    return [{"name": f"places/CJpid/photos/p{i}", "author": f"Author {i}"} for i in range(n)]


def _fresh_raw(context):
    return context.fake_redis.get(FRESH_KEY.format(context.venue_id))


def _fresh_list(context):
    raw = _fresh_raw(context)
    return None if raw is None else json.loads(raw)


def _resolve(context):
    return context.client.post(
        f"/internal/venues/{context.venue_id}/photos/resolve"
    )


# ── Background ────────────────────────────────────────────────────────────────
@given("the internal photo resolve endpoint is available")
def step_endpoint_available(context):
    # The endpoint is mounted by the harness when the internal router exists.
    context.venue_id = "ven_photo_1"


@given(
    "Google photo resolution returns keyless googleusercontent.com URLs via the "
    "media endpoint with skipHttpRedirect"
)
def step_google_keyless_contract(context):
    pass  # Documented contract; enforced by _install_google_transport asserts.


@given('the fresh photo cache key for a venue is "venue_photos_fresh_v1:{{venue_id}}"')
def step_fresh_key_format(context):
    context.fresh_key_format = FRESH_KEY


@given('the legacy photo cache key for a venue is "venue_photos_v1:{{venue_id}}"')
def step_legacy_key_format(context):
    context.legacy_key_format = LEGACY_KEY


@given(
    'the fresh photo cache time-to-live is driven by "photo_fresh_cache_ttl_hours" '
    "with a default of 6 hours"
)
def step_fresh_ttl_contract(context):
    pass


@given('at most "photos_per_venue" photos are resolved per venue')
def step_cap_contract(context):
    pass


# ── Given ─────────────────────────────────────────────────────────────────────
@given("a venue with a stored google_place_id")
def step_venue_with_place_id(context):
    context.venue_id = "ven_photo_1"
    context.repository.set_vibe_attributes(
        VibeAttributes(venue_id=context.venue_id, google_place_id="places/CJpid")
    )


@given("a venue with no stored google_place_id")
def step_venue_without_place_id(context):
    context.venue_id = "ven_no_pid"
    # No vibe attributes written -> no google_place_id anywhere. Any Google call
    # would be a bug, so forbid it.
    _install_google_transport(context, [], forbid=True)


@given('"photos_per_venue" is 5')
def step_photos_per_venue_is_5(context):
    _override_setting(context, "photos_per_venue", 5)


@given('"photo_fresh_cache_ttl_hours" is 6')
def step_fresh_ttl_is_6(context):
    _override_setting(context, "photo_fresh_cache_ttl_hours", 6)


@given("Google returns {count:d} photos for that place")
def step_google_returns_n(context, count):
    _install_google_transport(context, _photos_spec(count))


@given("Google returns no photos for that place")
def step_google_returns_none(context):
    _install_google_transport(context, [])


@given(
    'Google returns a photo with an author attribution "{author}" and a photo '
    "with no attribution"
)
def step_google_author_mix(context, author):
    _install_google_transport(
        context,
        [
            {"name": "places/CJpid/photos/pa", "author": author},
            {"name": "places/CJpid/photos/pb", "author": None},
        ],
    )


@given("Google photo resolution raises an error")
def step_google_raises(context):
    _install_google_transport(context, [], raise_error=True)


@given("a previous resolve attempt failed and cached no url-bearing entry")
def step_previous_failure(context):
    _install_google_transport(context, [], raise_error=True)
    _resolve(context)  # degrades to empty and caches nothing
    data = _fresh_list(context)
    assert not data, f"a failed attempt must not cache a url-bearing entry, got {data}"


@given("Google now returns {count:d} photos for that place")
def step_google_now_returns_n(context, count):
    _install_google_transport(context, _photos_spec(count))


# ── When ──────────────────────────────────────────────────────────────────────
@when("the internal resolve endpoint is called for the venue")
def step_call_resolve(context):
    context.response = _resolve(context)


@when('the "photos" admin enrichment job is triggered')
def step_trigger_photos_job(context):
    context.response = context.client.post("/admin/trigger/photos")


# ── Then ──────────────────────────────────────────────────────────────────────
# NOTE: `the response status is {N:d}` is a shared generic step (defined in
# future_time_forecast_steps / user_activity_tracking_steps); it asserts on
# context.response, which the When steps here set. Reusing it avoids an
# AmbiguousStep at behave's global step registration.
@then('the response body contains a "venue_photos" list of {count:d} items')
@then('the response body contains a "venue_photos" list of exactly {count:d} items')
def step_body_list_count(context, count):
    body = context.response.json()
    assert "venue_photos" in body, f"missing venue_photos: {body}"
    assert len(body["venue_photos"]) == count, (
        f"expected {count} items, got {len(body['venue_photos'])}: {body['venue_photos']}"
    )


@then('the response body contains an empty "venue_photos" list')
def step_body_empty(context):
    body = context.response.json()
    assert body.get("venue_photos") == [], f"expected empty list, got {body}"


@then('each item has a "url" and an "author_name"')
def step_items_have_keys(context):
    for item in context.response.json()["venue_photos"]:
        assert "url" in item and "author_name" in item, f"item missing keys: {item}"


@then('every "url" is a keyless googleusercontent.com URL with no "key" query parameter')
def step_urls_keyless(context):
    for item in context.response.json()["venue_photos"]:
        url = item["url"]
        assert "googleusercontent.com" in url, f"not a googleusercontent URL: {url}"
        assert "key=" not in url and "key%3D" not in url, f"url carries a key param: {url}"


@then('no "url" is a places.googleapis.com media URL')
def step_urls_not_media(context):
    for item in context.response.json()["venue_photos"]:
        url = item["url"]
        assert "places.googleapis.com" not in url and "/media" not in url, (
            f"url is a places.googleapis.com media URL: {url}"
        )


@then("the fresh photo cache for the venue holds the same {count:d} items")
def step_cache_holds_same(context, count):
    cached = _fresh_list(context)
    assert cached is not None, "fresh cache not written"
    assert len(cached) == count, f"expected {count} cached, got {len(cached)}: {cached}"
    assert cached == context.response.json()["venue_photos"], (
        f"cache {cached} != response {context.response.json()['venue_photos']}"
    )


@then("the fresh photo cache for the venue holds exactly {count:d} items")
def step_cache_exact(context, count):
    cached = _fresh_list(context)
    assert cached is not None and len(cached) == count, (
        f"expected {count} cached, got {cached}"
    )


@then('one returned item has "author_name" equal to "{author}"')
def step_item_author(context, author):
    authors = [i["author_name"] for i in context.response.json()["venue_photos"]]
    assert author in authors, f"expected author {author!r} in {authors}"


@then('the item without attribution has a null "author_name"')
def step_item_null_author(context):
    authors = [i["author_name"] for i in context.response.json()["venue_photos"]]
    assert None in authors, f"expected a null author_name in {authors}"


@then("the fresh photo cache for the venue has a positive time-to-live")
def step_cache_positive_ttl(context):
    ttl = context.fake_redis.ttl(FRESH_KEY.format(context.venue_id))
    assert ttl is not None and ttl > 0, f"expected positive TTL, got {ttl}"


@then("the fresh photo cache time-to-live is at most {hours:d} hours")
def step_cache_ttl_max(context, hours):
    ttl = context.fake_redis.ttl(FRESH_KEY.format(context.venue_id))
    assert ttl is not None and 0 < ttl <= hours * 3600, (
        f"expected 0 < TTL <= {hours}h ({hours * 3600}s), got {ttl}"
    )


@then("the fresh photo cache for the venue is written")
def step_cache_written(context):
    assert _fresh_raw(context) is not None, "fresh cache key not written"


@then("the legacy photo cache for the venue is not written by the resolve path")
def step_legacy_not_written(context):
    assert context.fake_redis.get(LEGACY_KEY.format(context.venue_id)) is None, (
        "resolve path must not write the legacy venue_photos_v1 key"
    )


@then("no url-bearing entry is written to the fresh photo cache")
def step_no_url_bearing_entry(context):
    data = _fresh_list(context)
    if data is None:
        return  # nothing cached at all (exception path)
    assert all(not (isinstance(x, dict) and x.get("url")) for x in data), (
        f"a url-bearing entry was cached: {data}"
    )


@then("the fresh photo cache for the venue holds an empty list")
def step_cache_empty_list(context):
    assert _fresh_list(context) == [], f"expected an empty cached list, got {_fresh_list(context)}"


@then("the response indicates an unknown job")
def step_unknown_job(context):
    detail = context.response.json().get("detail", "")
    assert "Unknown job" in detail, f"expected 'Unknown job', got {detail!r}"
