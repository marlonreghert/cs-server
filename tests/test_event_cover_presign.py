"""Unit tests for GET /admin/events/{event_id}/cover
(plans/260806_event-cover-presign.md).

Covers the internal edge cases the BDD feature
(tests/bdd/api/event-cover-presign.feature) does not assert on directly:
route registration order, expiry arithmetic against a frozen clock, the
presign()->None->502 mapping, the metric's four result values, and the
opt-in admin_api_key gate's off-by-default behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers.admin_events_router import router, set_container


class _FakeEventStore:
    def __init__(self, events: dict | None = None) -> None:
        self.events = events or {}

    def get_event(self, event_id: str):
        return self.events.get(event_id)


class _FakeMediaStore:
    def __init__(self, url_or_none) -> None:
        self._result = url_or_none
        self.calls: list[dict] = []

    async def presign(self, key: str, expires_in: int = 900):
        self.calls.append({"key": key, "expires_in": expires_in})
        return self._result


def _client(event_store, media_store) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    set_container(SimpleNamespace(
        pipeline_repository=event_store, media_archive_store=media_store,
    ))
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_admin_api_key():
    """admin_api_key is a global setting; keep tests isolated from each other
    and from whatever the process default is."""
    original = settings.admin_api_key
    yield
    settings.admin_api_key = original


# ── route ordering ───────────────────────────────────────────────────────────
def test_cover_route_registered_before_event_catch_all():
    """Pins registration order directly on the router object: this router has
    already shipped a bug where a catch-all registered first swallowed
    /promoters and /review into 404s (see the comments beside those routes).
    /{event_id}/cover must appear before the bare /{event_id} GET."""
    paths = [r.path for r in router.routes]
    cover_idx = paths.index("/admin/events/{event_id}/cover")
    catch_all_idx = paths.index("/admin/events/{event_id}")
    assert cover_idx < catch_all_idx, paths


# ── expiry arithmetic ────────────────────────────────────────────────────────
def test_expires_at_matches_expires_in_against_a_frozen_clock():
    event_store = _FakeEventStore({"evt-1": {"event_id": "evt-1", "cover_photo_key": "k1"}})
    media_store = _FakeMediaStore("https://example.com/signed")
    client = _client(event_store, media_store)

    frozen_now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    with patch("app.routers.admin_events_router.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        # timedelta/timezone come from the same module import; only .now is
        # stubbed, so the real timedelta arithmetic still runs.
        resp = client.get("/admin/events/evt-1/cover")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_in"] == settings.event_cover_presign_expires_seconds
    expected_expiry = frozen_now.isoformat().replace("+00:00", "Z")
    # Accept either Z or +00:00 rendering; assert the actual delta instead of
    # a brittle string match.
    expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    delta = (expires_at - frozen_now).total_seconds()
    assert delta == settings.event_cover_presign_expires_seconds, (
        f"expires_at - now = {delta}s, expected {settings.event_cover_presign_expires_seconds}s "
        f"(expected_expiry ~= {expected_expiry})"
    )


# ── presign() -> None -> 502, never a 200 with a null url ───────────────────
def test_presign_returning_none_maps_to_502_not_200_null():
    event_store = _FakeEventStore({"evt-1": {"event_id": "evt-1", "cover_photo_key": "k1"}})
    media_store = _FakeMediaStore(None)  # MediaArchiveStore.presign()'s real failure contract
    client = _client(event_store, media_store)

    resp = client.get("/admin/events/evt-1/cover")

    assert resp.status_code == 502, resp.text
    assert resp.status_code != 200
    # Even inspecting the raw body: no "url" key sneaks through as null.
    body = resp.json()
    assert "url" not in body, body


# ── metric's four result values ─────────────────────────────────────────────
def test_metric_records_all_four_result_values():
    from app.metrics import EVENT_COVER_PRESIGN_TOTAL

    def _count(result):
        return EVENT_COVER_PRESIGN_TOTAL.labels(result=result)._value.get()

    before = {r: _count(r) for r in ("signed", "no_key", "not_found", "failed")}

    # signed
    store = _FakeEventStore({"e-signed": {"event_id": "e-signed", "cover_photo_key": "k"}})
    client = _client(store, _FakeMediaStore("https://example.com/ok"))
    assert client.get("/admin/events/e-signed/cover").status_code == 200

    # no_key
    store = _FakeEventStore({"e-nokey": {"event_id": "e-nokey", "cover_photo_key": None}})
    client = _client(store, _FakeMediaStore("unused"))
    assert client.get("/admin/events/e-nokey/cover").status_code == 404

    # not_found
    store = _FakeEventStore({})
    client = _client(store, _FakeMediaStore("unused"))
    assert client.get("/admin/events/nope/cover").status_code == 404

    # failed
    store = _FakeEventStore({"e-failed": {"event_id": "e-failed", "cover_photo_key": "k"}})
    client = _client(store, _FakeMediaStore(None))
    assert client.get("/admin/events/e-failed/cover").status_code == 502

    after = {r: _count(r) for r in ("signed", "no_key", "not_found", "failed")}
    for result in ("signed", "no_key", "not_found", "failed"):
        assert after[result] == before[result] + 1, (result, before, after)


# ── admin_api_key gate: off by default, real when configured ────────────────
def test_admin_gate_is_a_noop_when_admin_api_key_is_unset():
    settings.admin_api_key = ""
    store = _FakeEventStore({"evt-1": {"event_id": "evt-1", "cover_photo_key": "k1"}})
    client = _client(store, _FakeMediaStore("https://example.com/ok"))

    resp = client.get("/admin/events/evt-1/cover")  # no header at all

    assert resp.status_code == 200, resp.text


def test_admin_gate_rejects_missing_or_wrong_key_when_configured():
    settings.admin_api_key = "s3cr3t"
    store = _FakeEventStore({"evt-1": {"event_id": "evt-1", "cover_photo_key": "k1"}})
    client = _client(store, _FakeMediaStore("https://example.com/ok"))

    no_header = client.get("/admin/events/evt-1/cover")
    wrong_header = client.get(
        "/admin/events/evt-1/cover", headers={"X-Admin-Api-Key": "wrong"},
    )
    right_header = client.get(
        "/admin/events/evt-1/cover", headers={"X-Admin-Api-Key": "s3cr3t"},
    )

    assert no_header.status_code == 401, no_header.text
    assert wrong_header.status_code == 401, wrong_header.text
    assert right_header.status_code == 200, right_header.text
