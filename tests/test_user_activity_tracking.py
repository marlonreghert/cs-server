"""Unit tests for app-activity tracking (POST /v1/sessions + activity counts).

Covers the EngagementService window math / pseudonymization / RDS-only contract
and the engagement router's 200/422/502 behavior, against the in-memory fake
store. The real RdsVenueStore SQL (ON CONFLICT, distinct-count windows) is proven
separately by tests/test_rds_store_contract.py under RDS_TEST_URL.
"""
from datetime import timedelta
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import engagement_router, set_engagement_service
from app.services.engagement_service import EngagementService
from app.utils.recife_time import recife_today
from tests.rds_fake import InMemoryRdsVenueStore


def _service():
    return EngagementService(
        redis_client=MagicMock(),
        rds_store=InMemoryRdsVenueStore(),
        pseudonymization_key="test-hmac-key",
    )


def _client(svc):
    app = FastAPI()
    app.include_router(engagement_router)
    set_engagement_service(svc)
    return TestClient(app)


# ── service: write path ───────────────────────────────────────────────────────
def test_record_session_pseudonymizes_and_never_touches_redis():
    svc = _service()
    svc.record_session("uid-alice")

    rows = svc.rds_store.app_session_rows_for(recife_today())
    assert rows == [svc.pseudonymize("uid-alice")]
    assert "uid-alice" not in rows
    assert not svc.rds_store.contains_raw_value("uid-alice")
    # App-activity is RDS-only — no Redis projection on this path.
    assert svc.redis.method_calls == []


def test_record_session_idempotent_same_day():
    svc = _service()
    svc.record_session("uid-alice")
    svc.record_session("uid-alice")
    assert svc.rds_store.count_users(None) == 1


# ── service: count windows ────────────────────────────────────────────────────
def test_activity_counts_empty():
    assert _service().activity_counts() == {
        "total_users": 0, "active_1d": 0, "active_7d": 0, "active_30d": 0,
    }


def test_activity_counts_window_math():
    svc = _service()
    today = recife_today()
    svc.rds_store.record_app_session(svc.pseudonymize("today-user"), today)
    svc.rds_store.record_app_session(svc.pseudonymize("old-user"), today - timedelta(days=10))

    assert svc.activity_counts() == {
        "total_users": 2,
        "active_1d": 1,    # only today-user
        "active_7d": 1,    # 10d-ago excluded
        "active_30d": 2,   # 10d-ago included
    }


def test_activity_counts_windows_are_inclusive_at_the_boundary():
    svc = _service()
    today = recife_today()
    # Exactly 6 days ago is inside the trailing-7d window; exactly 7 is outside.
    svc.rds_store.record_app_session(svc.pseudonymize("edge6"), today - timedelta(days=6))
    svc.rds_store.record_app_session(svc.pseudonymize("edge7"), today - timedelta(days=7))
    # Exactly 29 days ago is inside 30d; exactly 30 is outside.
    svc.rds_store.record_app_session(svc.pseudonymize("edge29"), today - timedelta(days=29))
    svc.rds_store.record_app_session(svc.pseudonymize("edge30"), today - timedelta(days=30))

    counts = svc.activity_counts()
    assert counts["active_7d"] == 1     # edge6 only
    assert counts["active_30d"] == 3    # edge6, edge7, edge29
    assert counts["total_users"] == 4


# ── router: HTTP contract ─────────────────────────────────────────────────────
def test_post_sessions_records_and_returns_ok():
    svc = _service()
    resp = _client(svc).post("/v1/sessions", json={"user_id": "uid-alice"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert svc.rds_store.count_users(None) == 1


def test_post_sessions_missing_user_id_is_422():
    resp = _client(_service()).post("/v1/sessions", json={})
    assert resp.status_code == 422


def test_post_sessions_returns_502_when_rds_unavailable():
    svc = _service()
    svc.rds_store.set_unavailable(True)
    resp = _client(svc).post("/v1/sessions", json={"user_id": "uid-alice"})
    assert resp.status_code == 502
    assert "retry" in resp.json()["detail"]
