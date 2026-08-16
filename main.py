"""Main entry point for cs-server Python application.

Startup is split into two phases for zero-downtime deploys:
1. Essential init (blocking): DI container + router injection
2. Server starts accepting requests (serves existing Redis data)
3. Enrichment pipelines run in background (photo, IG, etc.)
4. Scheduled background jobs run on their cron/interval triggers
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from app.config import Settings
from app.container import Container
from app.dao.datalake_writer import set_job_context as set_datalake_job_context
from app.services.pipeline_run_registry import (
    install_run_id_logging,
    new_run_id,
    run_scope,
)
from app.routers import venue_router, set_venue_handler, debug_router, set_debug_dependencies, admin_trigger_router, set_admin_container, engagement_router, set_engagement_service, internal_router, set_internal_container, admin_events_router, set_admin_events_container, admin_crawl_router, set_admin_crawl_container
from app.middleware import PrometheusMiddleware
from app.services.refresh_interval_watch import (
    WATCH_INTERVAL_SECONDS,
    RefreshIntervalWatcher,
)
from app.metrics import (
    BACKGROUND_JOB_RUNS_TOTAL,
    BACKGROUND_JOB_DURATION_SECONDS,
    BACKGROUND_JOB_LAST_RUN_TIMESTAMP,
    JOB_LOCK_REJECTED_TOTAL,
    REDIS_PROJECTION_VENUES,
    REDIS_PROJECTION_DEPRECATED_REMOVED_TOTAL,
)
from app.services import job_lock

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# Mask secrets (BestTime api_key_private, Google key=) that httpx + some clients
# would otherwise log in full request URLs/params.
from app.log_redaction import install_secret_redaction  # noqa: E402

install_secret_redaction()
# Stamp `job=<id>` onto every line emitted inside a pipeline run, so existing
# pipelines gain log correlation without editing a single log statement.
install_run_id_logging()
logger = logging.getLogger(__name__)

# Global container and scheduler
container: Container = None
scheduler: AsyncIOScheduler = None


def make_job(
    job_name: str,
    *,
    start_log: str,
    done_log,
    error_label: str,
    run,
    service_attr: "str | None" = None,
    disabled_log: "str | None" = None,
    require_container: bool = False,
    on_success=None,
    lock_name: "str | None" = None,
):
    """Build a scheduler-job coroutine with the shared instrumentation skeleton.

    Every produced job logs a start line, starts a perf timer, optionally skips
    (warn + return) when its backing service is absent, awaits the work, and
    records the same three background-job metrics with its own ``job_name`` on
    both success and error — so a new job cannot silently forget a metric or a
    guard. Behavior-preserving collapse of the eleven hand-rolled ``run_*_job``
    wrappers: metric names/labels, APScheduler job ids, and every log message
    are byte-identical to the originals.

    Args:
        job_name: the ``job_name`` metric label value (unchanged from before).
        start_log: INFO line emitted before the timer starts.
        done_log: INFO success line; a callable ``(result) -> str`` when the
            message embeds the run summary (``redis_projection``).
        error_label: names the job in the ERROR line
            ``[Scheduler] <error_label> failed: {e}``.
        run: ``async (container) -> result`` performing the actual work.
        service_attr: when set, skip (warn + return) if
            ``getattr(container, service_attr) is None`` — evaluated after the
            start log + timer start, matching the originals.
        disabled_log: WARNING line emitted when the ``service_attr`` guard trips.
        require_container: when True, return immediately (no log, no metric) if
            the global ``container`` is None — the ``redis_projection`` guard.
        on_success: optional ``(result) -> None`` hook for extra success-path
            metrics (``redis_projection``'s projection gauges), fired after the
            three job metrics and before the done log.
        lock_name: when set, the shared scheduler+admin concurrency guard
            (app/services/job_lock.py) key for this job. An admin trigger of
            the same job_name currently running holds this same name; if held,
            this run is skipped entirely (no log-start, no metrics) instead of
            doubling the paid BestTime/Google calls for the cycle. Released in
            a finally so a failed/disabled run never leaves it stuck.
    """
    async def _job():
        run_ctx = None
        if require_container and container is None:
            return
        if lock_name is not None and not job_lock.try_acquire(lock_name):
            logger.warning(
                f"[Scheduler] {error_label} skipped: '{lock_name}' already "
                "running (admin trigger in progress)"
            )
            JOB_LOCK_REJECTED_TOTAL.labels(job_name=lock_name, source="scheduler").inc()
            return
        try:
            logger.info(start_log)
            start_time = time.perf_counter()
            # One identity per run, shared by the data lake, the log filter and
            # the run registry — so a batch, a log line and a dashboard row all
            # name the SAME run. Previously this minted its own uuid here.
            run_id = new_run_id()
            set_datalake_job_context(job_name, run_id)
            run_ctx = run_scope(job_name, job_id=run_id)
            run_ctx.__enter__()
            if service_attr is not None and getattr(container, service_attr) is None:
                logger.warning(disabled_log)
                return
            try:
                result = await run(container)
                duration = time.perf_counter() - start_time
                BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
                BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
                BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
                if on_success is not None:
                    on_success(result)
                logger.info(done_log(result) if callable(done_log) else done_log)
            except Exception as e:
                duration = time.perf_counter() - start_time
                BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
                BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
                logger.error(f"[Scheduler] {error_label} failed: {e}")
                run_ctx.__exit__(type(e), e, None)
                run_ctx = None
        finally:
            if run_ctx is not None:
                run_ctx.__exit__(None, None, None)
            if lock_name is not None:
                job_lock.release(lock_name)

    return _job


def schedule(
    scheduler,
    *,
    enabled: bool,
    func,
    trigger,
    id: str,
    name: str,
    enabled_log: str,
    disabled_log: "str | None" = None,
) -> None:
    """Add a scheduled job or log why it stayed off — the add-or-log-disabled
    block repeated for every optional pipeline. Always-on jobs pass
    ``enabled=True`` and omit ``disabled_log`` (it is never emitted). The job
    id/name and log messages are unchanged from the inline blocks."""
    if enabled:
        scheduler.add_job(func, trigger=trigger, id=id, name=name, replace_existing=True)
        logger.info(enabled_log)
    else:
        logger.info(disabled_log)


run_venue_catalog_refresh_job = make_job(
    "venue_catalog_refresh",
    start_log="[Scheduler] Running VenueFilterMultiLocationJob",
    done_log="[Scheduler] VenueFilterMultiLocationJob completed",
    error_label="VenueFilterMultiLocationJob",
    run=lambda c: c.venues_refresher_service.refresh_venues_by_filter_for_default_locations(
        fetch_and_cache_live=True
    ),
)


run_live_forecast_refresh_job = make_job(
    "live_forecast_refresh",
    start_log="[Scheduler] Running LiveForecastRefreshJob",
    done_log="[Scheduler] LiveForecastRefreshJob completed",
    error_label="LiveForecastRefreshJob",
    run=lambda c: c.venues_refresher_service.refresh_live_forecasts_for_all_venues(),
    lock_name=job_lock.LIVE_FORECAST,
)


run_weekly_forecast_refresh_job = make_job(
    "weekly_forecast_refresh",
    start_log="[Scheduler] Running WeeklyForecastRefreshJob (Cron: Sunday 00:00)",
    done_log="[Scheduler] WeeklyForecastRefreshJob completed",
    error_label="WeeklyForecastRefreshJob",
    run=lambda c: c.venues_refresher_service.refresh_weekly_forecasts_for_all_venues(),
    lock_name=job_lock.WEEKLY_FORECAST,
)


run_google_places_enrichment_job = make_job(
    # Enriches vibe attributes + business status, soft-deprecating permanently
    # closed venues.
    "google_places_enrichment",
    start_log="[Scheduler] Running GooglePlacesEnrichmentJob",
    done_log="[Scheduler] GooglePlacesEnrichmentJob completed",
    error_label="GooglePlacesEnrichmentJob",
    service_attr="google_places_enrichment_service",
    disabled_log="[Scheduler] GooglePlacesEnrichmentJob skipped: "
    "Google Places API not configured",
    run=lambda c: c.google_places_enrichment_service.enrich_all_venues(),
    lock_name=job_lock.GOOGLE_PLACES,
)


run_photo_enrichment_job = make_job(
    "photo_enrichment",
    start_log="[Scheduler] Running PhotoEnrichmentJob",
    done_log="[Scheduler] PhotoEnrichmentJob completed",
    error_label="PhotoEnrichmentJob",
    service_attr="photo_enrichment_service",
    disabled_log="[Scheduler] PhotoEnrichmentJob skipped: "
    "Google Places API not configured",
    run=lambda c: c.photo_enrichment_service.refresh_photos_for_venues(),
)


run_instagram_enrichment_job = make_job(
    "instagram_enrichment",
    start_log="[Scheduler] Running InstagramEnrichmentJob",
    done_log="[Scheduler] InstagramEnrichmentJob completed",
    error_label="InstagramEnrichmentJob",
    service_attr="instagram_enrichment_service",
    disabled_log="[Scheduler] InstagramEnrichmentJob skipped: "
    "Apify API not configured",
    run=lambda c: c.instagram_enrichment_service.enrich_all_venues(),
)


run_ig_posts_enrichment_job = make_job(
    "ig_posts_enrichment",
    start_log="[Scheduler] Running IGPostsEnrichmentJob",
    done_log="[Scheduler] IGPostsEnrichmentJob completed",
    error_label="IGPostsEnrichmentJob",
    service_attr="instagram_posts_enrichment_service",
    disabled_log="[Scheduler] IGPostsEnrichmentJob skipped: "
    "Apify API not configured",
    run=lambda c: c.instagram_posts_enrichment_service.enrich_all_venues(),
)


run_menu_photo_enrichment_job = make_job(
    "menu_photo_enrichment",
    start_log="[Scheduler] Running MenuPhotoEnrichmentJob",
    done_log="[Scheduler] MenuPhotoEnrichmentJob completed",
    error_label="MenuPhotoEnrichmentJob",
    service_attr="menu_photo_enrichment_service",
    disabled_log="[Scheduler] MenuPhotoEnrichmentJob skipped: "
    "Menu photo enrichment not configured",
    run=lambda c: c.menu_photo_enrichment_service.enrich_all_venues(),
)


run_menu_extraction_job = make_job(
    "menu_extraction",
    start_log="[Scheduler] Running MenuExtractionJob",
    done_log="[Scheduler] MenuExtractionJob completed",
    error_label="MenuExtractionJob",
    service_attr="menu_extraction_service",
    disabled_log="[Scheduler] MenuExtractionJob skipped: "
    "Menu extraction not configured",
    run=lambda c: c.menu_extraction_service.extract_all_venues(),
)


run_vibe_classifier_job = make_job(
    "vibe_classifier",
    start_log="[Scheduler] Running VibeClassifierJob",
    done_log="[Scheduler] VibeClassifierJob completed",
    error_label="VibeClassifierJob",
    service_attr="vibe_classifier_service",
    disabled_log="[Scheduler] VibeClassifierJob skipped: "
    "Vibe classifier not configured",
    run=lambda c: c.vibe_classifier_service.classify_all_venues(),
)


async def _project_redis_from_rds(c) -> dict:
    """Run the projection body OFF the serving event loop (B0): it is synchronous
    + blocking (SQLAlchemy + Redis); running it inline on the AsyncIOScheduler
    loop would stall GET /v1/venues/nearby and /health for the whole run. The
    projector removes venues deprecated in RDS (B1) and counts the photo cache
    TTL down (B2). It is the sole Redis writer for pipeline data."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, c.redis_projection_service.rebuild_redis_from_rds
    )


def _record_projection_metrics(summary: dict) -> None:
    """Emit the projection-specific gauges after the shared job metrics (B1/B2)."""
    REDIS_PROJECTION_VENUES.set(summary.get("venues", 0))
    if summary.get("removed"):
        REDIS_PROJECTION_DEPRECATED_REMOVED_TOTAL.inc(summary["removed"])


run_redis_projection_job = make_job(
    "redis_projection",
    start_log="[Scheduler] Running RedisProjectionJob (off-loop)",
    done_log=lambda summary: f"[Scheduler] RedisProjectionJob completed: {summary}",
    error_label="RedisProjectionJob",
    run=_project_redis_from_rds,
    require_container=True,
    on_success=_record_projection_metrics,
    lock_name=job_lock.REBUILD_REDIS,
)


async def _detect_closed_venues(c):
    """Off-loop closure detection: flag venues whose newest review reports them
    permanently closed so the serving view drops them.

    Reads review text already stored in RDS (no upstream fetch) and writes only
    admin.venue_closure_signal — never a lifecycle change — so a reopened venue
    returns to serving on its own once newer evidence clears the signal."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, c.closure_detection_service.run)


run_closure_detection_job = make_job(
    "closure_detection",
    start_log="[Scheduler] Running ClosureDetectionJob (off-loop)",
    done_log=lambda summary: f"[Scheduler] ClosureDetectionJob completed: {summary}",
    error_label="ClosureDetectionJob",
    run=_detect_closed_venues,
    service_attr="closure_detection_service",
    require_container=True,
    disabled_log="[Scheduler] ClosureDetectionJob skipped: service not configured",
)


run_instagram_profile_photo_job = make_job(
    "instagram_profile_photos",
    start_log="[Scheduler] Running InstagramProfilePhotoJob",
    done_log=lambda summary: (
        f"[Scheduler] InstagramProfilePhotoJob completed: {summary}"
    ),
    error_label="InstagramProfilePhotoJob",
    run=lambda c: c.venue_profile_photo_service.run(),
    service_attr="venue_profile_photo_service",
    require_container=True,
    disabled_log=(
        "[Scheduler] InstagramProfilePhotoJob skipped: service not configured"
    ),
)


def register_refresh_jobs(scheduler, settings: Settings):
    """Register the BestTime refresh jobs (catalog discovery, live, weekly).

    Extracted from start_background_jobs so the scheduling policy is testable in
    isolation. Job 1 (catalog discovery) is scheduled only when discovery is
    enabled; live and weekly refresh are always scheduled.
    """
    # Job 1: Venue catalog refresh (discovery) — only when discovery is enabled.
    # Discovery spends BestTime's monthly unique-venue cap, so it is off by
    # default (see settings.discovery_enabled).
    schedule(
        scheduler,
        enabled=settings.discovery_enabled,
        func=run_venue_catalog_refresh_job,
        trigger=IntervalTrigger(minutes=settings.venues_catalog_refresh_minutes),
        id="venue_catalog_refresh",
        name="Venue Catalog Refresh (Multi-Location VenueFilter)",
        enabled_log=(
            f"[Scheduler] Scheduled venue catalog refresh every "
            f"{settings.venues_catalog_refresh_minutes} minutes"
        ),
        disabled_log=(
            "[Scheduler] Venue catalog discovery disabled "
            "(discovery_enabled=false); Job 1 not scheduled"
        ),
    )

    # Job 2: Live forecast refresh (always scheduled)
    schedule(
        scheduler,
        enabled=True,
        func=run_live_forecast_refresh_job,
        trigger=IntervalTrigger(minutes=settings.venues_live_refresh_minutes),
        id="live_forecast_refresh",
        name="Live Forecast Refresh",
        enabled_log=(
            f"[Scheduler] Scheduled live forecast refresh every "
            f"{settings.venues_live_refresh_minutes} minutes"
        ),
    )

    # Job 3: Weekly forecast refresh (always scheduled)
    # NOTE (found while building the crawl-target scheduler, plans/260809_
    # scheduled-incremental-instagram-crawl.md): `CronTrigger.from_crontab`
    # passes its day-of-week digit straight through to APScheduler's OWN
    # 0=Monday..6=Sunday numbering, with NO translation to standard Unix
    # cron's 0=Sunday..6=Saturday. `weekly_forecast_cron`'s default
    # "0 0 * * 0" — commented "Sundays at 00:00" below — is very likely
    # actually firing MONDAY 00:00 in production, despite its own comment.
    # Deliberately left AS-IS here: this is a live, already-scheduled job,
    # and changing which day it fires on is an operational decision for the
    # operator to make, not a side effect of an unrelated feature branch.
    # This site intentionally does NOT go through `app.services.
    # instagram_crawl_service.build_cron_trigger` (the day-of-week-safe
    # helper that feature introduces) — it keeps APScheduler-native
    # semantics on purpose, so the divergence stays visible rather than
    # being silently "fixed" into a different fire day out from under
    # anyone relying on the current (buggy, but current) Monday behavior.
    # See tests/test_scheduler.py::test_weekly_forecast_cron_currently_
    # fires_monday_not_sunday_apscheduler_native_quirk, which pins this.
    schedule(
        scheduler,
        enabled=True,
        func=run_weekly_forecast_refresh_job,
        trigger=CronTrigger.from_crontab(settings.weekly_forecast_cron),
        id="weekly_forecast_refresh",
        name="Weekly Forecast Refresh (Sunday 00:00)",
        enabled_log=(
            f"[Scheduler] Scheduled weekly forecast refresh with cron: "
            f"{settings.weekly_forecast_cron}"
        ),
    )


def register_crawl_schedule_sync(scheduler, settings: Settings) -> None:
    """Registers the periodic job that keeps the scheduler's dynamic
    per-target crawl jobs in sync with `events.crawl_target`
    (`CrawlScheduleSync`, app/services/crawl_schedule_sync.py). Applies the
    current registry once immediately (so a fresh deploy does not wait a
    full sync interval before an already-configured target's next fire),
    then re-syncs on `crawl_schedule_sync_interval_minutes`."""
    if not (settings.crawl_scheduler_enabled and container.instagram_crawl_service is not None):
        logger.info(
            "[Scheduler] Instagram crawl schedule sync disabled "
            "(CRAWL_SCHEDULER_ENABLED=false or missing Apify API token)"
        )
        return

    from app.services.crawl_schedule_sync import CrawlScheduleSync

    sync = CrawlScheduleSync(
        venue_dao=container.pipeline_repository,
        crawl_service=container.instagram_crawl_service,
        scheduler=scheduler,
    )
    sync.sync_once()
    schedule(
        scheduler,
        enabled=True,
        func=sync.run,
        trigger=IntervalTrigger(minutes=settings.crawl_schedule_sync_interval_minutes),
        id="crawl_schedule_sync",
        name="Instagram Crawl Schedule Sync",
        enabled_log=(
            f"[Scheduler] Scheduled Instagram crawl schedule sync every "
            f"{settings.crawl_schedule_sync_interval_minutes} minutes"
        ),
    )


def start_background_jobs(settings: Settings):
    """Start all background jobs using APScheduler."""
    global scheduler
    scheduler = AsyncIOScheduler()

    register_refresh_jobs(scheduler, settings)

    # Interval watch: applies the admin-tunable live refresh interval
    # (`admin_config:live_refresh_minutes`, written by vibesadmin) to the
    # running scheduler without a restart.
    refresh_interval_watcher = RefreshIntervalWatcher(
        redis_client=container.redis_client,
        scheduler=scheduler,
        default_minutes=settings.venues_live_refresh_minutes,
    )
    schedule(
        scheduler,
        enabled=True,
        func=refresh_interval_watcher.run,
        trigger=IntervalTrigger(seconds=WATCH_INTERVAL_SECONDS),
        id="refresh_interval_watch",
        name="Live Refresh Interval Watch",
        enabled_log=(
            f"[Scheduler] Scheduled live refresh interval watch every "
            f"{WATCH_INTERVAL_SECONDS} seconds"
        ),
    )

    # Job 4: Google Places enrichment (only if enabled and configured)
    schedule(
        scheduler,
        enabled=bool(settings.google_places_enrichment_enabled and settings.google_places_api_key),
        func=run_google_places_enrichment_job,
        trigger=CronTrigger.from_crontab(settings.google_places_enrichment_cron),
        id="google_places_enrichment",
        name="Google Places Enrichment (Daily 3 AM)",
        enabled_log=(
            f"[Scheduler] Scheduled Google Places enrichment with cron: "
            f"{settings.google_places_enrichment_cron}"
        ),
        disabled_log=(
            "[Scheduler] Google Places enrichment disabled "
            "(missing API key or disabled in config)"
        ),
    )

    # Job 5 (RETIRED): the catalog-wide photo pre-bake is intentionally NOT
    # scheduled. Photos are now resolved ON DEMAND per venue (fresh, keyless CDN
    # URLs cached briefly under venue_photos_fresh_v1:*) via
    # POST /internal/venues/{id}/photos/resolve. Pre-baking key-bearing /media
    # URLs for the whole catalog produced blank photos once Google rotated the
    # token faster than the ~5-day TTL. `run_photo_enrichment_job` and
    # PhotoEnrichmentService.refresh_photos_for_venues remain intact but dormant
    # (no scheduled/startup/admin trigger), mirroring the dormant-discovery
    # pattern. The legacy venue_photos_v1:* key + projection stay for Redis
    # compatibility.

    # Job 6: Instagram enrichment (only if enabled and configured)
    schedule(
        scheduler,
        enabled=bool(settings.instagram_enrichment_enabled and settings.apify_api_token),
        func=run_instagram_enrichment_job,
        trigger=CronTrigger.from_crontab(settings.instagram_enrichment_cron),
        id="instagram_enrichment",
        name="Instagram Enrichment (Weekly)",
        enabled_log=(
            f"[Scheduler] Scheduled Instagram enrichment with cron: "
            f"{settings.instagram_enrichment_cron}"
        ),
        disabled_log=(
            "[Scheduler] Instagram enrichment disabled "
            "(INSTAGRAM_ENRICHMENT_ENABLED=false or missing Apify API token)"
        ),
    )

    # Job 10: IG posts enrichment (only if enabled and configured)
    schedule(
        scheduler,
        enabled=bool(settings.ig_posts_enrichment_enabled and settings.apify_api_token),
        func=run_ig_posts_enrichment_job,
        trigger=CronTrigger.from_crontab(settings.ig_posts_enrichment_cron),
        id="ig_posts_enrichment",
        name="Instagram Posts Enrichment (Weekly)",
        enabled_log=(
            f"[Scheduler] Scheduled IG posts enrichment with cron: "
            f"{settings.ig_posts_enrichment_cron}"
        ),
        disabled_log=(
            "[Scheduler] IG posts enrichment disabled "
            "(IG_POSTS_ENRICHMENT_ENABLED=false or missing Apify API token)"
        ),
    )

    # Job 7: Menu photo enrichment (only if enabled and configured)
    schedule(
        scheduler,
        enabled=bool(
            settings.menu_enrichment_enabled
            and container.menu_photo_enrichment_service is not None
        ),
        func=run_menu_photo_enrichment_job,
        trigger=CronTrigger.from_crontab(settings.menu_enrichment_cron),
        id="menu_photo_enrichment",
        name=f"Menu Photo Enrichment (limit={settings.menu_enrichment_limit})",
        enabled_log=(
            f"[Scheduler] Scheduled menu photo enrichment with cron: "
            f"{settings.menu_enrichment_cron}"
        ),
        disabled_log=(
            "[Scheduler] Menu photo enrichment disabled "
            "(MENU_ENRICHMENT_ENABLED=false or missing dependencies)"
        ),
    )

    # Job 8: Menu extraction (only if enabled and configured)
    schedule(
        scheduler,
        enabled=bool(
            settings.menu_extraction_enabled
            and container.menu_extraction_service is not None
        ),
        func=run_menu_extraction_job,
        trigger=CronTrigger.from_crontab(settings.menu_extraction_cron),
        id="menu_extraction",
        name="Menu Data Extraction (OpenAI GPT-4o)",
        enabled_log=(
            f"[Scheduler] Scheduled menu extraction with cron: "
            f"{settings.menu_extraction_cron}"
        ),
        disabled_log=(
            "[Scheduler] Menu extraction disabled "
            "(MENU_EXTRACTION_ENABLED=false or missing dependencies)"
        ),
    )

    # Job 9: Vibe classifier (only if enabled and configured)
    schedule(
        scheduler,
        enabled=bool(
            settings.vibe_classifier_enabled
            and container.vibe_classifier_service is not None
        ),
        func=run_vibe_classifier_job,
        trigger=CronTrigger.from_crontab(settings.vibe_classifier_cron),
        id="vibe_classifier",
        name="Vibe Classifier (AI Photo Analysis)",
        enabled_log=(
            f"[Scheduler] Scheduled vibe classifier with cron: "
            f"{settings.vibe_classifier_cron}"
        ),
        disabled_log=(
            "[Scheduler] Vibe classifier disabled "
            "(VIBE_CLASSIFIER_ENABLED=false or missing dependencies)"
        ),
    )

    # Job 11: Redis projection (decoupling) — off-loop projector that re-asserts
    # the Redis serving projection from RDS, removes venues deprecated in RDS (B1),
    # and counts the photo cache TTL down (B2). It is the sole Redis writer for
    # pipeline data. Always scheduled.
    schedule(
        scheduler,
        enabled=True,
        func=run_redis_projection_job,
        trigger=IntervalTrigger(minutes=settings.redis_projection_minutes),
        id="redis_projection",
        name="Redis Projection (RDS -> Redis, off-loop)",
        enabled_log=(
            f"[Scheduler] Scheduled Redis projection every "
            f"{settings.redis_projection_minutes} minutes (off-loop)"
        ),
    )

    # Job 12: Closed-venue detection — flags venues whose newest review reports
    # permanent closure so the serving view drops them. OFF by default: a false
    # positive removes a live venue from serving, so it is enabled deliberately
    # after a dry run. Reviews change slowly, so the cadence is hours.
    schedule(
        scheduler,
        enabled=settings.closure_detection_enabled,
        func=run_closure_detection_job,
        trigger=IntervalTrigger(hours=settings.closure_detection_hours),
        id="closure_detection",
        name="Closed-venue detection (reviews -> serving exclusion)",
        enabled_log=(
            f"[Scheduler] Scheduled closed-venue detection every "
            f"{settings.closure_detection_hours} hours (off-loop)"
        ),
        disabled_log=(
            "[Scheduler] Closed-venue detection disabled "
            "(closure_detection_enabled=false)"
        ),
    )

    # Job 13: Instagram profile photos -> the venue list hero
    # (plans/260816_instagram-profile-photo-hero.md). OFF by default and
    # doubly gated: the service is only built when the media bucket AND the CDN
    # base are configured, and the run itself is inert unless
    # instagram_profile_photo_enabled is on. Both gates exist because the S3
    # write happens AFTER the Apify scrape is already paid for, so prod turns
    # this on only once infra/media/ has been applied and verified.
    #
    # Steady-state spend is bounded by the freshness gate, not by the cadence:
    # a daily tick over a 30-day refresh window re-scrapes roughly a thirtieth
    # of the catalog per run, and every venue it skips is skipped BEFORE the
    # billed call.
    schedule(
        scheduler,
        enabled=bool(
            settings.instagram_profile_photo_enabled
            and container.venue_profile_photo_service is not None
        ),
        func=run_instagram_profile_photo_job,
        trigger=IntervalTrigger(hours=settings.instagram_profile_photo_interval_hours),
        id="instagram_profile_photos",
        name="Instagram profile photos (venue list hero)",
        enabled_log=(
            f"[Scheduler] Scheduled Instagram profile photos every "
            f"{settings.instagram_profile_photo_interval_hours} hours"
        ),
        disabled_log=(
            "[Scheduler] Instagram profile photos disabled "
            "(instagram_profile_photo_enabled=false or missing dependencies)"
        ),
    )

    # Job 14: Scheduled Incremental Instagram Crawl — schedule sync
    # (plans/260809_scheduled-incremental-instagram-crawl.md §D). This does
    # NOT itself scrape anything: it periodically re-reads events.crawl_target
    # and reconciles the SCHEDULER'S OWN dynamic `crawl_target:<handle>` jobs
    # to match (one CronTrigger per enabled target, in the target's own
    # timezone), so an admin write to a target's cron/enabled flag takes
    # effect without a restart, the same guarantee refresh_interval_watch
    # already gives live_forecast_refresh. OFF by default (crawl_scheduler_
    # enabled=false) and inert without an Apify token, matching every other
    # optional Instagram pipeline registered above.
    register_crawl_schedule_sync(scheduler, settings)

    # Start scheduler
    scheduler.start()
    logger.info("[Scheduler] Background jobs started")


async def startup_essential(settings: Settings):
    """Essential initialization — must complete before serving requests.

    Only does DI container setup + router injection so the server can
    immediately serve data already persisted in Redis.
    """
    global container

    logger.info("[Main] Starting essential startup")

    # Initialize container (connects to Redis)
    logger.info("[Main] Initializing DI container")
    container = Container(settings)

    # Inject handler into router (routes already registered at app creation)
    logger.info("[Main] Injecting handler into router")
    set_venue_handler(container.venue_handler)
    logger.info("[Main] Handler injected successfully")

    # Inject dependencies for debug router
    set_debug_dependencies(container.pipeline_repository, container.google_places_api)

    # Inject container for admin trigger router
    set_admin_container(container)

    # Inject engagement service (favorites/hot_likes write-through API)
    set_engagement_service(container.engagement_service)

    # Inject container for the internal on-demand photo-resolve router.
    set_internal_container(container)

    # Inject container for the admin events review API.
    set_admin_events_container(container)

    # Inject container for the admin crawl-target CRUD + run-now API.
    set_admin_crawl_container(container)

    # Rebuild the eligibility serving mirror from its rows so a Redis flush before
    # this start does not leave filtering on the hardcoded defaults. Runs OFF the
    # event loop (blocking SQLAlchemy read, same pattern as the projector) so it
    # cannot stall the loop, and is degrade-safe; the periodic projector re-asserts
    # it thereafter.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, container.eligibility_rule_service.rehydrate_mirror)

    # Reconcile any venue-add job row still "running" from a PRIOR process's
    # lifetime (plans/260814_venue-add-job-rds-tracking.md) — deterministically
    # correct in this single-process deployment (job_lock.py's own documented
    # one-event-loop assumption): any row still "running" when THIS process
    # starts cannot belong to it. This is a pure status-metadata UPDATE
    # (admin.venue_add_job_run SET status='interrupted', ...) — it never
    # touches AddVenueHandler, BestTime, Google, or Instagram, and re-runs
    # nothing, so it does NOT conflict with startup_background_pipelines()'s
    # "no pipeline runs on startup" policy below (2026-07-01 incident: a
    # restart must never re-trigger paid work). Off the event loop (same
    # run_in_executor pattern as rehydrate_mirror above) and defensively
    # caught so an RDS hiccup during this one write degrades to "some old
    # rows stay stuck a bit longer" rather than "the server fails to boot".
    try:
        fixed = await loop.run_in_executor(
            None,
            container.venue_add_job_store.reconcile_orphaned,
            "process restarted while job was running",
        )
        logger.info(
            f"[Main] Reconciled {fixed} orphaned venue-add job(s) from a previous process"
        )
    except Exception as e:
        logger.error(f"[Main] Venue-add job reconciliation failed (non-fatal): {e}")

    logger.info("[Main] Essential startup completed — server is ready to serve")


async def startup_background_pipelines(settings: Settings):
    """No-op by design: **no pipeline runs on startup**.

    After the 2026-07-01 incident (a restart ran discovery and spent the scarce
    BestTime unique-venue quota), startup only serves the already-projected Redis
    data. All refresh/enrichment runs via the scheduled cron jobs
    (``register_refresh_jobs``) or explicit admin-panel triggers
    (``admin_trigger_router`` / ``JOB_REGISTRY``). The ``*_on_startup`` settings are
    intentionally dead — a stray ``*_on_startup=true`` must not re-trigger anything.
    The pipeline service methods remain intact for the cron/admin trigger paths.
    """
    logger.info(
        "[Main] No pipelines run on startup by design; refresh/enrichment happen "
        "via scheduled cron jobs or admin-panel triggers"
    )


async def shutdown_sequence():
    """Clean up resources on shutdown."""
    global container, scheduler

    logger.info("[Main] Starting shutdown sequence")

    if scheduler:
        logger.info("[Main] Stopping scheduler")
        scheduler.shutdown(wait=False)
        logger.info("[Main] Scheduler stopped")

    # Flush buffered archival records before the process goes away — after the
    # scheduler stops (no new records) and before the container tears down.
    if container is not None and getattr(container, "datalake_writer", None):
        logger.info("[Main] Flushing data lake writer")
        try:
            await container.datalake_writer.close()
        except Exception as e:
            logger.error(f"[Main] Data lake shutdown flush failed: {e}")

    if container:
        logger.info("[Main] Shutting down container")
        await container.shutdown()
        logger.info("[Main] Container shut down")

    logger.info("[Main] Shutdown sequence completed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown.

    The server starts serving immediately after essential init (DI + routes).
    Enrichment pipelines and scheduled jobs run in the background so existing
    Redis data can be served without delay.
    """
    settings = Settings()

    # Phase 1: Essential init (blocking) — server won't accept requests until done
    await startup_essential(settings)

    # Phase 1b: Start the data lake flusher, if archival is enabled. Failing to
    # start it must not stop the server — the lake is strictly additive.
    if container is not None and getattr(container, "datalake_writer", None):
        try:
            await container.datalake_writer.start()
        except Exception as e:
            logger.error(f"[Main] Data lake writer failed to start: {e}")

    # Phase 2: Start scheduled background jobs (cron: live/weekly refresh; discovery
    # stays gated off). This is the ONLY on-start scheduling path.
    logger.info("[Main] Starting periodic jobs")
    start_background_jobs(settings)

    # Phase 3: No pipeline runs on startup by design (log-only no-op). Refresh and
    # enrichment happen via the scheduled cron jobs above or admin-panel triggers.
    await startup_background_pipelines(settings)

    yield  # ← Server is now accepting requests

    # Shutdown
    await shutdown_sequence()


# Create FastAPI app
settings = Settings()
app = FastAPI(
    title="CS-Server API",
    description="Venue discovery and crowd tracking service",
    version="1.0.0",
    lifespan=lifespan,
)

# Add Prometheus metrics middleware
app.add_middleware(PrometheusMiddleware)

# Register routers at app creation time (before uvicorn starts)
app.include_router(venue_router)
app.include_router(debug_router)
app.include_router(admin_trigger_router)
app.include_router(engagement_router)
app.include_router(internal_router)
app.include_router(admin_events_router)
app.include_router(admin_crawl_router)


# Health check endpoint
@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "healthy"}


# Prometheus metrics endpoint
@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """Prometheus metrics endpoint for scraping."""
    return PlainTextResponse(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


if __name__ == "__main__":
    import uvicorn

    logger.info("[Main] Starting CS-Server")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.server_port,
        log_level=settings.log_level.lower(),
    )
