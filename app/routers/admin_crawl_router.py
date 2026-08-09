"""Admin API for scheduled Instagram crawl targets (`events.crawl_target`).

See plans/260809_scheduled-incremental-instagram-crawl.md §Data, Config, And
API Impact: "Admin API: CRUD for crawl targets, plus a run-now action and a
read model carrying last-run results/cost and next-fire time. Additive."
"Serving: none. No app-facing change." — nothing here is read by the mobile
app or `venue_router`; this is operator tooling only, the same posture
`admin_events_router` and `admin_trigger_router` already take.

Crontab validation happens HERE, at write time (`validate_crontab`,
app/services/instagram_crawl_service.py) — the unit test plan's own
requirement: "Crontab parsing rejects a malformed string at write time, not
at fire time." `CrawlScheduleSync` (app/services/crawl_schedule_sync.py)
still guards defensively at sync time too, but this is the primary gate.

`cron` means STANDARD Unix crontab(5) — day-of-week `0`/`7`=Sunday through
`6`=Saturday. `validate_crontab`/`build_cron_trigger` translate that before
building an APScheduler trigger (APScheduler's own day-of-week numbering is
different — `0`=Monday — and its `CronTrigger.from_crontab` does NOT
translate; see `instagram_crawl_service.py`'s module-level comment on
`build_cron_trigger` for how this was found and fixed). `next_run_at` below
goes through the SAME translation, so it is always consistent with what a
target will actually do, never a second, disagreeing interpretation.

Two additions per the cross-repo contract (../plans/260809_automated-event-
crawl.md, pinned for vibes_bot's console): a budget read model
(`GET /budget`) and, on every target row, the venues its handle covers
(`venues`) — both DERIVED HERE, never recomputed by the console. `next_run_at`
and `venues` are named exactly as that contract's "read model" row says;
`cursor_age_seconds` is deliberately NOT included — the console already has
the raw cursor timestamp and subtracting it from "now" is rendering, not
business logic, per an explicit, accepted deviation.

`POST /{handle}/run` is BACKGROUNDED — it starts the crawl and returns
immediately, it never awaits `run_target` inline. That used to block until
the whole crawl finished, which for a brand-new target's 3-month seed
(scrape, download, classify, chain extraction) takes minutes; vibes_bot's
admin proxy times out at 10s and Caddy at 180s, so the console's button
would time out while the crawl kept running server-side — the operator sees
a failure, the work silently continues, and the natural next move (click
again) used to be able to race the FIRST click, because the old endpoint
took no lock at all. `ScheduledInstagramCrawlService.start_run` (app/
services/instagram_crawl_service.py) now owns the fix: it acquires the same
per-handle lock the scheduler uses before returning, and `running` below
reads that same lock, so the console can poll it exactly like the Jobs page
already polls `job.running`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.services.instagram_crawl_service import (
    KIND_PROMOTER,
    KIND_VENUE,
    InvalidCrawlTargetConfig,
    build_cron_trigger,
    group_venue_ids_by_handle,
    validate_crontab,
)
from app.services.instagram_handle_sources import normalize_handle

router = APIRouter(prefix="/admin/crawl-targets", tags=["admin", "crawl"])

# Global container reference, set during startup — same pattern as
# admin_events_router / admin_trigger_router / internal_router.
_container = None

_VALID_KINDS = (KIND_VENUE, KIND_PROMOTER)


def set_container(container) -> None:
    global _container
    _container = container


def _dao():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    dao = getattr(_container, "pipeline_repository", None)
    if dao is None:
        raise HTTPException(status_code=503, detail="Venue repository not configured")
    return dao


def _crawl_service():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    svc = getattr(_container, "instagram_crawl_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Instagram crawl service not configured")
    return svc


def _budget_dao():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    dao = getattr(_container, "crawl_budget_dao", None)
    if dao is None:
        raise HTTPException(status_code=503, detail="Crawl budget not configured")
    return dao


def _require_valid_kind(kind: Optional[str]) -> None:
    if kind is not None and kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=422, detail=f"kind must be one of {_VALID_KINDS}, got {kind!r}"
        )


def _require_valid_cron(cron: Optional[str]) -> None:
    if cron is None:
        return
    try:
        validate_crontab(cron)
    except InvalidCrawlTargetConfig as e:
        raise HTTPException(status_code=422, detail=str(e))


class CrawlTargetVenueRef(BaseModel):
    venue_id: str
    venue_name: Optional[str] = None


class CrawlTargetOut(BaseModel):
    handle: str
    kind: str
    enabled: bool
    cron: str
    timezone: str
    crawl_reels: bool
    # Whether scheduled archiving classifies images (flyer detection) the
    # same way the manual VenuePhotoArchiveService run does. Defaults TRUE;
    # an operator sets it FALSE per target for an explicit cheap mode.
    classify_images: bool = True
    initial_lookback: Optional[str] = None
    results_limit: Optional[int] = None
    cursor_posts_at: Optional[datetime] = None
    cursor_reels_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_run_results: int = 0
    last_run_cost_usd: Optional[float] = None
    consecutive_failures: int = 0
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Computed, not stored — the read model's own convenience so an operator
    # does not have to parse a crontab string by hand. Named `next_run_at` to
    # match the cross-repo contract exactly (../plans/260809_automated-event-
    # crawl.md's read-model row).
    next_run_at: Optional[datetime] = None
    # The venues currently pointing at this handle — derived HERE (never by
    # the console): the whole reason targets are keyed by handle rather than
    # venue_id is that 48 production venues share one with another venue, so
    # an operator working from a per-venue mental model needs this to find
    # their venue's target at all. ALWAYS a list, never null — a promoter-
    # kind target, or a venue-kind one no venue currently references, is an
    # empty list, not an absent field the console has to special-case.
    venues: list[CrawlTargetVenueRef] = Field(default_factory=list)
    # Whether a crawl of this handle is in flight RIGHT NOW — scheduled or
    # admin-triggered, since both hold the SAME per-handle lock
    # (`ScheduledInstagramCrawlService.lock_name_for`/`is_running`). The
    # console's live indicator: poll this the same way the Jobs page
    # already polls `job.running`.
    running: bool = False


def _venue_coverage(dao, handle: str) -> list[CrawlTargetVenueRef]:
    """Every venue whose `instagram.handle` currently normalizes to this
    target's handle. Reuses `group_venue_ids_by_handle` — the SAME reverse
    index `ScheduledInstagramCrawlService._chain` uses to find who to
    archive/extract for — so the read model and the crawl's own chaining
    target can never disagree about which venues a handle covers."""
    handles = dao.list_instagram_handles() or {}
    venue_ids = group_venue_ids_by_handle(handles).get(handle, [])
    out = []
    for venue_id in venue_ids:
        venue = dao.get_venue(venue_id)
        out.append(CrawlTargetVenueRef(
            venue_id=venue_id, venue_name=venue.venue_name if venue is not None else None,
        ))
    return out


def _is_running(handle: str) -> bool:
    """False whenever the crawl service isn't configured at all (no Apify
    token) — there is no possible in-flight crawl to report in that case,
    so this degrades safely rather than raising out of a list/get response."""
    svc = getattr(_container, "instagram_crawl_service", None) if _container is not None else None
    return bool(svc is not None and svc.is_running(handle))


def _to_out(dao, row: dict) -> dict:
    """The full read model for one row: the stored fields, plus the derived
    ones the cross-repo contract requires (`next_run_at`, `venues`) and the
    live `running` indicator. Next-fire computation is best-effort and never
    raises — a target with a since-corrupted cron (should not happen; write
    time already validates it) reads as `next_run_at: null` rather than
    breaking the whole list/get response."""
    out = dict(row)
    try:
        trigger = build_cron_trigger(row["cron"], timezone=row.get("timezone") or "America/Recife")
        out["next_run_at"] = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    except Exception:
        out["next_run_at"] = None
    out["venues"] = _venue_coverage(dao, row["handle"])
    out["running"] = _is_running(row["handle"])
    return out


class CrawlBudgetOut(BaseModel):
    """The console's own words for this: "run now" has to reflect what's
    left, and the remaining budget is shown above the table — an operator
    clicking a paid action with no idea what's left is exactly the
    situation the monthly budget exists to prevent. `unit_cost_usd` is
    included so the console renders dollars without holding its own copy of
    the price."""
    year_month: str
    used: int
    limit: int
    remaining: int
    unit_cost_usd: float


class CrawlTargetCreate(BaseModel):
    handle: str
    kind: str = KIND_VENUE
    # Standard 5-field Unix crontab (minute hour day month day_of_week),
    # validated AND translated at write time via `validate_crontab` ->
    # `build_cron_trigger` (app/services/instagram_crawl_service.py).
    # day_of_week here is `0`/`7`=Sunday..`6`=Saturday, exactly like every
    # crontab(5) reference — the APScheduler-native numbering difference is
    # translated away before a trigger is ever built, not merely documented.
    cron: str
    enabled: bool = True
    timezone: str = "America/Recife"
    crawl_reels: bool = False
    # Default ON: a scheduled crawl REPLACES a manual archive run that
    # already classifies images, so defaulting this off would silently lose
    # coverage the operator already had (image-only flyers with no caption
    # event-marker). An operator opts a target OUT explicitly for cheap mode.
    classify_images: bool = True
    initial_lookback: Optional[str] = None
    results_limit: Optional[int] = None
    notes: Optional[str] = None


class CrawlTargetPatch(BaseModel):
    kind: Optional[str] = None
    cron: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None
    crawl_reels: Optional[bool] = None
    classify_images: Optional[bool] = None
    initial_lookback: Optional[str] = None
    results_limit: Optional[int] = None
    notes: Optional[str] = None


class RunNowResult(BaseModel):
    """Shape changed in the same window it shipped in (#164) but before the
    console consumed it, so there is no compatibility concern: `started`
    replaces the old synchronous `outcome`/`credit_exhausted` pair, because
    the endpoint no longer waits to find out either of those — it returns
    the moment the crawl is scheduled to run in the background, or the
    moment it decides not to. `reason` is populated only when `started` is
    False, with a string the console can show verbatim (`already_running`,
    `skipped_budget`, `skipped_disabled`, `skipped_failures`)."""
    handle: str
    started: bool
    reason: Optional[str] = None


@router.get("", response_model=list[CrawlTargetOut])
def list_crawl_targets(enabled: Optional[bool] = None, kind: Optional[str] = None):
    dao = _dao()
    rows = dao.list_crawl_targets(enabled=enabled, kind=kind) or []
    return [_to_out(dao, r) for r in rows]


# Registered BEFORE the "/{handle}" routes below: FastAPI/Starlette matches
# routes in REGISTRATION ORDER, and "/{handle}" is a single path segment —
# exactly the shape of "/budget" too. Registered after it, a request for
# GET /admin/crawl-targets/budget would be swallowed by
# get_crawl_target(handle="budget") and returned as a 404 "crawl target not
# found" instead of ever reaching this code — the same trap
# admin_events_router.py's "/promoters"/"/review" routes already document
# and guard against for this exact reason. See
# tests/test_admin_crawl_router.py for the regression test.
@router.get("/budget", response_model=CrawlBudgetOut)
def get_crawl_budget():
    dao = _budget_dao()
    year_month = dao.current_year_month_utc()
    used = dao.get_month_count(year_month)
    limit = int(settings.crawl_monthly_result_budget)
    return CrawlBudgetOut(
        year_month=year_month, used=used, limit=limit,
        remaining=max(limit - used, 0),
        unit_cost_usd=float(settings.apify_instagram_post_cost_usd),
    )


@router.get("/{handle}", response_model=CrawlTargetOut)
def get_crawl_target(handle: str):
    dao = _dao()
    row = dao.get_crawl_target(normalize_handle(handle))
    if row is None:
        raise HTTPException(status_code=404, detail="crawl target not found")
    return _to_out(dao, row)


@router.post("", response_model=CrawlTargetOut, status_code=201)
def create_crawl_target(body: CrawlTargetCreate):
    dao = _dao()
    handle = normalize_handle(body.handle)
    if not handle:
        raise HTTPException(status_code=422, detail="handle is required")
    _require_valid_kind(body.kind)
    _require_valid_cron(body.cron)
    fields = body.model_dump(exclude={"handle"})
    row = dao.upsert_crawl_target(handle, fields)
    return _to_out(dao, row)


@router.patch("/{handle}", response_model=CrawlTargetOut)
def update_crawl_target(handle: str, body: CrawlTargetPatch):
    dao = _dao()
    handle = normalize_handle(handle)
    if dao.get_crawl_target(handle) is None:
        raise HTTPException(status_code=404, detail="crawl target not found")
    fields = body.model_dump(exclude_unset=True)
    _require_valid_kind(fields.get("kind"))
    _require_valid_cron(fields.get("cron"))
    row = dao.upsert_crawl_target(handle, fields)
    return _to_out(dao, row)


@router.delete("/{handle}", status_code=204)
def delete_crawl_target(handle: str):
    handle = normalize_handle(handle)
    if not _dao().delete_crawl_target(handle):
        raise HTTPException(status_code=404, detail="crawl target not found")


@router.post("/{handle}/run", response_model=RunNowResult)
async def run_crawl_target_now(handle: str, response: Response):
    """Starts the crawl in the background and returns immediately — never
    awaits the crawl itself (see the module docstring for why: a 3-month
    seed takes minutes, well past the console's proxy timeout). `202` when
    it actually starts; `200` with a `reason` when it correctly declines to
    (already running, disabled, past its failure threshold, or the monthly
    budget is spent) — none of those are HTTP-level errors, only `404`
    (unknown handle) is."""
    handle = normalize_handle(handle)
    if _dao().get_crawl_target(handle) is None:
        raise HTTPException(status_code=404, detail="crawl target not found")
    result = await _crawl_service().start_run(handle)
    response.status_code = 202 if result.get("started") else 200
    return RunNowResult(handle=handle, started=bool(result.get("started")), reason=result.get("reason"))
