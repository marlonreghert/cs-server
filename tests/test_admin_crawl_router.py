"""Unit tests for app/routers/admin_crawl_router.py — CRUD + run-now + the
budget read model and venue coverage the cross-repo console contract
(../plans/260809_automated-event-crawl.md) requires — over a REAL
VenueRepository backed by the in-memory RDS fake. No live Apify; the
run-now tests wire a stub crawl service since they only need to prove the
route dispatches to `.start_run`/`.is_running` and shapes the response
(202/200/404, `started`/`reason`, `running`) — not the backgrounding/locking
mechanism itself, which is service-level behavior covered by
tests/test_instagram_crawl_service.py (genuinely concurrent, using the REAL
`ScheduledInstagramCrawlService`) and tests/test_crawl_schedule_sync.py (the
scheduled-vs-run-now cross-mechanism lock)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.routers.admin_crawl_router import router, set_container
from tests.rds_fake import InMemoryRdsVenueStore


class _StubCrawlService:
    def __init__(self, start_result=None, running=False):
        self._start_result = start_result if start_result is not None else {"started": True}
        self._running = running
        self.calls: list[str] = []

    async def start_run(self, handle):
        self.calls.append(handle)
        return self._start_result

    def is_running(self, handle):
        return self._running


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


def test_seed_results_limit_is_a_separate_field_from_results_limit():
    """Round-trips independently through create/patch/get — a separate
    setting from `results_limit` because the two want opposite values (see
    app/services/instagram_crawl_service.py's `_run_stream`)."""
    client, dao = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "seedcaptarget", "kind": "venue", "cron": "0 22 * * *",
        "results_limit": 10, "seed_results_limit": 300,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["results_limit"] == 10
    assert body["seed_results_limit"] == 300

    resp2 = client.patch("/admin/crawl-targets/seedcaptarget", json={"seed_results_limit": 50})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["seed_results_limit"] == 50
    assert resp2.json()["results_limit"] == 10  # untouched by the seed-cap patch


def test_reels_caps_are_separate_fields_that_round_trip_independently():
    client, dao = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "reelscaptarget", "kind": "venue", "cron": "0 22 * * *",
        "crawl_reels": True, "reels_results_limit": 4, "reels_seed_results_limit": 40,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reels_results_limit"] == 4
    assert body["reels_seed_results_limit"] == 40

    resp2 = client.patch("/admin/crawl-targets/reelscaptarget", json={"reels_seed_results_limit": 90})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["reels_seed_results_limit"] == 90
    assert resp2.json()["reels_results_limit"] == 4  # untouched


def test_effective_caps_resolve_the_fallback_chain_the_console_can_use_for_a_worst_case():
    """Proves the read model's `effective_*` fields are actually computed
    (not just present) via the SAME fallback chain the real crawl uses —
    so the console can state a reels-enabled target's true worst-case cost
    (`effective_seed_results_limit + effective_reels_seed_results_limit`)
    without re-implementing the reels-falls-back-to-posts chain itself."""
    client, dao = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "worstcasetarget", "kind": "venue", "cron": "0 22 * * *",
        "crawl_reels": True, "seed_results_limit": 150,
        # No reels_seed_results_limit set -> must fall back to the posts
        # seed cap (150) above, not the settings default (200).
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["effective_seed_results_limit"] == 150
    assert body["effective_reels_seed_results_limit"] == 150
    # settings.crawl_default_results_limit is 10 (app/config.py); neither
    # results_limit nor reels_results_limit was set on this target.
    assert body["effective_results_limit"] == 10
    assert body["effective_reels_results_limit"] == 10

    worst_case_first_run = body["effective_seed_results_limit"] + body["effective_reels_seed_results_limit"]
    assert worst_case_first_run == 300


def test_effective_reels_caps_are_zero_once_reels_have_already_seeded():
    """plans/260811_reels-on-seed-only.md: reels crawl exactly once, on the
    seed run, and never again once seeded — an operator sizing the NEXT run
    must be quoted 0 for reels, not a resolved-but-now-unreachable cap for
    a stream that will not run. Posts' own caps are untouched — the two
    streams' cost estimates stay independent, same as their cursors.

    plans/260814_seeded-state-and-config-validation.md §A: "seeded" is now
    `reels_seeded_at`, not `cursor_reels_at` alone — both are set here,
    exactly like a real completed seed leaves both behind together."""
    client, dao = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "alreadyseededreels", "kind": "venue", "cron": "0 22 * * *",
        "crawl_reels": True, "seed_results_limit": 90, "reels_seed_results_limit": 150,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Before any cursor is set, the estimate resolves normally.
    assert body["effective_reels_seed_results_limit"] == 150
    assert body["effective_reels_results_limit"] == 10

    dao.update_crawl_target("alreadyseededreels", {
        "cursor_reels_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "reels_seeded_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
    })
    resp2 = client.get("/admin/crawl-targets/alreadyseededreels")
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["effective_reels_seed_results_limit"] == 0
    assert body2["effective_reels_results_limit"] == 0
    # Posts' own caps are unaffected by the reels-side cursor.
    assert body2["effective_seed_results_limit"] == 90


# ── plans/260814_seeded-state-and-config-validation.md §A ───────────────────
def test_reels_seeded_is_false_for_a_brand_new_target():
    client, _ = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "reelsneverseeded", "kind": "venue", "cron": "0 22 * * *", "crawl_reels": True,
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["reels_seeded"] is False
    assert resp.json()["reels_seeded_at"] is None


def test_reels_seeded_is_true_once_an_empty_seed_completed():
    """The exact fact §A exists to surface: an EMPTY reels result still
    counts as seeded — `reels_seeded` must read True with no
    `cursor_reels_at` at all."""
    client, dao = _client()
    dao.upsert_crawl_target(
        "reelsemptyseeded", {"kind": "venue", "cron": "0 22 * * *", "crawl_reels": True},
    )
    dao.update_crawl_target(
        "reelsemptyseeded", {"reels_seeded_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
    )
    resp = client.get("/admin/crawl-targets/reelsemptyseeded")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reels_seeded"] is True
    assert body["reels_seeded_at"] is not None
    assert body["cursor_reels_at"] is None  # nothing was ever reached


def test_reels_seeded_is_false_with_a_cursor_alone_and_no_seeded_at():
    """Regression pin for §A's own defect: a row carrying `cursor_reels_at`
    but no `reels_seeded_at` (a shape that should not occur going forward,
    but did for every pre-existing row before this migration) must NOT
    read as seeded through the admin API either."""
    client, dao = _client()
    dao.upsert_crawl_target(
        "cursoronlyreels", {"kind": "venue", "cron": "0 22 * * *", "crawl_reels": True},
    )
    dao.update_crawl_target(
        "cursoronlyreels", {"cursor_reels_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
    )
    resp = client.get("/admin/crawl-targets/cursoronlyreels")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reels_seeded"] is False


# ── plans/260813_crawl-transport-failure-visibility.md §E ───────────────────
def test_posts_never_seeded_is_false_for_a_brand_new_target():
    client, _ = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "brandnew", "kind": "venue", "cron": "0 22 * * *",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["posts_never_seeded"] is False


def test_posts_never_seeded_is_true_once_a_run_leaves_the_cursor_unset():
    """The exact shape downtownbeergarden_ sat in for months: at least one
    run recorded, still no posts cursor."""
    client, dao = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "nevergotone", "kind": "venue", "cron": "0 22 * * *",
    })
    assert resp.status_code == 201, resp.text

    dao.update_crawl_target("nevergotone", {
        "last_run_at": datetime(2026, 8, 13, 1, 18, tzinfo=timezone.utc),
    })
    resp2 = client.get("/admin/crawl-targets/nevergotone")
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["posts_never_seeded"] is True


def test_posts_never_seeded_clears_once_the_cursor_is_set():
    client, dao = _client()
    resp = client.post("/admin/crawl-targets", json={
        "handle": "eventuallyseeded", "kind": "venue", "cron": "0 22 * * *",
    })
    assert resp.status_code == 201, resp.text

    dao.update_crawl_target("eventuallyseeded", {
        "last_run_at": datetime(2026, 8, 13, 1, 18, tzinfo=timezone.utc),
    })
    assert client.get("/admin/crawl-targets/eventuallyseeded").json()["posts_never_seeded"] is True

    dao.update_crawl_target("eventuallyseeded", {
        "cursor_posts_at": datetime(2026, 8, 14, tzinfo=timezone.utc),
    })
    resp2 = client.get("/admin/crawl-targets/eventuallyseeded")
    assert resp2.json()["posts_never_seeded"] is False


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


def test_run_now_starts_in_the_background_and_returns_202():
    """`202`, never a blocking wait for the crawl to finish — the endpoint's
    whole point after the fix is that it does NOT await the crawl."""
    stub = _StubCrawlService(start_result={"started": True})
    client, dao = _client(crawl_service=stub)
    dao.upsert_crawl_target("runnow", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.post("/admin/crawl-targets/runnow/run")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"handle": "runnow", "started": True, "reason": None}
    assert stub.calls == ["runnow"]


def test_run_now_returns_200_with_a_reason_when_it_cannot_start():
    """Not an HTTP error — a well-formed, synchronous answer describing WHY
    nothing started, verbatim from the service (`already_running`,
    `skipped_budget`, `skipped_disabled`, `skipped_failures`, ...)."""
    stub = _StubCrawlService(start_result={"started": False, "reason": "already_running"})
    client, dao = _client(crawl_service=stub)
    dao.upsert_crawl_target("busyhandle", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.post("/admin/crawl-targets/busyhandle/run")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"handle": "busyhandle", "started": False, "reason": "already_running"}


def test_run_now_missing_target_is_404_and_never_calls_the_service():
    stub = _StubCrawlService()
    client, _ = _client(crawl_service=stub)

    resp = client.post("/admin/crawl-targets/nosuch/run")

    assert resp.status_code == 404
    assert stub.calls == []


# ── `running` (the console's live indicator) ────────────────────────────────
def test_running_field_reflects_the_crawl_services_lock_state():
    dao = VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())
    dao.upsert_crawl_target("idlehandle", {"kind": "venue", "cron": "0 22 * * *"})

    client_idle, _ = _client(dao=dao, crawl_service=_StubCrawlService(running=False))
    assert client_idle.get("/admin/crawl-targets/idlehandle").json()["running"] is False

    client_busy, _ = _client(dao=dao, crawl_service=_StubCrawlService(running=True))
    assert client_busy.get("/admin/crawl-targets/idlehandle").json()["running"] is True


def test_running_defaults_false_without_a_crawl_service_configured():
    client, dao = _client()  # no crawl_service wired at all
    dao.upsert_crawl_target("noservicehandle", {"kind": "venue", "cron": "0 22 * * *"})

    resp = client.get("/admin/crawl-targets/noservicehandle")

    assert resp.json()["running"] is False


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
    # So the console can show "up to N results ~ $X" on the one-click
    # run-now flow with nothing typed (settings.crawl_default_results_limit
    # is 10, settings.crawl_default_seed_results_limit is 200).
    assert body["default_results_limit"] == 10
    assert body["default_seed_results_limit"] == 200


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
