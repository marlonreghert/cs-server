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
from app.routers import venue_router, set_venue_handler
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

    # Step 4: Start background jobs
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

# Register router at app creation time (before uvicorn starts)
app.include_router(venue_router)


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