"""Behave steps for tests/bdd/api/geofence-city-circles.feature.

The geo-fence becomes a set of state-capital circles (capital + radius km)
managed through the real admin endpoints (context.client), enforced only in the
serving view (context.rds_store.list_servable_venue_ids, the fake mirror of
serving.eligible_venue) and projected by context.redis_projection_service —
fail-open, reversible, never soft-deleting.

Shared-step contract: "the write succeeds" / "the write is rejected as invalid"
are owned by force_update_config_validator_steps.py and assert on
context.set_error (None on success, ValueError on a 400 rejection). The PUT
helper here translates the HTTP outcome into that contract so both features
share one vocabulary without AmbiguousStep collisions.

The RDS layer (repository / rds_store / redis_projection_service /
redis_only_dao) is built per scenario in environment.py.
"""
from __future__ import annotations

import json

from behave import given, then, when  # type: ignore[import-untyped]

from app.models import Venue

# Expected catalog coordinates for the capitals exercised in this feature.
# These PIN the server-owned catalog contract — assertions compare the coords
# the API resolves against these; they are never sent as inputs.
_CENTERS = {
    "recife": (-8.0476, -34.8770),
    "salvador": (-12.9714, -38.5014),
}

# Redis mirror of the fence (GET shape), written best-effort by the PUT.
_MIRROR_KEY = "admin_config:venue_geofence"

# Haversine km per degree of latitude at R=6371.0088 (the predicate's radius):
# offsetting latitude by d° moves the venue d*111.195 km from the center.
_KM_PER_DEG_LAT = 111.195


# ── helpers ───────────────────────────────────────────────────────────────────
def _get_fence(context):
    return context.client.get("/admin/config/geofence")


def _put_fence(context, payload: dict):
    """PUT the fence and translate the outcome into the shared set_error
    contract: 200 → None, 400 → ValueError, anything else → RuntimeError (so
    both shared then-steps fail loudly on an unexpected status)."""
    resp = context.client.put("/admin/config/geofence", json=payload)
    context.put_resp = resp
    if resp.status_code == 200:
        context.set_error = None
    elif resp.status_code == 400:
        context.set_error = ValueError(resp.text)
    else:
        context.set_error = RuntimeError(
            f"unexpected geo-fence PUT status {resp.status_code}: {resp.text}"
        )
    return resp


def _cities(*pairs) -> list[dict]:
    return [{"slug": slug, "radius_km": radius} for slug, radius in pairs]


def _seed(context, vid, name, lat, lng):
    # The Venue model requires float coords, but venues.address.lat/lng can be
    # NULL. To seed a coord-less venue, upsert placeholder coords then null the
    # address row (mirrors the real LEFT JOIN yielding NULL lat/lng).
    context.repository.upsert_venue(
        Venue(
            forecast=True, processed=True,
            venue_id=vid, venue_name=name,
            venue_address=f"{vid} address",
            venue_lat=lat if lat is not None else 0.0,
            venue_lng=lng if lng is not None else 0.0,
            venue_type="BAR",
        )
    )
    if lat is None or lng is None:
        addr = context.rds_store.addresses.get(vid)
        if addr is not None:
            addr["lat"] = lat
            addr["lng"] = lng
    return vid


def _fence_cities(resp) -> list[dict]:
    assert resp.status_code == 200, f"fence read failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert "cities" in body, f"fence response has no cities list: {body}"
    return body["cities"]


def _assert_circle(city: dict, slug: str, radius_km: float):
    lat, lng = _CENTERS[slug]
    assert city["slug"] == slug, city
    assert city.get("name"), f"city {slug} has no name: {city}"
    assert abs(city["lat"] - lat) < 1e-6, f"{slug} lat {city['lat']} != catalog {lat}"
    assert abs(city["lng"] - lng) < 1e-6, f"{slug} lng {city['lng']} != catalog {lng}"
    assert city["radius_km"] == radius_km, city


# ── Background ────────────────────────────────────────────────────────────────
@given('the admin geo-fence is enabled with the city "{slug}" at radius {radius:d} km')
def step_fence_enabled_with(context, slug, radius):
    resp = _put_fence(context, {"enabled": True, "cities": _cities((slug, radius))})
    assert resp.status_code == 200, (
        f"seeding the fence with {slug}@{radius}km must succeed, "
        f"got {resp.status_code}: {resp.text}"
    )


# ── capitals catalog ──────────────────────────────────────────────────────────
@when("the admin requests the geo-fence capitals catalog")
def step_request_catalog(context):
    context.catalog_resp = context.client.get("/admin/config/geofence/capitals")


@then("the response lists 27 capitals sorted by name")
def step_catalog_27_sorted(context):
    resp = context.catalog_resp
    assert resp.status_code == 200, f"catalog GET failed: {resp.status_code} {resp.text}"
    capitals = resp.json()["capitals"]
    assert len(capitals) == 27, f"expected 27 capitals, got {len(capitals)}"
    names = [c["name"] for c in capitals]
    assert names == sorted(names), f"capitals not sorted by name: {names}"


@then("every capital has a unique slug, a name, and coordinates within valid ranges")
def step_catalog_integrity(context):
    capitals = context.catalog_resp.json()["capitals"]
    slugs = [c["slug"] for c in capitals]
    assert len(set(slugs)) == len(slugs), f"duplicate slugs: {slugs}"
    for c in capitals:
        assert c.get("slug") and c.get("name"), c
        assert -90.0 <= c["lat"] <= 90.0, c
        assert -180.0 <= c["lng"] <= 180.0, c


# ── read ──────────────────────────────────────────────────────────────────────
@when("the admin reads the geo-fence config")
def step_read_fence(context):
    context.read_resp = _get_fence(context)


@then("the response has enabled true")
def step_enabled_true(context):
    resp = context.read_resp
    assert resp.status_code == 200, f"fence GET failed: {resp.status_code} {resp.text}"
    assert resp.json().get("enabled") is True, resp.json()


@then('the response lists one city with slug "{slug}", its catalog coordinates, and radius_km {radius:d}')
def step_one_city(context, slug, radius):
    cities = _fence_cities(context.read_resp)
    assert len(cities) == 1, f"expected one city, got {cities}"
    _assert_circle(cities[0], slug, radius)


# ── writes ────────────────────────────────────────────────────────────────────
@when('the admin writes a geo-fence with cities "{a}" at {ra:d} km and "{b}" at {rb:d} km')
def step_write_two_cities(context, a, ra, b, rb):
    context.written = [(a, ra), (b, rb)]
    _put_fence(context, {"enabled": True, "cities": _cities((a, ra), (b, rb))})


@when('the admin writes a geo-fence with the city "{slug}" at {radius:d} km')
def step_write_one_city(context, slug, radius):
    _put_fence(context, {"enabled": True, "cities": _cities((slug, radius))})


@when('the admin writes a geo-fence listing the city "{slug}" twice')
def step_write_duplicate(context, slug):
    _put_fence(
        context, {"enabled": True, "cities": _cities((slug, 30), (slug, 40))}
    )


@when("the admin writes an enabled geo-fence with no cities")
def step_write_enabled_empty(context):
    _put_fence(context, {"enabled": True, "cities": []})


@when("the admin writes a disabled geo-fence with no cities")
def step_write_disabled_empty(context):
    _put_fence(context, {"enabled": False, "cities": []})


@when("the admin writes a geo-fence using min/max lat/lng box fields")
def step_write_legacy_box(context):
    _put_fence(context, {
        "min_lat": -8.30, "max_lat": -7.85,
        "min_lng": -35.10, "max_lng": -34.80,
        "enabled": True,
    })


@then("the response lists both cities with their catalog coordinates")
def step_response_both_cities(context):
    resp = context.put_resp
    assert resp.status_code == 200, f"PUT failed: {resp.status_code} {resp.text}"
    cities = {c["slug"]: c for c in resp.json()["cities"]}
    assert len(cities) == len(context.written), cities
    for slug, radius in context.written:
        assert slug in cities, f"{slug} missing from {cities}"
        _assert_circle(cities[slug], slug, radius)


@then("reading the geo-fence config returns the same two circles")
def step_read_matches_put(context):
    cities = _fence_cities(_get_fence(context))
    assert cities == context.put_resp.json()["cities"], (
        f"GET circles {cities} != PUT circles {context.put_resp.json()['cities']}"
    )


@then("the Redis geo-fence mirror holds the same two circles")
def step_mirror_matches(context):
    raw = context.fake_redis.get(_MIRROR_KEY)
    assert raw is not None, f"Redis mirror {_MIRROR_KEY} is absent"
    mirrored = json.loads(raw)
    assert mirrored.get("cities") == context.put_resp.json()["cities"], mirrored


@then('reading the geo-fence config still returns the city "{slug}" at radius_km {radius:d}')
def step_fence_unchanged(context, slug, radius):
    cities = _fence_cities(_get_fence(context))
    assert len(cities) == 1, f"fence changed: {cities}"
    _assert_circle(cities[0], slug, radius)


@then("reading the geo-fence config returns enabled false")
def step_enabled_false(context):
    resp = _get_fence(context)
    assert resp.status_code == 200, f"fence GET failed: {resp.status_code} {resp.text}"
    assert resp.json().get("enabled") is False, resp.json()


@then("the rejection message names the cities-based payload")
def step_rejection_names_cities(context):
    assert context.put_resp.status_code == 400, context.put_resp.status_code
    assert "cities" in context.put_resp.text.lower(), (
        f"rejection must point at the new cities shape: {context.put_resp.text}"
    )


# ── serving membership ────────────────────────────────────────────────────────
@given('the fence has cities "{a}" at {ra:d} km and "{b}" at {rb:d} km')
def step_fence_two_cities(context, a, ra, b, rb):
    resp = _put_fence(context, {"enabled": True, "cities": _cities((a, ra), (b, rb))})
    assert resp.status_code == 200, f"fence setup failed: {resp.status_code} {resp.text}"


@given('the fence has the city "{slug}" at {radius:d} km')
def step_fence_one_city(context, slug, radius):
    resp = _put_fence(context, {"enabled": True, "cities": _cities((slug, radius))})
    assert resp.status_code == 200, f"fence setup failed: {resp.status_code} {resp.text}"


@given('an active venue with coordinates {km:d} km from the "{slug}" center')
def step_venue_km_from_center(context, km, slug):
    lat, lng = _CENTERS[slug]
    context.subject_id = _seed(
        context, f"venue_{km}km_{slug}", f"Bar {km}km {slug}",
        lat + km / _KM_PER_DEG_LAT, lng,
    )
    context.subject_has_coords = True


@given("an active venue with no stored coordinates")
def step_venue_without_coords(context):
    context.subject_id = _seed(context, "nocoords_bar", "No Coords Bar", None, None)
    context.subject_has_coords = False


@when("the serving projection runs")
def step_projection_runs(context):
    context.projection_summary = context.redis_projection_service.rebuild_redis_from_rds()


@when("the admin disables the geo-fence")
def step_disable_fence(context):
    current = _get_fence(context)
    kept = [
        {"slug": c["slug"], "radius_km": c["radius_km"]}
        for c in _fence_cities(current)
    ]
    resp = _put_fence(context, {"enabled": False, "cities": kept})
    assert resp.status_code == 200, f"disable failed: {resp.status_code} {resp.text}"


@then("the venue is included in the serving set")
def step_venue_served(context):
    servable = set(context.rds_store.list_servable_venue_ids())
    assert context.subject_id in servable, (
        f"{context.subject_id} should be servable but is absent"
    )
    # The Redis projection materializes the serving view — assert end-to-end
    # presence for venues the Venue model can represent. A coord-less venue is
    # fail-open in the VIEW (what this fence pins) but unrepresentable in the
    # projection (Venue requires float lat/lng; real venues.address rows are
    # NOT NULL, so that branch is defensive-only).
    if getattr(context, "subject_has_coords", True):
        assert context.redis_only_dao.get_venue(context.subject_id) is not None, (
            f"{context.subject_id} should be in the Redis serving projection"
        )


@then("the venue is excluded from the serving set")
def step_venue_not_served(context):
    servable = set(context.rds_store.list_servable_venue_ids())
    assert context.subject_id not in servable, (
        f"{context.subject_id} should be geo-excluded but is servable"
    )
    assert context.redis_only_dao.get_venue(context.subject_id) is None, (
        f"{context.subject_id} should be absent from the Redis serving projection"
    )


@then("the venue remains active and is not soft-deleted")
def step_venue_still_active(context):
    row = context.rds_store.venues.get(context.subject_id)
    assert row is not None, f"{context.subject_id} vanished from the system of record"
    assert row.get("lifecycle_status", "active") == "active", (
        f"geo exclusion must never soft-delete: {row}"
    )
