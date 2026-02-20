"""Main entry point for cs-server Python application.

Implements exact startup sequence from Go main.go (lines 139-165):
1. Initialize DI container
2. Run initial venue discovery (with live forecasts)
3. Run initial live forecast refresh
4. Run initial weekly forecast refresh
5. Start scheduled background jobs
6. Start HTTP server with FastAPI
"""
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
from app.routers import venue_router, set_venue_handler, debug_router, set_debug_dependencies
from app.middleware import PrometheusMiddleware
from app.metrics import (
    BACKGROUND_JOB_RUNS_TOTAL,
    BACKGROUND_JOB_DURATION_SECONDS,
    BACKGROUND_JOB_LAST_RUN_TIMESTAMP,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global container and scheduler
container: Container = None
scheduler: AsyncIOScheduler = None


async def run_venue_catalog_refresh_job():
    """Background job: Refresh venue catalog for default locations."""
    job_name = "venue_catalog_refresh"
    logger.info("[Scheduler] Running VenueFilterMultiLocationJob")
    start_time = time.perf_counter()
    try:
        await container.venues_refresher_service.refresh_venues_by_filter_for_default_locations(
            fetch_and_cache_live=True
        )
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] VenueFilterMultiLocationJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] VenueFilterMultiLocationJob failed: {e}")


async def run_live_forecast_refresh_job():
    """Background job: Refresh live forecasts for all venues."""
    job_name = "live_forecast_refresh"
    logger.info("[Scheduler] Running LiveForecastRefreshJob")
    start_time = time.perf_counter()
    try:
        await container.venues_refresher_service.refresh_live_forecasts_for_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] LiveForecastRefreshJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] LiveForecastRefreshJob failed: {e}")


async def run_weekly_forecast_refresh_job():
    """Background job: Refresh weekly forecasts for all venues."""
    job_name = "weekly_forecast_refresh"
    logger.info("[Scheduler] Running WeeklyForecastRefreshJob (Cron: Sunday 00:00)")
    start_time = time.perf_counter()
    try:
        await container.venues_refresher_service.refresh_weekly_forecasts_for_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] WeeklyForecastRefreshJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] WeeklyForecastRefreshJob failed: {e}")


async def run_google_places_enrichment_job():
    """Background job: Enrich venues with data from Google Places API.

    This includes:
    - Vibe attributes (pet friendly, outdoor seating, etc.)
    - Business status checks (operational, temporarily/permanently closed)
    - Removal of permanently closed venues
    """
    job_name = "google_places_enrichment"
    logger.info("[Scheduler] Running GooglePlacesEnrichmentJob")
    start_time = time.perf_counter()

    # Check if vibe attributes service is available
    if container.google_places_enrichment_service is None:
        logger.warning(
            "[Scheduler] GooglePlacesEnrichmentJob skipped: "
            "Google Places API not configured"
        )
        return

    try:
        await container.google_places_enrichment_service.enrich_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] GooglePlacesEnrichmentJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] GooglePlacesEnrichmentJob failed: {e}")


async def run_photo_enrichment_job():
    """Background job: Enrich venues with photos from Google Places API."""
    job_name = "photo_enrichment"
    logger.info("[Scheduler] Running PhotoEnrichmentJob")
    start_time = time.perf_counter()

    # Check if photo enrichment service is available
    if container.photo_enrichment_service is None:
        logger.warning(
            "[Scheduler] PhotoEnrichmentJob skipped: "
            "Google Places API not configured"
        )
        return

    try:
        await container.photo_enrichment_service.refresh_photos_for_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] PhotoEnrichmentJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] PhotoEnrichmentJob failed: {e}")


async def run_instagram_enrichment_job():
    """Background job: Discover Instagram handles for venues using Apify."""
    job_name = "instagram_enrichment"
    logger.info("[Scheduler] Running InstagramEnrichmentJob")
    start_time = time.perf_counter()

    if container.instagram_enrichment_service is None:
        logger.warning(
            "[Scheduler] InstagramEnrichmentJob skipped: "
            "Apify API not configured"
        )
        return

    try:
        await container.instagram_enrichment_service.enrich_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] InstagramEnrichmentJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] InstagramEnrichmentJob failed: {e}")


async def run_ig_posts_enrichment_job():
    """Background job: Scrape recent Instagram posts for venues with IG handles."""
    job_name = "ig_posts_enrichment"
    logger.info("[Scheduler] Running IGPostsEnrichmentJob")
    start_time = time.perf_counter()

    if container.instagram_posts_enrichment_service is None:
        logger.warning(
            "[Scheduler] IGPostsEnrichmentJob skipped: "
            "Apify API not configured"
        )
        return

    try:
        await container.instagram_posts_enrichment_service.enrich_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] IGPostsEnrichmentJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] IGPostsEnrichmentJob failed: {e}")


async def run_menu_photo_enrichment_job():
    """Background job: Fetch menu photos from Google Maps via Apify and store on S3."""
    job_name = "menu_photo_enrichment"
    logger.info("[Scheduler] Running MenuPhotoEnrichmentJob")
    start_time = time.perf_counter()

    if container.menu_photo_enrichment_service is None:
        logger.warning(
            "[Scheduler] MenuPhotoEnrichmentJob skipped: "
            "Menu photo enrichment not configured"
        )
        return

    try:
        await container.menu_photo_enrichment_service.enrich_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] MenuPhotoEnrichmentJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] MenuPhotoEnrichmentJob failed: {e}")


async def run_menu_extraction_job():
    """Background job: Extract structured menu data from photos using OpenAI GPT-4o."""
    job_name = "menu_extraction"
    logger.info("[Scheduler] Running MenuExtractionJob")
    start_time = time.perf_counter()

    if container.menu_extraction_service is None:
        logger.warning(
            "[Scheduler] MenuExtractionJob skipped: "
            "Menu extraction not configured"
        )
        return

    try:
        await container.menu_extraction_service.extract_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] MenuExtractionJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] MenuExtractionJob failed: {e}")


async def run_vibe_classifier_job():
    """Background job: Classify venue vibes from photos using OpenAI Vision."""
    job_name = "vibe_classifier"
    logger.info("[Scheduler] Running VibeClassifierJob")
    start_time = time.perf_counter()

    if container.vibe_classifier_service is None:
        logger.warning(
            "[Scheduler] VibeClassifierJob skipped: "
            "Vibe classifier not configured"
        )
        return

    try:
        await container.vibe_classifier_service.classify_all_venues()
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="success").inc()
        BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=job_name).set_to_current_time()
        logger.info("[Scheduler] VibeClassifierJob completed")
    except Exception as e:
        duration = time.perf_counter() - start_time
        BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=job_name).observe(duration)
        BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=job_name, status="error").inc()
        logger.error(f"[Scheduler] VibeClassifierJob failed: {e}")


def start_background_jobs(settings: Settings):
    """Start all background jobs using APScheduler."""
    global scheduler
    scheduler = AsyncIOScheduler()

    # Job 1: Venue catalog refresh
    scheduler.add_job(
        run_venue_catalog_refresh_job,
        trigger=IntervalTrigger(minutes=settings.venues_catalog_refresh_minutes),
        id="venue_catalog_refresh",
        name="Venue Catalog Refresh (Multi-Location VenueFilter)",
        replace_existing=True,
    )
    logger.info(
        f"[Scheduler] Scheduled venue catalog refresh every "
        f"{settings.venues_catalog_refresh_minutes} minutes"
    )

    # Job 2: Live forecast refresh
    scheduler.add_job(
        run_live_forecast_refresh_job,
        trigger=IntervalTrigger(minutes=settings.venues_live_refresh_minutes),
        id="live_forecast_refresh",
        name="Live Forecast Refresh",
        replace_existing=True,
    )
    logger.info(
        f"[Scheduler] Scheduled live forecast refresh every "
        f"{settings.venues_live_refresh_minutes} minutes"
    )

    # Job 3: Weekly forecast refresh
    scheduler.add_job(
        run_weekly_forecast_refresh_job,
        trigger=CronTrigger.from_crontab(settings.weekly_forecast_cron),
        id="weekly_forecast_refresh",
        name="Weekly Forecast Refresh (Sunday 00:00)",
        replace_existing=True,
    )
    logger.info(
        f"[Scheduler] Scheduled weekly forecast refresh with cron: "
        f"{settings.weekly_forecast_cron}"
    )

    # Job 4: Google Places enrichment (only if enabled and configured)
    if settings.google_places_enrichment_enabled and settings.google_places_api_key:
        scheduler.add_job(
            run_google_places_enrichment_job,
            trigger=CronTrigger.from_crontab(settings.google_places_enrichment_cron),
            id="google_places_enrichment",
            name="Google Places Enrichment (Daily 3 AM)",
            replace_existing=True,
        )
        logger.info(
            f"[Scheduler] Scheduled Google Places enrichment with cron: "
            f"{settings.google_places_enrichment_cron}"
        )
    else:
        logger.info(
            "[Scheduler] Google Places enrichment disabled "
            "(missing API key or disabled in config)"
        )

    # Job 5: Photo enrichment (only if enabled and configured)
    if settings.photo_enrichment_enabled and settings.google_places_api_key:
        scheduler.add_job(
            run_photo_enrichment_job,
            trigger=CronTrigger.from_crontab(settings.google_places_enrichment_cron),  # Same schedule as enrichment
            id="photo_enrichment",
            name=f"Photo Enrichment (limit={settings.photo_enrichment_limit})",
            replace_existing=True,
        )
        logger.info(
            f"[Scheduler] Scheduled photo enrichment with limit={settings.photo_enrichment_limit}, "
            f"photos_per_venue={settings.photos_per_venue}"
        )
    else:
        logger.info(
            "[Scheduler] Photo enrichment disabled "
            "(PHOTO_ENRICHMENT_ENABLED=false or missing API key)"
        )

    # Job 6: Instagram enrichment (only if enabled and configured)
    if settings.instagram_enrichment_enabled and settings.apify_api_token:
        scheduler.add_job(
            run_instagram_enrichment_job,
            trigger=CronTrigger.from_crontab(settings.instagram_enrichment_cron),
            id="instagram_enrichment",
            name="Instagram Enrichment (Weekly)",
            replace_existing=True,
        )
        logger.info(
            f"[Scheduler] Scheduled Instagram enrichment with cron: "
            f"{settings.instagram_enrichment_cron}"
        )
    else:
        logger.info(
            "[Scheduler] Instagram enrichment disabled "
            "(INSTAGRAM_ENRICHMENT_ENABLED=false or missing Apify API token)"
        )

    # Job 10: IG posts enrichment (only if enabled and configured)
    if settings.ig_posts_enrichment_enabled and settings.apify_api_token:
        scheduler.add_job(
            run_ig_posts_enrichment_job,
            trigger=CronTrigger.from_crontab(settings.ig_posts_enrichment_cron),
            id="ig_posts_enrichment",
            name="Instagram Posts Enrichment (Weekly)",
            replace_existing=True,
        )
        logger.info(
            f"[Scheduler] Scheduled IG posts enrichment with cron: "
            f"{settings.ig_posts_enrichment_cron}"
        )
    else:
        logger.info(
            "[Scheduler] IG posts enrichment disabled "
            "(IG_POSTS_ENRICHMENT_ENABLED=false or missing Apify API token)"
        )

    # Job 7: Menu photo enrichment (only if enabled and configured)
    if settings.menu_enrichment_enabled and container.menu_photo_enrichment_service is not None:
        scheduler.add_job(
            run_menu_photo_enrichment_job,
            trigger=CronTrigger.from_crontab(settings.menu_enrichment_cron),
            id="menu_photo_enrichment",
            name=f"Menu Photo Enrichment (limit={settings.menu_enrichment_limit})",
            replace_existing=True,
        )
        logger.info(
            f"[Scheduler] Scheduled menu photo enrichment with cron: "
            f"{settings.menu_enrichment_cron}"
        )
    else:
        logger.info(
            "[Scheduler] Menu photo enrichment disabled "
            "(MENU_ENRICHMENT_ENABLED=false or missing dependencies)"
        )

    # Job 8: Menu extraction (only if enabled and configured)
    if settings.menu_extraction_enabled and container.menu_extraction_service is not None:
        scheduler.add_job(
            run_menu_extraction_job,
            trigger=CronTrigger.from_crontab(settings.menu_extraction_cron),
            id="menu_extraction",
            name="Menu Data Extraction (OpenAI GPT-4o)",
            replace_existing=True,
        )
        logger.info(
            f"[Scheduler] Scheduled menu extraction with cron: "
            f"{settings.menu_extraction_cron}"
        )
    else:
        logger.info(
            "[Scheduler] Menu extraction disabled "
            "(MENU_EXTRACTION_ENABLED=false or missing dependencies)"
        )

    # Job 9: Vibe classifier (only if enabled and configured)
    if settings.vibe_classifier_enabled and container.vibe_classifier_service is not None:
        scheduler.add_job(
            run_vibe_classifier_job,
            trigger=CronTrigger.from_crontab(settings.vibe_classifier_cron),
            id="vibe_classifier",
            name="Vibe Classifier (AI Photo Analysis)",
            replace_existing=True,
        )
        logger.info(
            f"[Scheduler] Scheduled vibe classifier with cron: "
            f"{settings.vibe_classifier_cron}"
        )
    else:
        logger.info(
            "[Scheduler] Vibe classifier disabled "
            "(VIBE_CLASSIFIER_ENABLED=false or missing dependencies)"
        )

    # Start scheduler
    scheduler.start()
    logger.info("[Scheduler] Background jobs started")


async def startup_sequence(settings: Settings):
    """Run initial data loads before starting jobs.

    Implements exact sequence from Go main.go.
    """
    global container

    logger.info("[Main] Starting startup sequence")

    # Initialize container
    logger.info("[Main] Initializing DI container")
    container = Container(settings)

    # Inject handler into router (routes already registered at app creation)
    logger.info("[Main] Injecting handler into router")
    set_venue_handler(container.venue_handler)
    logger.info("[Main] Handler injected successfully")

    # Inject dependencies for debug router
    set_debug_dependencies(container.redis_venue_dao, container.google_places_api)

    # Check if we should run initial refresh
    if settings.refresh_on_startup:
        # Step 1: Initial venue discovery (with live forecasts)
        logger.info("[Main] Refreshing venues data (initial load)")
        try:
            await container.venues_refresher_service.refresh_venues_by_filter_for_default_locations(
                fetch_and_cache_live=True
            )
            logger.info("[Main] Initial venue refresh completed")
        except Exception as e:
            logger.error(f"[Main] Initial venue refresh failed: {e}")

        # Step 2: Initial live forecast refresh
        logger.info("[Main] Refreshing venues live forecast (initial load)")
        try:
            await container.venues_refresher_service.refresh_live_forecasts_for_all_venues()
            logger.info("[Main] Initial live forecast refresh completed")
        except Exception as e:
            logger.error(f"[Main] Initial live forecast refresh failed: {e}")

        # Step 3: Initial weekly forecast refresh
        logger.info("[Main] Refreshing weekly forecasts (initial load)")
        try:
            await container.venues_refresher_service.refresh_weekly_forecasts_for_all_venues()
            logger.info("[Main] Initial weekly forecast refresh completed")
        except Exception as e:
            logger.error(f"[Main] Initial weekly forecast refresh failed: {e}")
    else:
        logger.info("[Main] Skipping initial refresh (REFRESH_ON_STARTUP=false)")

    # Step 4: Initial Google Places enrichment (if enabled)
    if (
        settings.google_places_enrichment_on_startup
        and settings.google_places_api_key
        and container.google_places_enrichment_service is not None
    ):
        # Force refresh if permanently closed removal is enabled
        # This ensures we re-check all venues for permanently closed status
        force_refresh = settings.remove_permanently_closed_venues
        if force_refresh:
            logger.info(
                "[Main] Running Google Places enrichment with force_refresh=True "
                "(remove_permanently_closed_venues is enabled)"
            )
        else:
            logger.info("[Main] Running Google Places enrichment (initial load)")
        try:
            await container.google_places_enrichment_service.enrich_all_venues(
                force_refresh=force_refresh
            )
            logger.info("[Main] Initial Google Places enrichment completed")
        except Exception as e:
            logger.error(f"[Main] Initial Google Places enrichment failed: {e}")
    else:
        logger.info("[Main] Skipping initial Google Places enrichment")

    # Step 5: Initial photo enrichment (if enabled)
    if (
        settings.photo_enrichment_on_startup
        and settings.google_places_api_key
        and container.photo_enrichment_service is not None
    ):
        logger.info("[Main] Enriching venues with photos (initial load)")
        try:
            await container.photo_enrichment_service.refresh_photos_for_venues()
            logger.info("[Main] Initial photo enrichment completed")
        except Exception as e:
            logger.error(f"[Main] Initial photo enrichment failed: {e}")
    else:
        logger.info("[Main] Skipping initial photo enrichment")

    # Step 6: Initial Instagram enrichment (if enabled)
    if (
        settings.instagram_enrichment_on_startup
        and settings.apify_api_token
        and container.instagram_enrichment_service is not None
    ):
        logger.info("[Main] Running Instagram enrichment (initial load)")
        try:
            await container.instagram_enrichment_service.enrich_all_venues()
            logger.info("[Main] Initial Instagram enrichment completed")
        except Exception as e:
            logger.error(f"[Main] Initial Instagram enrichment failed: {e}")
    else:
        logger.info("[Main] Skipping initial Instagram enrichment")

    # Step 6b: Initial IG posts enrichment (if enabled)
    if (
        settings.ig_posts_enrichment_on_startup
        and settings.apify_api_token
        and container.instagram_posts_enrichment_service is not None
    ):
        logger.info("[Main] Running IG posts enrichment (initial load)")
        try:
            await container.instagram_posts_enrichment_service.enrich_all_venues()
            logger.info("[Main] Initial IG posts enrichment completed")
        except Exception as e:
            logger.error(f"[Main] Initial IG posts enrichment failed: {e}")
    else:
        logger.info("[Main] Skipping initial IG posts enrichment")

    # Step 7: Initial menu photo enrichment (if enabled)
    if (
        settings.menu_enrichment_on_startup
        and container.menu_photo_enrichment_service is not None
    ):
        logger.info("[Main] Running menu photo enrichment (initial load)")
        try:
            await container.menu_photo_enrichment_service.enrich_all_venues()
            logger.info("[Main] Initial menu photo enrichment completed")
        except Exception as e:
            logger.error(f"[Main] Initial menu photo enrichment failed: {e}")
    else:
        logger.info("[Main] Skipping initial menu photo enrichment")

    # Step 8: Initial menu extraction (if enabled)
    if (
        settings.menu_extraction_on_startup
        and container.menu_extraction_service is not None
    ):
        logger.info("[Main] Running menu extraction (initial load)")
        try:
            await container.menu_extraction_service.extract_all_venues()
            logger.info("[Main] Initial menu extraction completed")
        except Exception as e:
            logger.error(f"[Main] Initial menu extraction failed: {e}")
    else:
        logger.info("[Main] Skipping initial menu extraction")

    # Step 9: Initial vibe classification (if enabled)
    if (
        settings.vibe_classifier_on_startup
        and container.vibe_classifier_service is not None
    ):
        logger.info("[Main] Running vibe classification (initial load)")
        try:
            await container.vibe_classifier_service.classify_all_venues()
            logger.info("[Main] Initial vibe classification completed")
        except Exception as e:
            logger.error(f"[Main] Initial vibe classification failed: {e}")
    else:
        logger.info("[Main] Skipping initial vibe classification")

    # Step 10: Start background jobs
    logger.info("[Main] Starting periodic jobs")
    start_background_jobs(settings)

    logger.info("[Main] Startup sequence completed")


async def shutdown_sequence():
    """Clean up resources on shutdown."""
    global container, scheduler

    logger.info("[Main] Starting shutdown sequence")

    if scheduler:
        logger.info("[Main] Stopping scheduler")
        scheduler.shutdown(wait=False)
        logger.info("[Main] Scheduler stopped")

    if container:
        logger.info("[Main] Shutting down container")
        await container.shutdown()
        logger.info("[Main] Container shut down")

    logger.info("[Main] Shutdown sequence completed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown."""
    # Startup
    settings = Settings()
    await startup_sequence(settings)
    yield
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