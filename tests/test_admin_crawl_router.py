"""Unit tests for app/routers/admin_crawl_router.py — CRUD + run-now + the
budget read model and venue coverage the cross-repo console contract
(../plans/260809_automated-event-crawl.md) requires — over a REAL
VenueRepository backed by the in-memory RDS fake. No live Apify; the
run-now test wires a stub crawl service since it only needs to prove the
route dispatches to `.run_target` and shapes the response, not the crawl
logic itself (covered by tests/test_instagram_crawl_service.py and the BDD
feature)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.routers.admin_crawl_router import router, set_container
from tests.rds_fake import InMemoryRdsVenueStore


class _StubCrawlService:
    def __init__(self, report):
        self._report = report
        self.calls: list[str] = []

    async def run_target(self, handle):
        self.calls.append(handle)
        return self._report


class _FakeBudgetDao:
    """Mirrors CrawlBudgetDao's public interface — the Redis-backed monthly
    counter, faked at the true external boundary, same as tests/test_
    instagram_crawl_service.py's identical fake."""

    def __init__(self, year_month="2026-08", used=0):
        self._year_month = year_month
        self._counts = {year_month: used}

    def current_year_month_utc(self, now=None):
        return self._year_month

    def get_month_count(self, year_month):
        return self._counts.get(year_month, 0)

    def increment_month(self, year_month, n):
        self._counts[year_month] = self._counts.get(year_month, 0) + n
        return self._counts[year_month]


def _client(dao=None, crawl_service=None, budget_dao=None):
    app = FastAPI()
    app.include_router(router)
    container = type("C", (), {
        "pipeline_repository": dao or VenueRepository(client=None, rds_store=InMemoryRdsVenueStore()),
        "instagram_crawl_service": crawl_service,
        "crawl_budget_dao": budget_dao,
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
    assert "next_run_at" in body
    assert body["venues"] == []

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


# ── Budget read model (../plans/260809_automated-event-crawl.md) ───────────
def test_get_budget_returns_month_used_limit_remaining_and_unit_price():
    budget_dao = _FakeBudgetDao(year_month="2026-08", used=250)
    client, _ = _client(budget_dao=budget_dao)

    resp = client.get("/admin/crawl-targets/budget")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["year_month"] == "2026-08"
    assert body["used"] == 250
    # settings.crawl_monthly_result_budget default is 1000 (app/config.py).
    assert body["limit"] == 1000
    assert body["remaining"] == 750
    assert body["unit_cost_usd"] > 0


def test_get_budget_remaining_never_goes_negative_when_over_spent():
    budget_dao = _FakeBudgetDao(year_month="2026-08", used=5000)
    client, _ = _client(budget_dao=budget_dao)

    resp = client.get("/admin/crawl-targets/budget")

    assert resp.status_code == 200, resp.text
    assert resp.json()["remaining"] == 0


def test_budget_unconfigured_is_503_not_a_silent_zero():
    client, _ = _client(budget_dao=None)
    resp = client.get("/admin/crawl-targets/budget")
    assert resp.status_code == 503


def test_budget_route_is_not_swallowed_by_the_handle_lookup():
    """The regression this repo has already been bitten by once
    (admin_events_router.py's "/review"/"/promoters" vs "/{event_id}"):
    FastAPI matches routes in registration order, and "/budget" is the same
    shape as "/{handle}". Proven two ways: with NO crawl target named
    "budget" (a wrong ordering would 404 as "crawl target not found"
    instead of returning the budget shape), and — the sharper case — WITH
    one (a wrong ordering would return 200 with the crawl TARGET's shape,
    not the budget's)."""
    budget_dao = _FakeBudgetDao(year_month="2026-08", used=10)
    client, dao = _client(budget_dao=budget_dao)

    resp_no_target = client.get("/admin/crawl-targets/budget")
    assert resp_no_target.status_code == 200, resp_no_target.text
    assert "year_month" in resp_no_target.json()
    assert "cron" not in resp_no_target.json()

    dao.upsert_crawl_target("budget", {"kind": "venue", "cron": "0 22 * * *"})
    resp_with_target = client.get("/admin/crawl-targets/budget")
    assert resp_with_target.status_code == 200, resp_with_target.text
    body = resp_with_target.json()
    assert "year_month" in body, body
    assert "cron" not in body, body


# ── Venue coverage (../plans/260809_automated-event-crawl.md) ──────────────
def test_venue_coverage_lists_every_venue_sharing_the_handle():
    client, dao = _client()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="Entre Amigos O Bode", venue_lat=-8.0, venue_lng=-34.9))
    dao.upsert_venue(Venue(
        venue_id="v2", venue_name="Entre Amigos O Bode Espinheiro", venue_lat=-8.0, venue_lng=-34.9,
    ))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="entreamigosobode", status="found"))
    dao.set_venue_instagram(VenueInstagram(venue_id="v2", instagram_handle="entreamigosobode", status="found"))
    dao.upsert_crawl_target("entreamigosobode", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.get("/admin/crawl-targets/entreamigosobode")

    assert resp.status_code == 200, resp.text
    venues = resp.json()["venues"]
    assert {v["venue_id"] for v in venues} == {"v1", "v2"}
    assert {v["venue_name"] for v in venues} == {
        "Entre Amigos O Bode", "Entre Amigos O Bode Espinheiro",
    }


def test_promoter_target_has_an_empty_venue_list_not_null():
    client, dao = _client()
    dao.upsert_crawl_target("somepromoter", {"kind": "promoter", "cron": "0 22 * * *"})

    resp = client.get("/admin/crawl-targets/somepromoter")

    assert resp.status_code == 200, resp.text
    assert resp.json()["venues"] == []


def test_venue_target_with_no_venue_currently_pointing_at_it_has_an_empty_list():
    client, dao = _client()
    dao.upsert_crawl_target("orphanhandle", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.get("/admin/crawl-targets/orphanhandle")

    assert resp.status_code == 200, resp.text
    assert resp.json()["venues"] == []


def test_venue_coverage_is_included_in_the_list_endpoint_too():
    client, dao = _client()
    dao.upsert_venue(Venue(venue_id="v1", venue_name="Solo Venue", venue_lat=-8.0, venue_lng=-34.9))
    dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="solohandle", status="found"))
    dao.upsert_crawl_target("solohandle", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.get("/admin/crawl-targets")

    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["handle"] == "solohandle")
    assert row["venues"] == [{"venue_id": "v1", "venue_name": "Solo Venue"}]
