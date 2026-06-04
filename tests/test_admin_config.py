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
