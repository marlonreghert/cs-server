"""Admin trigger routes for on-demand enrichment jobs."""
import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Global container reference - set during startup
_container = None

# Track running jobs to prevent double-triggers
_running_jobs: dict[str, asyncio.Task] = {}


def set_container(container):
    """Set the DI container (called during startup)."""
    global _container
    _container = container
    logger.info("[AdminTriggerRouter] Container injected")


class TriggerResponse(BaseModel):
    status: str
    job: str
    message: str


# Map of job names to their execution logic
JOB_REGISTRY = {
    "venue_catalog": {
        "label": "Venue Catalog Fetch",
        "description": "Fetch venues from BestTime API for all default locations",
    },
    "live_forecast": {
        "label": "Live Forecast Refresh",
        "description": "Refresh live busyness forecasts for all cached venues",
    },
    "weekly_forecast": {
        "label": "Weekly Forecast Refresh",
        "description": "Refresh weekly forecast data for all cached venues",
    },
    "google_places": {
        "label": "Google Places Enrichment",
        "description": "Enrich venues with Google Places vibe attributes and business status",
    },
    "photos": {
        "label": "Photo Enrichment",
        "description": "Fetch venue photos from Google Places API",
    },
    "instagram": {
        "label": "Instagram Discovery",
        "description": "Discover Instagram handles for venues via Google Places + Apify",
    },
    "instagram_posts": {
        "label": "Instagram Posts Scraping",
        "description": "Scrape recent Instagram posts for venues with IG handles",
    },
    "menu_photos": {
        "label": "Menu Photo Enrichment",
        "description": "Fetch menu photos from Instagram highlights and Google Maps",
    },
    "menu_extraction": {
        "label": "Menu Extraction (GPT-4o)",
        "description": "Extract structured menu data from photos using OpenAI vision",
    },
    "vibe_classifier": {
        "label": "Vibe Classifier (AI)",
        "description": "Classify venue vibes from photos using 2-stage GPT pipeline",
    },
}


async def _run_job(job_name: str):
    """Execute an enrichment job by name."""
    c = _container
    start = time.perf_counter()

    if job_name == "venue_catalog":
        await c.venues_refresher_service.refresh_venues_by_filter_for_default_locations(
            fetch_and_cache_live=True
        )
    elif job_name == "live_forecast":
        await c.venues_refresher_service.refresh_live_forecasts_for_all_venues()
    elif job_name == "weekly_forecast":
        await c.venues_refresher_service.refresh_weekly_forecasts_for_all_venues()
    elif job_name == "google_places":
        if c.google_places_enrichment_service is None:
            raise ValueError("Google Places API not configured")
        await c.google_places_enrichment_service.enrich_all_venues()
    elif job_name == "photos":
        if c.photo_enrichment_service is None:
            raise ValueError("Photo enrichment not configured (missing Google Places API key)")
        await c.photo_enrichment_service.refresh_photos_for_venues()
    elif job_name == "instagram":
        if c.instagram_enrichment_service is None:
            raise ValueError("Instagram enrichment not configured (missing Apify API token)")
        await c.instagram_enrichment_service.enrich_all_venues()
    elif job_name == "instagram_posts":
        if c.instagram_posts_enrichment_service is None:
            raise ValueError("IG posts enrichment not configured (missing Apify API token)")
        await c.instagram_posts_enrichment_service.enrich_all_venues()
    elif job_name == "menu_photos":
        if c.menu_photo_enrichment_service is None:
            raise ValueError("Menu photo enrichment not configured (missing S3/Apify)")
        await c.menu_photo_enrichment_service.enrich_all_venues()
    elif job_name == "menu_extraction":
        if c.menu_extraction_service is None:
            raise ValueError("Menu extraction not configured (missing OpenAI/S3)")
        await c.menu_extraction_service.extract_all_venues()
    elif job_name == "vibe_classifier":
        if c.vibe_classifier_service is None:
            raise ValueError("Vibe classifier not configured (missing OpenAI API key)")
        await c.vibe_classifier_service.classify_all_venues()
    else:
        raise ValueError(f"Unknown job: {job_name}")

    duration = time.perf_counter() - start
    logger.info(f"[AdminTrigger] Job '{job_name}' completed in {duration:.1f}s")


@router.get("/jobs")
async def list_jobs():
    """List all available enrichment jobs and their current status."""
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")

    jobs = []
    for name, info in JOB_REGISTRY.items():
        # Check if service is available
        available = True
        if name == "google_places" and _container.google_places_enrichment_service is None:
            available = False
        elif name == "photos" and _container.photo_enrichment_service is None:
            available = False
        elif name == "instagram" and _container.instagram_enrichment_service is None:
            available = False
        elif name == "instagram_posts" and _container.instagram_posts_enrichment_service is None:
            available = False
        elif name == "menu_photos" and _container.menu_photo_enrichment_service is None:
            available = False
        elif name == "menu_extraction" and _container.menu_extraction_service is None:
            available = False
        elif name == "vibe_classifier" and _container.vibe_classifier_service is None:
            available = False

        # Check if currently running
        task = _running_jobs.get(name)
        running = task is not None and not task.done()

        jobs.append({
            "name": name,
            "label": info["label"],
            "description": info["description"],
            "available": available,
            "running": running,
        })

    return {"jobs": jobs}


@router.post("/trigger/{job_name}")
async def trigger_job(job_name: str):
    """Trigger an enrichment job to run in the background."""
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")

    if job_name not in JOB_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")

    # Check if already running
    existing = _running_jobs.get(job_name)
    if existing is not None and not existing.done():
        return TriggerResponse(
            status="already_running",
            job=job_name,
            message=f"{JOB_REGISTRY[job_name]['label']} is already running",
        )

    # Launch as background task
    async def _wrapper():
        try:
            await _run_job(job_name)
        except Exception as e:
            logger.error(f"[AdminTrigger] Job '{job_name}' failed: {e}")
        finally:
            _running_jobs.pop(job_name, None)

    task = asyncio.create_task(_wrapper())
    _running_jobs[job_name] = task

    return TriggerResponse(
        status="started",
        job=job_name,
        message=f"{JOB_REGISTRY[job_name]['label']} started in background",
    )


@router.post("/recount-discovery-points")
async def recount_discovery_points():
    """Recount venues per discovery point using GEORADIUS and update counters."""
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")

    try:
        points = _container.venues_refresher_service.recount_discovery_points()
        return {
            "status": "ok",
            "points": points,
            "message": f"Recounted {len(points)} discovery points",
        }
    except Exception as e:
        logger.error(f"[AdminTrigger] Recount discovery points failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
