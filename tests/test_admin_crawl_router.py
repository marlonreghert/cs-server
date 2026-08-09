"""Unit tests for app/routers/admin_crawl_router.py — CRUD + run-now over a
REAL VenueRepository backed by the in-memory RDS fake. No live Apify; the
run-now test wires a stub crawl service since it only needs to prove the
route dispatches to `.run_target` and shapes the response, not the crawl
logic itself (covered by tests/test_instagram_crawl_service.py and the BDD
feature)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.venue_repository import VenueRepository
from app.routers.admin_crawl_router import router, set_container
from tests.rds_fake import InMemoryRdsVenueStore


class _StubCrawlService:
    def __init__(self, report):
        self._report = report
        self.calls: list[str] = []

    async def run_target(self, handle):
        self.calls.append(handle)
        return self._report


def _client(dao=None, crawl_service=None):
    app = FastAPI()
    app.include_router(router)
    container = type("C", (), {
        "pipeline_repository": dao or VenueRepository(client=None, rds_store=InMemoryRdsVenueStore()),
        "instagram_crawl_service": crawl_service,
    })()
    set_container(container)
    return TestClient(app), container.pipeline_repository


def test_create_then_get_round_trips():
    client, dao = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "@NewHandle", "kind": "venue", "cron": "0 22 * * *",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # normalize_handle strips '@' and lowercases.
    assert body["handle"] == "newhandle"
    assert body["enabled"] is True
    assert body["timezone"] == "America/Recife"
    assert "next_fire_at" in body

    resp2 = client.get("/admin/crawl-targets/newhandle")
    assert resp2.status_code == 200
    assert resp2.json()["handle"] == "newhandle"


def test_create_rejects_a_malformed_cron():
    client, _ = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "badcron", "kind": "venue", "cron": "not a crontab",
    })
    assert resp.status_code == 422, resp.text


def test_create_rejects_an_invalid_kind():
    client, _ = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "wrongkind", "kind": "sponsor", "cron": "0 22 * * *",
    })
    assert resp.status_code == 422, resp.text


def test_patch_updates_only_given_fields():
    client, dao = _client()
    dao.upsert_crawl_target("patchme", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.patch("/admin/crawl-targets/patchme", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert resp.json()["cron"] == "0 22 * * *"  # untouched


def test_patch_missing_target_is_404():
    client, _ = _client()
    resp = client.patch("/admin/crawl-targets/nosuch", json={"enabled": False})
    assert resp.status_code == 404


def test_delete_then_get_is_404():
    client, dao = _client()
    dao.upsert_crawl_target("deleteme", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.delete("/admin/crawl-targets/deleteme")
    assert resp.status_code == 204

    resp2 = client.get("/admin/crawl-targets/deleteme")
    assert resp2.status_code == 404


def test_run_now_dispatches_to_the_crawl_service():
    stub = _StubCrawlService({"outcome": "success", "credit_exhausted": False})
    client, dao = _client(crawl_service=stub)
    dao.upsert_crawl_target("runnow", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.post("/admin/crawl-targets/runnow/run")

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "success"
    assert stub.calls == ["runnow"]


def test_run_now_missing_target_is_404_and_never_calls_the_service():
    stub = _StubCrawlService({"outcome": "success"})
    client, _ = _client(crawl_service=stub)

    resp = client.post("/admin/crawl-targets/nosuch/run")

    assert resp.status_code == 404
    assert stub.calls == []


def test_list_filters_by_enabled_and_kind():
    client, dao = _client()
    dao.upsert_crawl_target("v1", {"kind": "venue", "cron": "0 22 * * *", "enabled": True})
    dao.upsert_crawl_target("v2", {"kind": "venue", "cron": "0 22 * * *", "enabled": False})
    dao.upsert_crawl_target("p1", {"kind": "promoter", "cron": "0 22 * * *", "enabled": True})

    resp = client.get("/admin/crawl-targets", params={"enabled": True, "kind": "venue"})
    assert resp.status_code == 200
    handles = {row["handle"] for row in resp.json()}
    assert handles == {"v1"}
