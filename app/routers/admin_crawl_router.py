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
`build_cron_trigger` for how this was found and fixed). `next_fire_at` below
goes through the SAME translation, so it is always consistent with what a
target will actually do, never a second, disagreeing interpretation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.instagram_crawl_service import (
    KIND_PROMOTER,
    KIND_VENUE,
    InvalidCrawlTargetConfig,
    build_cron_trigger,
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
    # does not have to parse a crontab string by hand.
    next_fire_at: Optional[datetime] = None


def _with_next_fire(row: dict) -> dict:
    """Best-effort next-fire time from the stored cron/timezone. Never
    raises — a target with a since-corrupted cron (should not happen; write
    time already validates it) reads as `next_fire_at: null` rather than
    breaking the whole list/get response."""
    out = dict(row)
    try:
        trigger = build_cron_trigger(row["cron"], timezone=row.get("timezone") or "America/Recife")
        out["next_fire_at"] = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    except Exception:
        out["next_fire_at"] = None
    return out


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
    handle: str
    outcome: Optional[str] = None
    credit_exhausted: bool = False


@router.get("", response_model=list[CrawlTargetOut])
def list_crawl_targets(enabled: Optional[bool] = None, kind: Optional[str] = None):
    rows = _dao().list_crawl_targets(enabled=enabled, kind=kind) or []
    return [_with_next_fire(r) for r in rows]


@router.get("/{handle}", response_model=CrawlTargetOut)
def get_crawl_target(handle: str):
    row = _dao().get_crawl_target(normalize_handle(handle))
    if row is None:
        raise HTTPException(status_code=404, detail="crawl target not found")
    return _with_next_fire(row)


@router.post("", response_model=CrawlTargetOut, status_code=201)
def create_crawl_target(body: CrawlTargetCreate):
    handle = normalize_handle(body.handle)
    if not handle:
        raise HTTPException(status_code=422, detail="handle is required")
    _require_valid_kind(body.kind)
    _require_valid_cron(body.cron)
    fields = body.model_dump(exclude={"handle"})
    row = _dao().upsert_crawl_target(handle, fields)
    return _with_next_fire(row)


@router.patch("/{handle}", response_model=CrawlTargetOut)
def update_crawl_target(handle: str, body: CrawlTargetPatch):
    handle = normalize_handle(handle)
    if _dao().get_crawl_target(handle) is None:
        raise HTTPException(status_code=404, detail="crawl target not found")
    fields = body.model_dump(exclude_unset=True)
    _require_valid_kind(fields.get("kind"))
    _require_valid_cron(fields.get("cron"))
    row = _dao().upsert_crawl_target(handle, fields)
    return _with_next_fire(row)


@router.delete("/{handle}", status_code=204)
def delete_crawl_target(handle: str):
    handle = normalize_handle(handle)
    if not _dao().delete_crawl_target(handle):
        raise HTTPException(status_code=404, detail="crawl target not found")


@router.post("/{handle}/run", response_model=RunNowResult)
async def run_crawl_target_now(handle: str):
    handle = normalize_handle(handle)
    if _dao().get_crawl_target(handle) is None:
        raise HTTPException(status_code=404, detail="crawl target not found")
    report = await _crawl_service().run_target(handle)
    return RunNowResult(
        handle=handle, outcome=report.get("outcome"),
        credit_exhausted=bool(report.get("credit_exhausted")),
    )
