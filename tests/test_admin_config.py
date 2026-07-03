"""Unit tests for admin config → RDS (system of record) + Redis mirror.

Covers AdminConfigService edge cases and the /admin/config HTTP mapping that the
BDD feature (which exercises the service directly) does not assert.
"""
import json
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.admin_trigger_router import router, set_container
from app.services.admin_config_service import AdminConfigService
from app.services.venue_eligibility import EligibilityConfig, load_eligibility_config
from tests.rds_fake import InMemoryRdsVenueStore, RdsUnavailable


def _eligibility_validator(value):
    EligibilityConfig.from_dict(value, from_admin_override=True)
    return value


def _svc(validators=None):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    store = InMemoryRdsVenueStore()
    svc = AdminConfigService(redis_client, rds_store=store, validators=validators or {})
    return svc, redis_client, store


# ── service: write-through ordering ─────────────────────────────────────────
def test_set_writes_rds_then_mirror():
    svc, r, store = _svc()
    svc.set("scoring_weights", {"a": 1})
    assert store.get_admin_config("scoring_weights")["value"] == {"a": 1}
    assert json.loads(r.get("admin_config:scoring_weights")) == {"a": 1}
    assert svc.get("scoring_weights") == {"a": 1}


def test_get_falls_back_to_rds_when_mirror_absent():
    svc, r, store = _svc()
    store.upsert_admin_config("k", {"v": 9}, "seed")  # RDS only, no mirror
    assert r.get("admin_config:k") is None
    assert svc.get("k") == {"v": 9}


def test_get_missing_returns_none():
    svc, _, _ = _svc()
    assert svc.get("nope") is None


# ── service: failure modes ───────────────────────────────────────────────────
def test_mirror_failure_after_rds_commit_raises_but_rds_persisted():
    svc, r, store = _svc()

    def boom(*a, **k):
        raise RuntimeError("mirror down")

    r.set = boom
    with pytest.raises(RuntimeError):
        svc.set("k", {"v": 1})
    assert store.get_admin_config("k")["value"] == {"v": 1}  # truth committed


def test_rds_outage_raises_before_touching_mirror():
    svc, r, store = _svc()
    store.set_unavailable(True)
    with pytest.raises(RdsUnavailable):
        svc.set("k", {"v": 1})
    assert r.get("admin_config:k") is None  # mirror never touched


# ── service: validation dispatch + byte-compat ──────────────────────────────
def test_validation_rejects_invalid_eligibility_before_any_write():
    svc, r, store = _svc(validators={"venue_eligibility": _eligibility_validator})
    with pytest.raises(ValueError):
        svc.set("venue_eligibility", {"blocked_venue_types": "not-a-list"})
    assert store.get_admin_config("venue_eligibility") is None
    assert r.get("admin_config:venue_eligibility") is None


def test_eligibility_value_is_byte_compatible_with_reader():
    svc, r, _ = _svc(validators={"venue_eligibility": _eligibility_validator})
    svc.set("venue_eligibility",
            {"blocked_venue_types": ["DRUGSTORE"], "blocked_google_types": ["pharmacy"]})
    cfg = load_eligibility_config(r)
    assert "DRUGSTORE" in cfg.blocked_venue_types
    assert "pharmacy" in cfg.blocked_google_types


# ── service: delete ──────────────────────────────────────────────────────────
def test_delete_removes_rds_and_mirror():
    svc, r, store = _svc()
    svc.set("k", {"v": 1})
    svc.delete("k")
    assert store.get_admin_config("k") is None
    assert r.get("admin_config:k") is None
    assert svc.get("k") is None


# ── HTTP endpoint mapping ────────────────────────────────────────────────────
def _client(svc):
    app = FastAPI()
    app.include_router(router)
    set_container(SimpleNamespace(admin_config_service=svc, rds_store=svc.rds_store))
    return TestClient(app)


def test_put_get_delete_list_endpoints():
    svc, _, _ = _svc()
    client = _client(svc)
    assert client.put("/admin/config/scoring_weights", json={"a": 1}).status_code == 200
    assert client.get("/admin/config/scoring_weights").json()["value"] == {"a": 1}
    assert "scoring_weights" in client.get("/admin/config").json()["keys"]
    assert client.delete("/admin/config/scoring_weights").status_code == 200
    assert client.get("/admin/config/scoring_weights").status_code == 404  # missing


def test_put_invalid_eligibility_returns_400():
    svc, _, _ = _svc(validators={"venue_eligibility": _eligibility_validator})
    client = _client(svc)
    resp = client.put("/admin/config/venue_eligibility", json={"blocked_venue_types": "x"})
    assert resp.status_code == 400


def test_put_returns_502_when_mirror_fails():
    svc, r, store = _svc()

    def boom(*a, **k):
        raise RuntimeError("mirror down")

    r.set = boom
    client = _client(svc)
    resp = client.put("/admin/config/scoring_weights", json={"a": 1})
    assert resp.status_code == 502
    assert store.get_admin_config("scoring_weights")["value"] == {"a": 1}  # RDS committed


# ── geo-fence endpoints (typed geo-fence tables, NOT the generic admin_config) ─
def test_geofence_get_returns_default_fence():
    svc, _, _ = _svc()
    client = _client(svc)
    resp = client.get("/admin/config/geofence")
    assert resp.status_code == 200
    fence = resp.json()
    assert set(fence) == {"enabled", "cities", "geo_excluded_active"}
    assert [c["slug"] for c in fence["cities"]] == ["recife"]
    # No venues seeded → nothing outside the circles (and never null when the
    # store can count).
    assert fence["geo_excluded_active"] == 0


def test_geofence_count_degrades_to_none_when_uncountable():
    # The warning number is best-effort: a store that cannot count (deploy
    # window before migration 0015, or a query failure) yields null — the
    # endpoint itself never fails.
    svc, _, store = _svc()

    def boom():
        raise RuntimeError("relation admin.geo_fence_city does not exist")

    store.count_active_venues_outside_circles = boom
    client = _client(svc)
    resp = client.get("/admin/config/geofence")
    assert resp.status_code == 200
    assert resp.json()["geo_excluded_active"] is None


def test_geofence_put_response_carries_the_count():
    svc, r, _ = _svc()
    client = _client_with_redis(svc, r)
    resp = client.put(
        "/admin/config/geofence",
        json={"enabled": False, "cities": [{"slug": "recife", "radius_km": 40}]},
    )
    assert resp.status_code == 200
    assert resp.json()["geo_excluded_active"] == 0
    # The Redis mirror stays the bare validated fence — the count is
    # response-only (the mirror must round-trip validate_geo_fence()).
    mirrored = json.loads(r.get("admin_config:venue_geofence"))
    assert "geo_excluded_active" not in mirrored


def test_geofence_capitals_catalog_route():
    # /config/geofence/capitals must resolve to the dedicated handler (the
    # generic /config/{key} matches a single segment only).
    svc, _, _ = _svc()
    client = _client(svc)
    resp = client.get("/admin/config/geofence/capitals")
    assert resp.status_code == 200
    capitals = resp.json()["capitals"]
    assert len(capitals) == 27
    assert [c["name"] for c in capitals] == sorted(c["name"] for c in capitals)


def _client_with_redis(svc, redis_client):
    """A client whose container also exposes a venue_dao.client for the geo-fence
    Redis mirror (the real container wires the DAO; _svc's SimpleNamespace does not)."""
    app = FastAPI()
    app.include_router(router)
    set_container(SimpleNamespace(
        admin_config_service=svc,
        rds_store=svc.rds_store,
        venue_dao=SimpleNamespace(client=redis_client),
    ))
    return TestClient(app)


def test_geofence_put_routes_to_typed_table_not_admin_config():
    # Route-collision guard: /config/geofence must hit the dedicated handler
    # (writing the typed geo-fence tables via the store), NOT the generic
    # /config/{key} which would land in admin.admin_config where the serving
    # view never reads it.
    svc, r, store = _svc()
    client = _client_with_redis(svc, r)
    resp = client.put(
        "/admin/config/geofence",
        json={"enabled": True, "cities": [{"slug": "salvador", "radius_km": 25}]},
    )
    assert resp.status_code == 200
    # Landed in the typed store, not the generic admin_config blob, with the
    # coordinates resolved server-side from the capitals catalog.
    stored = store.get_geo_fence()
    assert [c["slug"] for c in stored["cities"]] == ["salvador"]
    assert stored["cities"][0]["lat"] == pytest.approx(-12.9714)
    assert store.get_admin_config("geofence") is None
    # Redis mirror written for admin/parity reads.
    mirrored = json.loads(r.get("admin_config:venue_geofence"))
    assert [c["slug"] for c in mirrored["cities"]] == ["salvador"]


def test_geofence_put_invalid_returns_400_and_fence_unchanged():
    svc, _, store = _svc()
    client = _client(svc)
    before = store.get_geo_fence()
    resp = client.put(
        "/admin/config/geofence",
        json={"enabled": True, "cities": [{"slug": "narnia", "radius_km": 25}]},
    )
    assert resp.status_code == 400
    assert store.get_geo_fence() == before  # active fence untouched


def test_geofence_put_legacy_box_is_400_naming_new_shape():
    svc, _, store = _svc()
    client = _client(svc)
    before = store.get_geo_fence()
    resp = client.put(
        "/admin/config/geofence",
        json={"min_lat": -9.0, "max_lat": -7.0, "min_lng": -36.0, "max_lng": -34.0},
    )
    assert resp.status_code == 400
    assert "cities" in resp.text
    assert store.get_geo_fence() == before


def test_venue_catalog_trigger_is_404_unknown_job():
    # Discovery is dormant: venue_catalog was removed from JOB_REGISTRY.
    from app.routers.admin_trigger_router import JOB_REGISTRY

    assert "venue_catalog" not in JOB_REGISTRY
    svc, _, _ = _svc()
    client = _client(svc)
    resp = client.post("/admin/trigger/venue_catalog")
    assert resp.status_code == 404
    assert "unknown job" in resp.text.lower()


# ── legacy /venues/eligibility-config reconciled through AdminConfigService ──
def _elig_client(container):
    app = FastAPI()
    app.include_router(router)
    set_container(container)
    return TestClient(app)


def test_eligibility_endpoint_lands_in_rds_when_service_wired():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    store = InMemoryRdsVenueStore()
    svc = AdminConfigService(redis_client, rds_store=store,
                             validators={"venue_eligibility": _eligibility_validator})
    client = _elig_client(SimpleNamespace(admin_config_service=svc, rds_store=store))
    body = {"blocked_venue_types": ["DRUGSTORE"], "blocked_google_types": ["pharmacy"]}
    resp = client.post("/admin/venues/eligibility-config", json=body)
    assert resp.status_code == 200
    assert store.get_admin_config("venue_eligibility")["value"] == body  # RDS = truth
    # byte-compat: the reader parses the mirror back into the live config
    assert "DRUGSTORE" in load_eligibility_config(redis_client).blocked_venue_types


def test_eligibility_endpoint_falls_back_to_redis_without_service():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    store = InMemoryRdsVenueStore()  # only to assert it stays untouched
    venue_dao = SimpleNamespace(client=redis_client)
    client = _elig_client(SimpleNamespace(admin_config_service=None, venue_dao=venue_dao, rds_store=store))
    resp = client.post("/admin/venues/eligibility-config", json={"blocked_venue_types": ["DRUGSTORE"]})
    assert resp.status_code == 200
    assert "DRUGSTORE" in load_eligibility_config(redis_client).blocked_venue_types  # Redis written
    assert store.get_admin_config("venue_eligibility") is None  # guard fell back; RDS untouched


def test_eligibility_endpoint_invalid_returns_400_persists_nothing():
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    store = InMemoryRdsVenueStore()
    svc = AdminConfigService(redis_client, rds_store=store,
                             validators={"venue_eligibility": _eligibility_validator})
    client = _elig_client(SimpleNamespace(admin_config_service=svc, rds_store=store))
    resp = client.post("/admin/venues/eligibility-config", json={"blocked_venue_types": "not-a-list"})
    assert resp.status_code == 400
    assert store.get_admin_config("venue_eligibility") is None
    assert redis_client.get("admin_config:venue_eligibility") is None
