"""Admin trigger routes for on-demand enrichment jobs."""
import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Body, Query, Response
from pydantic import BaseModel

from app.handlers.add_venue_handler import (
    AddVenueHandler,
    AddVenueByAddressRequest,
)
from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    EligibilityConfig,
    load_eligibility_config,
)

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
        "default_config": {"force_refresh": False},
    },
    "photos": {
        "label": "Photo Enrichment",
        "description": "Fetch venue photos from Google Places API",
        "default_config": {"limit": 200, "photos_per_venue": 5},
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
    "instagram_validate": {
        "label": "Instagram Handle Validation",
        "description": "Check all cached Instagram handles and remove invalid ones (404 profiles)",
    },
    "inventory_sync": {
        "label": "BestTime Inventory Sync",
        "description": "Pull every venue in our BestTime account inventory into Redis. Free — does not spend the monthly new-venue budget.",
    },
    "venue_eligibility": {
        "label": "Venue Eligibility Sweep",
        "description": "Soft-delete ineligible venues (drugstores, markets, churches, empty names, blocked Google types) with a rejection reason. Cache-first — makes no new Google calls.",
    },
    "backfill_rds": {
        "label": "Backfill RDS from Redis (one-time)",
        "description": "Import the current Redis dataset into RDS as the system of record (venues first, then enrichment). Idempotent. Run once after enabling RDS.",
    },
    "rebuild_redis": {
        "label": "Rebuild Redis from RDS",
        "description": "Reconstruct the Redis serving projection (incl. the geo index and live busyness) from RDS. Disaster recovery / Redis warm.",
    },
}


async def _run_job(job_name: str, config: Optional[dict] = None):
    """Execute an enrichment job by name with optional config overrides."""
    c = _container
    cfg = config or {}
    force = cfg.get("force_refresh", False)
    limit = cfg.get("limit")
    start = time.perf_counter()

    if job_name == "venue_catalog":
        await c.venues_refresher_service.refresh_venues_by_filter_for_default_locations(
            fetch_and_cache_live=True
        )
    elif job_name == "inventory_sync":
        await c.venues_refresher_service.sync_account_inventory_to_redis()
    elif job_name == "venue_eligibility":
        await c.venues_refresher_service.run_eligibility_sweep()
    elif job_name == "backfill_rds":
        if getattr(c, "rds_store", None) is None:
            raise ValueError("RDS not enabled (set rds_enabled=true)")
        c.redis_projection_service.backfill_rds_from_redis()
    elif job_name == "rebuild_redis":
        if getattr(c, "rds_store", None) is None:
            raise ValueError("RDS not enabled (set rds_enabled=true)")
        c.redis_projection_service.rebuild_redis_from_rds()
    elif job_name == "live_forecast":
        await c.venues_refresher_service.refresh_live_forecasts_for_all_venues()
    elif job_name == "weekly_forecast":
        await c.venues_refresher_service.refresh_weekly_forecasts_for_all_venues()
    elif job_name == "google_places":
        if c.google_places_enrichment_service is None:
            raise ValueError("Google Places API not configured")
        await c.google_places_enrichment_service.enrich_all_venues(force_refresh=force)
    elif job_name == "photos":
        if c.photo_enrichment_service is None:
            raise ValueError("Photo enrichment not configured (missing Google Places API key)")
        await c.photo_enrichment_service.refresh_photos_for_venues(
            limit=limit,
            max_photos_per_venue=cfg.get("photos_per_venue"),
        )
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
    elif job_name == "instagram_validate":
        if c.google_places_enrichment_service is None:
            raise ValueError("Google Places enrichment not configured")
        await c.google_places_enrichment_service.validate_cached_instagram_handles()
    else:
        raise ValueError(f"Unknown job: {job_name}")

    duration = time.perf_counter() - start
    logger.info(f"[AdminTrigger] Job '{job_name}' completed in {duration:.1f}s (config={cfg})")


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
        elif name == "instagram_validate" and _container.google_places_enrichment_service is None:
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
            "default_config": info.get("default_config"),
        })

    return {"jobs": jobs}


@router.post("/trigger/{job_name}")
async def trigger_job(job_name: str, config: Optional[dict] = None):
    """Trigger an enrichment job to run in the background.

    Optional JSON body with config overrides, e.g.:
    - {"force_refresh": true} — re-process already cached venues
    - {"limit": 50} — override the default venue limit
    """
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
            await _run_job(job_name, config=config)
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


@router.post("/venues/by-address")
async def add_venue_by_address(request: AddVenueByAddressRequest, response: Response):
    """Register a venue in our BestTime account inventory by name + address.

    Body: AddVenueByAddressRequest. See app/handlers/add_venue_handler.py for
    the full status-code matrix.
    """
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    if getattr(_container, "add_venue_handler", None) is None:
        raise HTTPException(
            status_code=503,
            detail="add-venue handler not configured",
        )
    handler: AddVenueHandler = _container.add_venue_handler
    outcome = await handler.add(request)
    response.status_code = outcome.status_code
    return outcome.body


@router.get("/venues/monthly-budget")
async def get_monthly_budget():
    """Return the current state of the monthly new-venue budget."""
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    budget = getattr(_container, "venue_budget_service", None)
    if budget is None:
        raise HTTPException(
            status_code=503,
            detail="venue budget service not configured",
        )
    snap = budget.get_snapshot()
    return {
        "quota": snap.quota,
        "manual_reserve": snap.manual_reserve,
        "month_counter": snap.month_counter,
        "year_month": snap.year_month,
        "discovery_effective_cap_remaining": snap.discovery_effective_cap_remaining,
        "manual_add_available": snap.manual_add_available,
    }


def _get_venue_dao_from_container():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    venue_dao = getattr(_container, "venue_dao", None) or getattr(
        _container, "redis_venue_dao", None
    )
    if venue_dao is None:
        raise HTTPException(status_code=503, detail="venue DAO not configured")
    return venue_dao


@router.get("/venues/eligibility-config")
async def get_eligibility_config():
    """Return the active venue-eligibility block-lists for the admin panel.

    Reports whether the active config is the Redis admin override or the
    built-in defaults so operators can see what is in effect.
    """
    venue_dao = _get_venue_dao_from_container()
    config = load_eligibility_config(getattr(venue_dao, "client", None))
    return config.to_public_dict()


@router.post("/venues/eligibility-config")
async def update_eligibility_config(config: dict = Body(...)):
    """Update the venue-eligibility block-lists (admin-tunable, no redeploy).

    Validates that each provided field is a list of strings, persists the
    override to Redis, and returns the resulting active config. Invalid bodies
    are rejected with HTTP 400 and the active config is left unchanged.

    Note: tightening the blocked lists causes the next eligibility sweep to
    soft-delete more venues, which is one-way in V1 (no restore).
    """
    venue_dao = _get_venue_dao_from_container()
    try:
        validated = EligibilityConfig.from_dict(config, from_admin_override=True)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid eligibility config: {e}")

    client = getattr(venue_dao, "client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="venue DAO client not configured")
    try:
        client.set(ADMIN_CONFIG_ELIGIBILITY_KEY, json.dumps(config))
    except Exception as e:
        logger.error(f"[AdminTrigger] Failed to persist eligibility config: {e}")
        raise HTTPException(status_code=500, detail="failed to persist eligibility config")

    return validated.to_public_dict()


def _venue_cache_flags(venue_dao, venue_id: str) -> dict[str, bool]:
    weekly_forecast = False
    for day_int in range(7):
        try:
            if venue_dao.get_week_raw_forecast(venue_id, day_int) is not None:
                weekly_forecast = True
                break
        except Exception:
            continue

    return {
        "live_forecast": venue_dao.get_live_forecast(venue_id) is not None,
        "weekly_forecast": weekly_forecast,
        "vibe_attributes": venue_dao.get_vibe_attributes(venue_id) is not None,
        "photos": bool(venue_dao.get_venue_photos(venue_id)),
        "opening_hours": venue_dao.get_opening_hours(venue_id) is not None,
        "instagram": venue_dao.get_venue_instagram(venue_id) is not None,
        "reviews": venue_dao.get_venue_reviews(venue_id) is not None,
        "menu_photos": venue_dao.get_venue_menu_photos(venue_id) is not None,
        "menu_data": venue_dao.get_venue_menu_data(venue_id) is not None,
        "vibe_profile": venue_dao.get_venue_vibe_profile(venue_id) is not None,
    }


@router.get("/venues/inventory")
async def list_venue_inventory(
    status: str = Query("active", pattern="^(active|deprecated|all)$"),
    q: Optional[str] = Query(None, description="Case-insensitive venue name/address search"),
    limit: int = Query(50, ge=1, le=250),
    cursor: Optional[str] = Query(None, description="Offset cursor from previous response"),
):
    """List active/deprecated venues for the vibes_bot admin panel."""
    venue_dao = _get_venue_dao_from_container()
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        raise HTTPException(status_code=400, detail="cursor must be an integer offset")

    try:
        all_venues = venue_dao.list_all_venues()
        active_count = sum(1 for venue in all_venues if venue.is_active())
        deprecated_count = len(all_venues) - active_count

        if status == "active":
            venues = [venue for venue in all_venues if venue.is_active()]
        elif status == "deprecated":
            venues = [venue for venue in all_venues if venue.is_deprecated()]
        else:
            venues = all_venues

        if q:
            needle = q.lower()
            venues = [
                venue
                for venue in venues
                if needle in (venue.venue_name or "").lower()
                or needle in (venue.venue_address or "").lower()
                or needle in (venue.venue_id or "").lower()
            ]

        venues.sort(key=lambda venue: (venue.venue_name or "", venue.venue_id or ""))
        page = venues[offset: offset + limit]
        next_offset = offset + limit
        next_cursor = str(next_offset) if next_offset < len(venues) else None

        return {
            "items": [
                {
                    "venue_id": venue.venue_id,
                    "venue_name": venue.venue_name,
                    "venue_address": venue.venue_address,
                    "venue_lat": venue.venue_lat,
                    "venue_lng": venue.venue_lng,
                    "lifecycle_status": venue.lifecycle_status,
                    "deprecated_reason": venue.deprecated_reason,
                    "deprecated_source": venue.deprecated_source,
                    "deprecated_at": (
                        venue.deprecated_at.isoformat() if venue.deprecated_at else None
                    ),
                    "google_business_status": venue.google_business_status,
                    "cache_flags": _venue_cache_flags(venue_dao, venue.venue_id),
                }
                for venue in page
            ],
            "next_cursor": next_cursor,
            "counts": {
                "active": active_count,
                "deprecated": deprecated_count,
                "total": len(all_venues),
                "filtered": len(venues),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AdminTrigger] Venue inventory listing failed: {e}")
        raise HTTPException(status_code=500, detail="venue inventory listing failed")


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


@router.get("/venue-type-breakdown")
async def venue_type_breakdown():
    """Get a breakdown of all venues by BestTime type and Google Places type."""
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")

    try:
        venue_dao = _container.venue_dao
        all_ids = venue_dao.list_all_venue_ids()

        besttime_types: dict[str, int] = {}
        google_types: dict[str, int] = {}
        total = 0
        with_google_type = 0

        for vid in all_ids:
            venue = venue_dao.get_venue(vid)
            if not venue:
                continue
            total += 1

            bt = venue.venue_type or "unknown"
            besttime_types[bt] = besttime_types.get(bt, 0) + 1

            # Check Google Places type from vibe attributes
            vibe_attrs = venue_dao.get_vibe_attributes(vid)
            if vibe_attrs and vibe_attrs.google_primary_type:
                gt = vibe_attrs.google_primary_type
                google_types[gt] = google_types.get(gt, 0) + 1
                with_google_type += 1

        return {
            "total_venues": total,
            "with_google_type": with_google_type,
            "besttime_types": dict(sorted(besttime_types.items(), key=lambda x: -x[1])),
            "google_places_types": dict(sorted(google_types.items(), key=lambda x: -x[1])),
        }
    except Exception as e:
        logger.error(f"[AdminTrigger] Venue type breakdown failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
