"""Admin trigger routes for on-demand enrichment jobs."""
import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Body, Query, Response
from pydantic import BaseModel, Field

from app.handlers.add_venue_handler import (
    AddVenueHandler,
    AddVenueByAddressRequest,
)
from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    ADMIN_CONFIG_GEOFENCE_KEY,
    DEFAULT_GEO_FENCE,
    EligibilityConfig,
    load_eligibility_config,
    validate_geo_fence,
)
from app.services.admin_config_service import AdminConfigService
from app.services.eligibility_rules import EligibilityRuleService

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


# Map of job names to their execution logic.
#
# `venue_catalog` (venue-filter discovery) is intentionally ABSENT: discovery is
# dormant after the 2026-07-01 incident (no startup, no scheduled job, no admin
# trigger). Triggering it now returns the standard 404 "Unknown job". The refresher
# method (`refresh_venues_by_filter_for_default_locations`) remains for future reuse
# but has no reachable trigger path.
JOB_REGISTRY = {
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
    "google_places_backfill": {
        "label": "Google Places Pending Backfill",
        "description": "One-time, idempotent Google-only enrichment of PENDING venues "
        "(active, no vibe attributes). Skips enriched + no-match venues; no BestTime call.",
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

    if job_name == "inventory_sync":
        await c.venues_refresher_service.sync_account_inventory_to_redis()
    elif job_name == "rebuild_redis":
        # Off-loop (B0): the projection body is synchronous + blocking; running it
        # inline would stall /v1/venues/nearby and /health for the whole run.
        await asyncio.get_event_loop().run_in_executor(
            None, c.redis_projection_service.rebuild_redis_from_rds
        )
    elif job_name == "live_forecast":
        await c.venues_refresher_service.refresh_live_forecasts_for_all_venues()
    elif job_name == "weekly_forecast":
        await c.venues_refresher_service.refresh_weekly_forecasts_for_all_venues()
    elif job_name == "google_places":
        if c.google_places_enrichment_service is None:
            raise ValueError("Google Places API not configured")
        await c.google_places_enrichment_service.enrich_all_venues(force_refresh=force)
    elif job_name == "google_places_backfill":
        if c.google_places_enrichment_service is None:
            raise ValueError("Google Places API not configured")
        summary = await c.google_places_enrichment_service.enrich_pending_venues(
            limit=cfg.get("limit")
        )
        logger.info(f"[AdminTrigger] google_places_backfill summary: {summary}")
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
        if name in ("google_places", "google_places_backfill") and _container.google_places_enrichment_service is None:
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
        # `venue_catalog` (discovery) is intentionally absent from JOB_REGISTRY and
        # falls here — discovery has no reachable trigger by design.
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


class GeoLinkUndoRequest(BaseModel):
    venue_id: str = Field(..., min_length=1)


@router.post("/venues/geo-link/undo")
async def undo_geo_link(request: GeoLinkUndoRequest, response: Response):
    """Reverse a fresh geo-fallback link (contract for the vibes_bot admin panel).

    Returns 200 {status:"undone"} on success, 200 {status:"already_undone"} when
    idempotent, 404 when the venue is unknown, 409 when it is not undo-eligible
    (older than 24h or deprecated by something other than a prior undo). See
    app/handlers/add_venue_handler.py:undo_geo_link for the full matrix.
    """
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    handler: Optional[AddVenueHandler] = getattr(_container, "add_venue_handler", None)
    if handler is None:
        raise HTTPException(
            status_code=503,
            detail="add-venue handler not configured",
        )
    outcome = await handler.undo_geo_link(request.venue_id)
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


def _eligibility_rule_service() -> Optional[EligibilityRuleService]:
    svc = getattr(_container, "eligibility_rule_service", None) if _container is not None else None
    return svc if isinstance(svc, EligibilityRuleService) else None


@router.get("/venues/eligibility-config")
async def get_eligibility_config():
    """Return the active venue-eligibility block-lists for the admin panel.

    Ex2: reads the normalized admin.eligibility_rule rows directly (the durable
    truth). Falls back to the Redis mirror when the rule service is not wired.
    """
    rule_svc = _eligibility_rule_service()
    if rule_svc is not None:
        return rule_svc.effective_config().to_public_dict()
    venue_dao = _get_venue_dao_from_container()
    config = load_eligibility_config(getattr(venue_dao, "client", None))
    return config.to_public_dict()


@router.post("/venues/eligibility-config")
async def update_eligibility_config(config: dict = Body(...)):
    """Update the venue-eligibility block-lists (admin-tunable, no redeploy).

    Validates that each provided field is a list of strings, persists the
    override to RDS (system of record) then mirrors the existing Redis
    `admin_config:venue_eligibility` key via AdminConfigService, and returns the
    resulting active config. Invalid bodies are rejected with HTTP 400 and the
    active config is left unchanged. Falls back to direct Redis when no admin
    config service is wired (preserves today's behavior).

    Note: tightening the blocked lists causes the next eligibility sweep to
    soft-delete more venues, which is one-way in V1 (no restore).
    """
    try:
        validated = EligibilityConfig.from_dict(config, from_admin_override=True)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid eligibility config: {e}")

    # Ex2: route the full-blob write through the rule service (decompose into
    # rows = truth, then reassemble the mirror).
    rule_svc = _eligibility_rule_service()
    if rule_svc is not None:
        try:
            rule_svc.set_full_config(config, updated_by="admin")
        except Exception as e:
            logger.error(f"[AdminTrigger] Failed to persist eligibility rules to RDS: {e}")
            raise HTTPException(status_code=502, detail="failed to persist eligibility config; retry")
        return validated.to_public_dict()

    svc = getattr(_container, "admin_config_service", None) if _container is not None else None
    if isinstance(svc, AdminConfigService):
        # RDS (truth) + Redis mirror. Input is already validated above, so any
        # exception from set() is an RDS/mirror failure -> 502 (retryable).
        try:
            svc.set("venue_eligibility", config, updated_by="admin")
        except Exception as e:
            logger.error(f"[AdminTrigger] Failed to persist eligibility config to RDS: {e}")
            raise HTTPException(
                status_code=502, detail="failed to persist eligibility config; retry"
            )
    else:
        # Fallback: no admin config service wired -> direct Redis (legacy behavior).
        venue_dao = _get_venue_dao_from_container()
        client = getattr(venue_dao, "client", None)
        if client is None:
            raise HTTPException(status_code=503, detail="venue DAO client not configured")
        try:
            client.set(ADMIN_CONFIG_ELIGIBILITY_KEY, json.dumps(config))
        except Exception as e:
            logger.error(f"[AdminTrigger] Failed to persist eligibility config: {e}")
            raise HTTPException(status_code=500, detail="failed to persist eligibility config")

    return validated.to_public_dict()


# ── single eligibility rule edits (Ex2: one-row add/remove) ──────────────────
def _require_eligibility_rule_service() -> EligibilityRuleService:
    svc = _eligibility_rule_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="eligibility rule service not configured")
    return svc


@router.post("/venues/eligibility-rule")
async def add_eligibility_rule(body: dict = Body(...)):
    """Add ONE eligibility rule (rule_type + value) as a single row, then
    reassemble the Redis mirror. Returns the resulting active config."""
    svc = _require_eligibility_rule_service()
    try:
        cfg = svc.add_rule(body.get("rule_type"), body.get("value"), updated_by="admin")
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid eligibility rule: {e}")
    except Exception as e:
        logger.error(f"[AdminTrigger] Failed to add eligibility rule: {e}")
        raise HTTPException(status_code=502, detail="failed to persist eligibility rule; retry")
    return cfg.to_public_dict()


@router.delete("/venues/eligibility-rule")
async def remove_eligibility_rule(
    rule_type: str = Query(...), value: str = Query(...)
):
    """Remove ONE eligibility rule (rule_type + value), then reassemble the
    mirror. Removing the last rule drops the override (readers use defaults)."""
    svc = _require_eligibility_rule_service()
    try:
        cfg = svc.remove_rule(rule_type, value, updated_by="admin")
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid eligibility rule: {e}")
    except Exception as e:
        logger.error(f"[AdminTrigger] Failed to remove eligibility rule: {e}")
        raise HTTPException(status_code=502, detail="failed to remove eligibility rule; retry")
    return cfg.to_public_dict()


# ── Recife-metro geo-fence box (admin.geo_fence, read by serving.eligible_venue) ─
# These routes MUST be declared before the generic `/config/{key}` handler below,
# or Starlette matches `/config/geofence` as `{key}="geofence"` and the write lands
# in admin.admin_config instead of admin.geo_fence — where the SQL view never sees
# it. The geo-fence is a typed table because the serving view reads it.
def _geo_fence_store():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    store = getattr(_container, "rds_store", None)
    if store is None or not hasattr(store, "get_geo_fence"):
        raise HTTPException(status_code=503, detail="geo-fence store not configured")
    return store


def _geo_fence_redis_client():
    """The Redis client used to mirror admin_config:venue_geofence (admin GET +
    parity reads). Best-effort: a missing client just skips the mirror."""
    venue_dao = getattr(_container, "venue_dao", None) or getattr(
        _container, "redis_venue_dao", None
    )
    return getattr(venue_dao, "client", None)


@router.get("/config/geofence")
async def get_geo_fence():
    """Return the active Recife/Olinda geo-fence box for the admin panel."""
    store = _geo_fence_store()
    try:
        box = store.get_geo_fence()
    except Exception as e:
        logger.error(f"[AdminGeoFence] read failed: {e}")
        raise HTTPException(status_code=502, detail="geo-fence read failed; retry")
    return box or dict(DEFAULT_GEO_FENCE)


@router.put("/config/geofence")
async def put_geo_fence(box: dict = Body(...)):
    """Update the geo-fence box (admin-tunable, no redeploy). Validates ranges
    (lat -90..90, lng -180..180, min<max) and rejects invalid payloads with HTTP
    400, leaving the active box unchanged. Writes the typed admin.geo_fence row
    (the SQL serving view reads it), then mirrors admin_config:venue_geofence in
    Redis for admin/parity reads. The next projection re-includes/excludes venues
    accordingly (reversible)."""
    try:
        validated = validate_geo_fence(box)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid geo-fence: {e}")

    store = _geo_fence_store()
    try:
        store.set_geo_fence(validated, updated_by="admin")
    except Exception as e:
        logger.error(f"[AdminGeoFence] persist to RDS failed: {e}")
        raise HTTPException(status_code=502, detail="failed to persist geo-fence; retry")

    # Best-effort Redis mirror (admin GET + parity reads). RDS is the durable truth
    # the serving view reads, so a mirror failure must not fail the write.
    client = _geo_fence_redis_client()
    if client is not None:
        try:
            client.set(ADMIN_CONFIG_GEOFENCE_KEY, json.dumps(validated))
        except Exception as e:
            logger.warning(f"[AdminGeoFence] Redis mirror write failed: {e}")

    return validated


# ── generic admin config (RDS system of record, Redis mirror) ────────────────
def _admin_config_service():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    svc = getattr(_container, "admin_config_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="admin config service not configured")
    return svc


@router.get("/config")
async def list_admin_config():
    """List the admin config keys owned by RDS (mirrored to Redis)."""
    return {"keys": _admin_config_service().list_keys()}


@router.get("/config/{key}")
async def get_admin_config(key: str):
    """Return the live value for a config key (Redis mirror; RDS is durable)."""
    value = _admin_config_service().get(key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"config key not found: {key}")
    return {"key": key, "value": value}


@router.put("/config/{key}")
async def put_admin_config(key: str, value: dict = Body(...)):
    """Write a config key to RDS (truth) then mirror Redis. Per-key validation
    runs before any write; a failed mirror after the RDS commit returns 502 so
    the caller retries (idempotent)."""
    svc = _admin_config_service()
    try:
        stored = svc.set(key, value, updated_by="admin")
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid config for {key}: {e}")
    except Exception as e:
        logger.error(f"[AdminConfig] write failed for {key}: {e}")
        raise HTTPException(status_code=502, detail=f"config write failed for {key}; retry")
    return {"key": key, "value": stored}


@router.delete("/config/{key}")
async def delete_admin_config(key: str):
    """Hard-delete a config key from RDS and the Redis mirror (readers default)."""
    try:
        _admin_config_service().delete(key)
    except Exception as e:
        logger.error(f"[AdminConfig] delete failed for {key}: {e}")
        raise HTTPException(status_code=502, detail=f"config delete failed for {key}; retry")
    return {"status": "ok", "key": key}


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


@router.get("/users/activity-counts")
async def user_activity_counts():
    """Distinct-user counts for the admin dashboard: total plus trailing 1d/7d/30d
    active windows. "Active" means the user made an authenticated app request that
    Recife day (the closest backend-observable proxy for a login)."""
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    try:
        return _container.engagement_service.activity_counts()
    except Exception as e:
        logger.error(f"[AdminTrigger] user activity counts failed: {e}")
        raise HTTPException(status_code=500, detail="user activity counts failed")


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
