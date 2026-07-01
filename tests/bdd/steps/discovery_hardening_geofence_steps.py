"""Behave steps for tests/bdd/refresh/discovery-hardening-geofence.feature.

Three coupled behaviours, all driven through production surfaces:

  1. Startup runs no pipeline — exercises main.startup_background_pipelines with a
     mock container and every *_on_startup flag on; asserts no pipeline method is
     called.
  2. Discovery is dormant — the venue_catalog admin trigger is a 404 (removed from
     JOB_REGISTRY); discovery points admin config is empty by default.
  3. Recife-metro geo-fence — the RDS-fake serving view (context.rds_store, mirror
     of serving.eligible_venue) excludes out-of-box venues; the box is edited via
     the real PUT /admin/config/geofence endpoint (context.client) so the endpoint
     write-routing is exercised, not shortcut.

The RDS layer (context.repository / rds_store / redis_projection_service /
redis_only_dao) is built per scenario in environment.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import Venue

# Default Recife/Olinda box (confirmed): lat -8.30..-7.85, lng -35.10..-34.80.
_DEFAULT_BOX = {
    "min_lat": -8.30, "max_lat": -7.85,
    "min_lng": -35.10, "max_lng": -34.80,
    "enabled": True,
}
# A point well inside the box (central Recife).
_INSIDE_LAT, _INSIDE_LNG = -8.05, -34.88
# Olinda — inside the box, north of Recife.
_OLINDA_LAT, _OLINDA_LNG = -7.99, -34.85
# Well outside the box (e.g. São Paulo).
_OUTSIDE_LAT, _OUTSIDE_LNG = -23.55, -46.63


def _seed(context, vid, name, lat, lng, *, venue_type="BAR"):
    # The Venue model requires float coords, but venues.address.lat/lng can be NULL
    # in reality. To seed a coord-less venue we upsert with placeholder coords then
    # null the address row directly (mirrors a venue whose address row has no coords).
    context.repository.upsert_venue(
        Venue(
            forecast=True, processed=True,
            venue_id=vid, venue_name=name,
            venue_address=f"{vid} address",
            venue_lat=lat if lat is not None else 0.0,
            venue_lng=lng if lng is not None else 0.0,
            venue_type=venue_type,
        )
    )
    if lat is None or lng is None:
        addr = context.rds_store.addresses.get(vid)
        if addr is not None:
            addr["lat"] = None if lat is None else lat
            addr["lng"] = None if lng is None else lng
    return vid


def _servable(context) -> set[str]:
    return set(context.rds_store.list_servable_venue_ids())


def _put_geofence(context, box: dict):
    """Edit the box through the real admin endpoint (exercises write-routing)."""
    return context.client.put("/admin/config/geofence", json=box)


# ── Background ────────────────────────────────────────────────────────────────
@given("the venue platform is configured with the default Recife/Olinda geo-fence box")
def step_default_box(context):
    context.named_ids = {}
    # Seed the default box through the fake store's dedicated geo-fence method
    # (mirror of admin.geo_fence). Guarded so the Background does not crash before
    # the method exists — the geo assertions then fail on membership (true red),
    # not on a missing attribute.
    setter = getattr(context.rds_store, "set_geo_fence", None)
    if callable(setter):
        setter(dict(_DEFAULT_BOX))


# ══════════════════════════════════════════════════════════════════════════════
# 1 — No pipeline runs on startup
# ══════════════════════════════════════════════════════════════════════════════
@given('every "*_on_startup" flag is set to true')
def step_all_startup_flags(context):
    context.startup_settings = SimpleNamespace(
        refresh_on_startup=True,
        google_places_enrichment_on_startup=True,
        photo_enrichment_on_startup=True,
        instagram_enrichment_on_startup=True,
        ig_posts_enrichment_on_startup=True,
        menu_enrichment_on_startup=True,
        menu_extraction_on_startup=True,
        vibe_classifier_on_startup=True,
        google_places_api_key="test-key",
        apify_api_token="test-token",
        remove_permanently_closed_venues=False,
    )


@when("the application starts up")
def step_app_starts_up(context):
    import main

    # A container whose every pipeline entrypoint is an AsyncMock so we can assert
    # none was awaited. All optional services present so a "skip because unwired"
    # can't masquerade as the desired "skip because startup runs nothing".
    refresher = MagicMock()
    refresher.refresh_venues_by_filter_for_default_locations = AsyncMock()
    refresher.refresh_live_forecasts_for_all_venues = AsyncMock()
    refresher.refresh_weekly_forecasts_for_all_venues = AsyncMock()

    def _svc():
        s = MagicMock()
        s.enrich_all_venues = AsyncMock()
        s.refresh_photos_for_venues = AsyncMock()
        s.extract_all_venues = AsyncMock()
        s.classify_all_venues = AsyncMock()
        return s

    mock_container = MagicMock()
    mock_container.venues_refresher_service = refresher
    mock_container.google_places_enrichment_service = _svc()
    mock_container.photo_enrichment_service = _svc()
    mock_container.instagram_enrichment_service = _svc()
    mock_container.instagram_posts_enrichment_service = _svc()
    mock_container.menu_photo_enrichment_service = _svc()
    mock_container.menu_extraction_service = _svc()
    mock_container.vibe_classifier_service = _svc()

    context.startup_container = mock_container
    context.startup_mocks = [refresher] + [
        getattr(mock_container, name)
        for name in (
            "google_places_enrichment_service",
            "photo_enrichment_service",
            "instagram_enrichment_service",
            "instagram_posts_enrichment_service",
            "menu_photo_enrichment_service",
            "menu_extraction_service",
            "vibe_classifier_service",
        )
    ]

    original = getattr(main, "container", None)
    main.container = mock_container
    context.startup_records = []

    import logging

    class _ListHandler(logging.Handler):
        def emit(self, record):
            try:
                context.startup_records.append(record.getMessage())
            except Exception:
                pass

    handler = _ListHandler(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        asyncio.run(main.startup_background_pipelines(context.startup_settings))
    finally:
        root.removeHandler(handler)
        main.container = original


@then("no venue discovery, refresh, or enrichment pipeline is executed")
def step_no_pipeline_executed(context):
    called = []
    for mock in context.startup_mocks:
        for attr, value in vars(mock).items():
            if isinstance(value, AsyncMock) and value.await_count:
                called.append(f"{attr}:{value.await_count}")
    # Also inspect the AsyncMock children created via attribute access.
    for mock in context.startup_mocks:
        for name in dir(mock):
            child = getattr(mock, name)
            if isinstance(child, AsyncMock) and child.await_count:
                called.append(f"{name}:{child.await_count}")
    assert not called, f"a startup pipeline was executed: {sorted(set(called))}"


@then("the server serves the already-cached venues")
def step_server_serves_cached(context):
    # Serving does not depend on startup pipelines: the already-projected Redis set
    # is readable. Seed one and confirm it is still reachable after startup ran.
    _seed(context, "cached_bar", "Cached Bar", _INSIDE_LAT, _INSIDE_LNG)
    venue = context.repository.get_venue("cached_bar")
    assert venue is not None and venue.is_active()


@then("a log states that no pipelines run on startup by design")
def step_startup_log_present(context):
    joined = "\n".join(context.startup_records).lower()
    assert "startup" in joined and "no pipeline" in joined, (
        f"expected a 'no pipelines on startup' log line, got: {context.startup_records}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1b — Scheduled cron + admin triggers still run
# ══════════════════════════════════════════════════════════════════════════════
class _RecordingScheduler:
    def __init__(self):
        self.job_ids = []

    def add_job(self, func, **kwargs):
        self.job_ids.append(kwargs.get("id"))


@when("a scheduled refresh job fires")
def step_scheduled_job_fires(context):
    import main

    sched = _RecordingScheduler()
    settings_stub = SimpleNamespace(
        discovery_enabled=False,
        venues_catalog_refresh_minutes=43200,
        venues_live_refresh_minutes=5,
        weekly_forecast_cron="0 0 * * 0",
    )
    main.register_refresh_jobs(sched, settings_stub)
    context.scheduled_job_ids = sched.job_ids


@then("its pipeline executes normally")
def step_scheduled_pipeline_runs(context):
    # The live + weekly refresh jobs are always scheduled (discovery is the only
    # gated one); their presence proves the scheduler path is intact.
    assert "live_forecast_refresh" in context.scheduled_job_ids, context.scheduled_job_ids
    assert "weekly_forecast_refresh" in context.scheduled_job_ids, context.scheduled_job_ids


@then("triggering an enabled job from the admin panel executes that pipeline")
def step_admin_trigger_runs(context):
    # inventory_sync is a free, always-available admin job; triggering it returns a
    # "started" status (a 404 would mean the admin trigger path is broken).
    resp = context.client.post("/admin/trigger/inventory_sync")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") in ("started", "already_running"), resp.json()


# ══════════════════════════════════════════════════════════════════════════════
# 2 — Discovery is dormant
# ══════════════════════════════════════════════════════════════════════════════
@when('the admin panel triggers the "venue_catalog" job')
def step_trigger_venue_catalog(context):
    context.catalog_resp = context.client.post("/admin/trigger/venue_catalog")


@then("the request is rejected as an unknown job")
def step_catalog_unknown(context):
    resp = context.catalog_resp
    assert resp.status_code == 404, f"expected 404 Unknown job, got {resp.status_code}: {resp.text}"
    assert "unknown job" in resp.text.lower(), resp.text


@then("no GET /venues/filter call is made to BestTime")
def step_no_filter_call(context):
    assert not any(c.get("method") == "venue_filter" for c in context.besttime.calls)


@then("other admin-triggerable jobs remain available")
def step_other_jobs_available(context):
    resp = context.client.get("/admin/jobs")
    assert resp.status_code == 200, resp.text
    names = {job["name"] for job in resp.json()["jobs"]}
    assert "venue_catalog" not in names, "venue_catalog must be absent from the registry"
    assert "live_forecast" in names, names
    assert "inventory_sync" in names, names


@given("the discovery points admin config is empty or absent")
def step_discovery_points_absent(context):
    # Default fakeredis has no admin_config:discovery_points key.
    context.fake_redis.delete("admin_config:discovery_points")


@then("the venue-filter discovery has no locations to query")
def step_no_discovery_points(context):
    from app.services.venues_refresher_service import VenuesRefresherService

    refresher = VenuesRefresherService(
        venue_dao=context.repository,
        besttime_api=context.besttime,
        redis_client=context.fake_redis,
    )
    points = refresher._get_discovery_points()
    assert points == [], f"expected no discovery points, got {points}"


# ══════════════════════════════════════════════════════════════════════════════
# 3 — Recife-metro geo-fence eligibility
# ══════════════════════════════════════════════════════════════════════════════
@given("an active venue whose coordinates fall outside the Recife box")
def step_venue_outside(context):
    context.subject_id = _seed(
        context, "outside_bar", "Outside Bar", _OUTSIDE_LAT, _OUTSIDE_LNG
    )


@given("an active venue located in Olinda inside the Recife box")
def step_venue_olinda(context):
    context.subject_id = _seed(
        context, "olinda_bar", "Olinda Bar", _OLINDA_LAT, _OLINDA_LNG
    )


@given("an active venue that has no stored coordinates")
def step_venue_no_coords(context):
    context.subject_id = _seed(
        context, "nocoords_bar", "No Coords Bar", None, None
    )


@given("a venue previously excluded because it was outside the box")
def step_venue_previously_excluded(context):
    # Just north of the default box's max_lat (-7.85): outside now, inside a widened
    # box. Using a point near the boundary keeps the "widen" edit small + realistic.
    context.subject_id = _seed(
        context, "boundary_bar", "Boundary Bar", -7.70, -34.85
    )
    assert context.subject_id not in _servable(context), (
        "venue must start excluded (outside the default box)"
    )


@given("a mix of venues inside and outside the Recife box")
def step_mix_of_venues(context):
    context.inside_ids = {
        _seed(context, "mix_in_1", "Mix In 1", _INSIDE_LAT, _INSIDE_LNG),
        _seed(context, "mix_in_2", "Mix In 2", _OLINDA_LAT, _OLINDA_LNG),
    }
    context.outside_ids = {
        _seed(context, "mix_out_1", "Mix Out 1", _OUTSIDE_LAT, _OUTSIDE_LNG),
        _seed(context, "mix_out_2", "Mix Out 2", -1.0, -48.0),  # Belém
    }
    # A missing-coords venue is fail-open (inside the servable set).
    context.inside_ids.add(_seed(context, "mix_nocoords", "Mix NoCoords", None, None))


@when("the serving projection is rebuilt")
def step_projection_rebuilt(context):
    context.projection_summary = context.redis_projection_service.rebuild_redis_from_rds()


@when("eligibility is evaluated")
def step_eligibility_evaluated(context):
    context.servable = _servable(context)


@when("an operator widens the geo-fence box to include the venue")
def step_widen_box(context):
    widened = dict(_DEFAULT_BOX)
    widened["max_lat"] = -7.50  # now includes the boundary venue at -7.70
    resp = _put_geofence(context, widened)
    assert resp.status_code == 200, f"widen PUT failed: {resp.status_code} {resp.text}"


@when("an operator submits a geo-fence box with min latitude greater than max latitude")
def step_invalid_box(context):
    context.box_before = _servable(context)
    bad = dict(_DEFAULT_BOX)
    bad["min_lat"], bad["max_lat"] = 0.0, -10.0  # min > max
    context.invalid_resp = _put_geofence(context, bad)


def _box_from_store(context) -> dict:
    """Read the active box from the fake store (mirror of admin.geo_fence),
    falling back to the confirmed default when the accessor does not exist yet."""
    getter = getattr(context.rds_store, "get_geo_fence", None)
    if callable(getter):
        box = getter()
        if box:
            return box
    return dict(_DEFAULT_BOX)


def _inside_box(lat, lng, box) -> bool:
    """Fail-open bbox: missing coords or a disabled box are never excluded."""
    if not box or not box.get("enabled", True):
        return True
    if lat is None or lng is None:
        return True
    return (box["min_lat"] <= lat <= box["max_lat"]
            and box["min_lng"] <= lng <= box["max_lng"])


@when("eligibility is computed by the serving view and by the code evaluator")
def step_computed_both_ways(context):
    from app.services.venue_eligibility import (
        evaluate,
        eligibility_config_from_rules,
    )

    config = eligibility_config_from_rules(context.rds_store.list_eligibility_rules())
    box = _box_from_store(context)
    context.view_eligible = _servable(context)

    code_eligible = set()
    for vid, row in context.rds_store.venues.items():
        if row.get("lifecycle_status", "active") != "active":
            continue
        addr = context.rds_store.get_address(vid) or {}
        lat, lng = addr.get("lat"), addr.get("lng")
        verdict = evaluate(row.get("venue_name"), row.get("venue_type"), None, config)
        if verdict.soft_deletable:
            continue
        if not _inside_box(lat, lng, box):
            continue
        code_eligible.add(vid)
    context.code_eligible = code_eligible


# ── Then: geo-fence membership ────────────────────────────────────────────────
@then("the venue is absent from the eligible serving set")
def step_venue_absent(context):
    assert context.subject_id not in _servable(context), (
        f"{context.subject_id} should be geo-excluded but is servable"
    )
    # The projection was rebuilt: the out-of-box venue must not be in Redis either.
    assert context.redis_only_dao.get_venue(context.subject_id) is None, (
        f"{context.subject_id} should be absent from the Redis serving projection"
    )


@then("the venue is present in the eligible serving set")
@then("the venue becomes present in the eligible serving set")
def step_venue_present(context):
    assert context.subject_id in _servable(context), (
        f"{context.subject_id} should be servable but is absent"
    )
    # Both scenarios reach here only after rebuilding the projection, so the venue
    # must also be materialized in the Redis serving projection (honors "and from
    # the serving projection" — the projector mirrors the view).
    assert context.redis_only_dao.get_venue(context.subject_id) is not None, (
        f"{context.subject_id} should be projected to Redis after rebuild"
    )


@then("the venue is not counted toward the priority refresh budget")
def step_not_in_priority_budget(context):
    # The bounded refresh selects from the same serving view (servable-by-priority).
    selected = set(context.rds_store.list_servable_venue_ids_by_priority(10_000_000))
    assert context.subject_id not in selected, (
        f"{context.subject_id} must not be in the priority refresh selection"
    )


def _row_active(context, vid) -> bool:
    row = context.rds_store.venues.get(vid)
    return row is not None and row.get("lifecycle_status", "active") == "active"


@then("the venue row remains in the system of record (not deleted)")
def step_row_remains(context):
    assert _row_active(context, context.subject_id), (
        f"{context.subject_id} must remain active in RDS (geo-fence never deletes)"
    )


@then("the venue is not excluded by the geo-fence")
def step_not_geo_excluded(context):
    assert context.subject_id in context.servable, (
        f"missing-coords venue {context.subject_id} must be fail-open (servable)"
    )


@then("the geo-fence exclusion is reversible and never soft-deletes the venue")
def step_reversible_never_deletes(context):
    assert _row_active(context, context.subject_id), (
        f"{context.subject_id} must remain active (geo-fence never soft-deletes)"
    )


@then("the update is rejected")
def step_update_rejected(context):
    assert context.invalid_resp.status_code == 400, (
        f"expected 400, got {context.invalid_resp.status_code}: {context.invalid_resp.text}"
    )


@then("the active geo-fence box is unchanged")
def step_box_unchanged(context):
    # The serving set is identical to before the invalid PUT (box never mutated).
    assert _servable(context) == context.box_before
    box = context.rds_store.get_geo_fence()
    assert box["min_lat"] == _DEFAULT_BOX["min_lat"], box
    assert box["max_lat"] == _DEFAULT_BOX["max_lat"], box


@then("both classify exactly the same venues as eligible")
def step_view_matches_code(context):
    assert context.view_eligible == context.code_eligible, (
        f"view {context.view_eligible} != code {context.code_eligible}"
    )
