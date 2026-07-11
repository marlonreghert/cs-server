"""Admin trigger routes for on-demand enrichment jobs."""
import asyncio
import json
import logging
import time
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Body, Query, Response
from pydantic import BaseModel, Field

from app.handlers.add_venue_handler import (
    AddVenueHandler,
    AddVenueByAddressRequest,
)
from app.models.batch_add import BatchAddRequest
from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    ADMIN_CONFIG_GEOFENCE_KEY,
    STATE_CAPITALS,
    EligibilityConfig,
    default_geo_fence,
    load_eligibility_config,
    validate_geo_fence,
)
from app.services.admin_config_service import AdminConfigService
from app.services.eligibility_rules import EligibilityRuleService
from app.services import job_lock
from app.metrics import JOB_LOCK_REJECTED_TOTAL

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


def require(attr: Optional[str] = None, *, detail: Optional[str] = None):
    """Return a required container attribute (or the container itself when
    ``attr`` is None), raising the same 503s the pasted preambles did.

    Collapses the ~13 copies of ``if _container is None: raise
    HTTPException(503, "Container not initialized")`` and the per-endpoint
    ``if getattr(_container, X) is None: raise HTTPException(503, "...")``
    service guards. The detail strings are unchanged.
    """
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    if attr is None:
        return _container
    value = getattr(_container, attr, None)
    if value is None:
        raise HTTPException(status_code=503, detail=detail or f"{attr} not configured")
    return value


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
#
# `photos` (catalog-wide photo pre-bake) is likewise ABSENT: photos are resolved
# ON DEMAND per venue (POST /internal/venues/{id}/photos/resolve → fresh, keyless
# URLs), so the catalog pre-bake is retired. Triggering `photos` returns 404.
# PhotoEnrichmentService.refresh_photos_for_venues remains for compatibility but
# has no reachable trigger path.
async def _run_google_places_backfill(c, cfg: dict) -> None:
    """google_places_backfill runner: Google-only PENDING backfill + summary log
    (kept out of the lambda table so its extra summary log survives)."""
    summary = await c.google_places_enrichment_service.enrich_pending_venues(
        limit=cfg.get("limit")
    )
    logger.info(f"[AdminTrigger] google_places_backfill summary: {summary}")


def _rebuild_redis_offloop(c, cfg: dict):
    """rebuild_redis runner. Off-loop (B0): the projection body is synchronous +
    blocking; running it inline would stall /v1/venues/nearby and /health for the
    whole run. Returns the awaitable executor future."""
    return asyncio.get_event_loop().run_in_executor(
        None, c.redis_projection_service.rebuild_redis_from_rds
    )


# Each entry carries its own dispatch (`runner`: async ``(container, cfg)``) and,
# when it depends on an optional service, the container attribute to guard on
# (`service_attr`) plus the exact ValueError detail (`unavailable_detail`). This
# is the single source of truth for both `_run_job` dispatch and the `list_jobs`
# availability listing — replacing the two parallel if/elif ladders.
JOB_REGISTRY = {
    "live_forecast": {
        "label": "Live Forecast Refresh",
        "description": "Refresh live busyness forecasts for all cached venues",
        "runner": lambda c, cfg: c.venues_refresher_service.refresh_live_forecasts_for_all_venues(),
    },
    "weekly_forecast": {
        "label": "Weekly Forecast Refresh",
        "description": "Refresh weekly forecast data for all cached venues",
        "runner": lambda c, cfg: c.venues_refresher_service.refresh_weekly_forecasts_for_all_venues(),
    },
    "google_places": {
        "label": "Google Places Enrichment",
        "description": "Enrich venues with Google Places vibe attributes and business status",
        "default_config": {"force_refresh": False},
        "service_attr": "google_places_enrichment_service",
        "unavailable_detail": "Google Places API not configured",
        "runner": lambda c, cfg: c.google_places_enrichment_service.enrich_all_venues(
            force_refresh=cfg.get("force_refresh", False)
        ),
    },
    "google_places_backfill": {
        "label": "Google Places Pending Backfill",
        "description": "One-time, idempotent Google-only enrichment of PENDING venues "
        "(active, no vibe attributes). Skips enriched + no-match venues; no BestTime call.",
        "service_attr": "google_places_enrichment_service",
        "unavailable_detail": "Google Places API not configured",
        "runner": _run_google_places_backfill,
    },
    "instagram": {
        "label": "Instagram Discovery",
        "description": "Discover Instagram handles for venues via Google Places + Apify",
        "service_attr": "instagram_enrichment_service",
        "unavailable_detail": "Instagram enrichment not configured (missing Apify API token)",
        "runner": lambda c, cfg: c.instagram_enrichment_service.enrich_all_venues(),
    },
    "instagram_posts": {
        "label": "Instagram Posts Scraping",
        "description": "Scrape recent Instagram posts for venues with IG handles",
        "service_attr": "instagram_posts_enrichment_service",
        "unavailable_detail": "IG posts enrichment not configured (missing Apify API token)",
        "runner": lambda c, cfg: c.instagram_posts_enrichment_service.enrich_all_venues(),
    },
    "menu_photos": {
        "label": "Menu Photo Enrichment",
        "description": "Fetch menu photos from Instagram highlights and Google Maps",
        "service_attr": "menu_photo_enrichment_service",
        "unavailable_detail": "Menu photo enrichment not configured (missing S3/Apify)",
        "runner": lambda c, cfg: c.menu_photo_enrichment_service.enrich_all_venues(),
    },
    "menu_extraction": {
        "label": "Menu Extraction (GPT-4o)",
        "description": "Extract structured menu data from photos using OpenAI vision",
        "service_attr": "menu_extraction_service",
        "unavailable_detail": "Menu extraction not configured (missing OpenAI/S3)",
        "runner": lambda c, cfg: c.menu_extraction_service.extract_all_venues(),
    },
    "vibe_classifier": {
        "label": "Vibe Classifier (AI)",
        "description": "Classify venue vibes from photos using 2-stage GPT pipeline",
        "service_attr": "vibe_classifier_service",
        "unavailable_detail": "Vibe classifier not configured (missing OpenAI API key)",
        "runner": lambda c, cfg: c.vibe_classifier_service.classify_all_venues(),
    },
    "instagram_validate": {
        "label": "Instagram Handle Validation",
        "description": "Check all cached Instagram handles and remove invalid ones (404 profiles)",
        "service_attr": "google_places_enrichment_service",
        "unavailable_detail": "Google Places enrichment not configured",
        "runner": lambda c, cfg: c.google_places_enrichment_service.validate_cached_instagram_handles(),
    },
    "inventory_sync": {
        "label": "BestTime Inventory Sync",
        "description": "Pull every venue in our BestTime account inventory into Redis. Free — does not spend the monthly new-venue budget.",
        "runner": lambda c, cfg: c.venues_refresher_service.sync_account_inventory_to_redis(),
    },
    "rebuild_redis": {
        "label": "Rebuild Redis from RDS",
        "description": "Reconstruct the Redis serving projection (incl. the geo index and live busyness) from RDS. Disaster recovery / Redis warm.",
        "runner": _rebuild_redis_offloop,
    },
}


async def _run_job(job_name: str, config: Optional[dict] = None):
    """Execute an enrichment job by name with optional config overrides.

    Dispatch and the optional-service guard both derive from JOB_REGISTRY: an
    unknown job or an unconfigured service raises the same ValueError as before.
    """
    c = _container
    cfg = config or {}
    start = time.perf_counter()

    entry = JOB_REGISTRY.get(job_name)
    if entry is None:
        raise ValueError(f"Unknown job: {job_name}")

    service_attr = entry.get("service_attr")
    if service_attr is not None and getattr(c, service_attr) is None:
        raise ValueError(entry["unavailable_detail"])

    await entry["runner"](c, cfg)

    duration = time.perf_counter() - start
    logger.info(f"[AdminTrigger] Job '{job_name}' completed in {duration:.1f}s (config={cfg})")


@router.get("/jobs")
async def list_jobs():
    """List all available enrichment jobs and their current status."""
    require()

    jobs = []
    for name, info in JOB_REGISTRY.items():
        # Availability derives from the registry's `service_attr`: a job with no
        # optional service is always available; otherwise it is available when
        # that container service is wired.
        service_attr = info.get("service_attr")
        available = service_attr is None or getattr(_container, service_attr) is not None

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
    require()

    if job_name not in JOB_REGISTRY:
        # `venue_catalog` (discovery) is intentionally absent from JOB_REGISTRY and
        # falls here — discovery has no reachable trigger by design.
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")

    # Check if already running (admin-vs-admin dedup)
    existing = _running_jobs.get(job_name)
    if existing is not None and not existing.done():
        return TriggerResponse(
            status="already_running",
            job=job_name,
            message=f"{JOB_REGISTRY[job_name]['label']} is already running",
        )

    # Shared scheduler+admin concurrency guard (app/services/job_lock.py) for
    # the 4 paid-refresh jobs: refuse a trigger while the SCHEDULER holds the
    # same lock_name, instead of doubling the cycle's BestTime/Google calls.
    # Acquired here (synchronous, no await before it) rather than inside
    # _wrapper so a racing scheduler tick cannot slip in between this check
    # and actually starting the background task.
    locked = job_name in job_lock.LOCKED_JOB_NAMES
    if locked and not job_lock.try_acquire(job_name):
        JOB_LOCK_REJECTED_TOTAL.labels(job_name=job_name, source="admin").inc()
        return TriggerResponse(
            status="already_running",
            job=job_name,
            message=(
                f"{JOB_REGISTRY[job_name]['label']} is already running "
                "(scheduled run in progress)"
            ),
        )

    # Launch as background task
    async def _wrapper():
        try:
            await _run_job(job_name, config=config)
        except Exception as e:
            logger.error(f"[AdminTrigger] Job '{job_name}' failed: {e}")
        finally:
            _running_jobs.pop(job_name, None)
            if locked:
                job_lock.release(job_name)

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
    handler: AddVenueHandler = require(
        "add_venue_handler", detail="add-venue handler not configured"
    )
    outcome = await handler.add(request)
    response.status_code = outcome.status_code
    return outcome.body


@router.post("/venues/batch-add")
async def batch_add_venues(request: BatchAddRequest, response: Response):
    """Add a whole curated list server-side, deterministically.

    Launches a background job that runs each row through the same
    AddVenueHandler.add() as POST /venues/by-address (same dedupe, geo-fallback,
    timeout recovery, enrichment, and BestTime rate limiter), persisting a
    pollable summary. Returns immediately with a job_id — poll
    GET /venues/batch-add/{job_id} for progress + the final per-row results.
    See app/services/batch_add_service.py.
    """
    service = require("batch_add_service", detail="batch-add service not configured")
    accepted = service.start_job(request)
    # Single-flight: another batch job is already running (409, not a new job).
    if accepted.get("status") == "already_running":
        response.status_code = 409
    else:
        response.status_code = 202
    return accepted


@router.get("/venues/batch-add/{job_id}")
async def get_batch_add_job(job_id: str):
    """Poll a batch-add job: {status, processed, total, summary, results, budget}."""
    service = require("batch_add_service", detail="batch-add service not configured")
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


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
    handler: AddVenueHandler = require(
        "add_venue_handler", detail="add-venue handler not configured"
    )
    outcome = await handler.undo_geo_link(request.venue_id)
    response.status_code = outcome.status_code
    return outcome.body


@router.get("/venues/monthly-budget")
async def get_monthly_budget():
    """Return the current state of the monthly new-venue budget."""
    budget = require("venue_budget_service", detail="venue budget service not configured")
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
    # The container exposes the RDS-backed repository as `pipeline_repository`
    # (renamed from the misleading `redis_venue_dao`). Read it directly — the old
    # fuzzy `venue_dao`-or-`redis_venue_dao` getattr fallback is gone with the
    # rename.
    return require("pipeline_repository", detail="venue DAO not configured")


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


# ── Capital-circle geo-fence (admin.geo_fence + admin.geo_fence_city, read by
# serving.eligible_venue) ──────────────────────────────────────────────────────
# These routes MUST be declared before the generic `/config/{key}` handler below,
# or Starlette matches `/config/geofence` as `{key}="geofence"` and the write lands
# in admin.admin_config instead of the geo-fence tables — where the SQL view never
# sees it. The geo-fence lives in typed tables because the serving view reads them.
def _geo_fence_store():
    store = require("rds_store", detail="geo-fence store not configured")
    if not hasattr(store, "get_geo_fence"):
        raise HTTPException(status_code=503, detail="geo-fence store not configured")
    return store


def _geo_fence_redis_client():
    """The Redis client used to mirror admin_config:venue_geofence (admin GET +
    parity reads). Best-effort: a missing client just skips the mirror."""
    venue_dao = getattr(_container, "pipeline_repository", None)
    return getattr(venue_dao, "client", None)


def _geo_excluded_active_count(store) -> Optional[int]:
    """Active venues outside every configured circle regardless of the enabled
    flag — the panel's warning number (what the restriction excludes while on;
    what re-enters serving while off). Best-effort: None when it cannot be
    computed (e.g. the deploy-before-migration window) — never fails the
    endpoint."""
    counter = getattr(store, "count_active_venues_outside_circles", None)
    if counter is None:
        return None
    try:
        return int(counter())
    except Exception as e:
        logger.warning(f"[AdminGeoFence] outside-circles count failed: {e}")
        return None


@router.get("/config/geofence")
async def get_geo_fence():
    """Return the active geo-fence for the admin panel: the enabled flag plus
    every configured capital circle with catalog-resolved coordinates, and
    `geo_excluded_active` — active venues outside every circle (null when the
    count is unavailable)."""
    store = _geo_fence_store()
    try:
        fence = store.get_geo_fence()
    except Exception as e:
        logger.error(f"[AdminGeoFence] read failed: {e}")
        raise HTTPException(status_code=502, detail="geo-fence read failed; retry")
    fence = fence or default_geo_fence()
    return {**fence, "geo_excluded_active": _geo_excluded_active_count(store)}


@router.get("/config/geofence/capitals")
async def list_geo_fence_capitals():
    """The server-side capital catalog for the admin panel's city select — the
    26 Brazilian state capitals + Brasília, sorted by name. The server owns all
    coordinates; PUT /config/geofence accepts only slug + radius_km."""
    return {"capitals": sorted(
        (dict(c) for c in STATE_CAPITALS), key=lambda c: c["name"]
    )}


@router.put("/config/geofence")
async def put_geo_fence(fence: dict = Body(...)):
    """Replace the geo-fence (admin-tunable, no redeploy): full-list
    {"enabled": bool, "cities": [{"slug", "radius_km"}]}, slug resolved to
    catalog coordinates server-side. Rejects with HTTP 400 — fence unchanged —
    on an unknown/duplicate slug, an out-of-[1,200] radius, `enabled` true with
    zero cities, or a legacy bounding-box payload. Writes the typed geo-fence
    tables transactionally (the SQL serving view reads them), then mirrors
    admin_config:venue_geofence in Redis for admin/parity reads. The next
    projection re-includes/excludes venues accordingly (reversible)."""
    try:
        validated = validate_geo_fence(fence)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid geo-fence: {e}")

    store = _geo_fence_store()
    try:
        store.set_geo_fence(validated, updated_by="admin")
    except Exception as e:
        logger.error(f"[AdminGeoFence] persist to RDS failed: {e}")
        raise HTTPException(status_code=502, detail="failed to persist geo-fence; retry")
    logger.info(
        "[AdminGeoFence] fence updated by=admin enabled=%s cities=%s",
        validated["enabled"],
        [f"{c['slug']}@{c['radius_km']:g}km" for c in validated["cities"]],
    )

    # Best-effort Redis mirror (admin GET + parity reads). RDS is the durable truth
    # the serving view reads, so a mirror failure must not fail the write. The
    # mirror stays the bare validated fence; the count is response-only.
    client = _geo_fence_redis_client()
    if client is not None:
        try:
            client.set(ADMIN_CONFIG_GEOFENCE_KEY, json.dumps(validated))
        except Exception as e:
            logger.warning(f"[AdminGeoFence] Redis mirror write failed: {e}")

    return {**validated, "geo_excluded_active": _geo_excluded_active_count(store)}


# ── generic admin config (RDS system of record, Redis mirror) ────────────────
def _admin_config_service():
    return require("admin_config_service", detail="admin config service not configured")


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
async def put_admin_config(key: str, value: Union[dict, list] = Body(...)):
    """Write a config key to RDS (truth) then mirror Redis. Per-key validation
    runs before any write; a failed mirror after the RDS commit returns 502 so
    the caller retries (idempotent).

    Accepts a JSON object OR array: most config keys are objects, but a few are
    list-valued (notably ``vibe_modes``, an ordered array of mode configs). The
    storage layer (RDS ``jsonb`` + the ``json.dumps`` Redis mirror) handles both,
    so the HTTP boundary must not reject a top-level array."""
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


def _venue_cache_flags_bulk(venue_dao, venue_ids: list[str]) -> dict[str, dict[str, bool]]:
    """Cache-presence flags for a whole page of venues (P4): one round-trip per
    key family for the whole page instead of ~16 reads PER venue.

    `weekly_forecast` keeps its original "any of the 7 days" semantics (a
    7-MGET union) — distinct from `update_data_quality_metrics`'s Monday-only
    gauge, which must not be unified with this one."""
    if not venue_ids:
        return {}

    weekly_present: set[str] = set()
    for day_int in range(7):
        weekly_present |= set(venue_dao.get_week_raw_forecasts_bulk(venue_ids, day_int))

    live_map = venue_dao.get_live_forecasts_bulk(venue_ids)
    vibe_map = venue_dao.get_vibe_attributes_bulk(venue_ids)
    photos_map = venue_dao.get_venue_photos_bulk(venue_ids)
    hours_map = venue_dao.get_opening_hours_bulk(venue_ids)
    ig_map = venue_dao.get_venue_instagram_bulk(venue_ids)
    reviews_map = venue_dao.get_venue_reviews_bulk(venue_ids)
    menu_photos_map = venue_dao.get_venue_menu_photos_bulk(venue_ids)
    menu_data_map = venue_dao.get_venue_menu_data_bulk(venue_ids)
    vibe_profile_map = venue_dao.get_venue_vibe_profile_bulk(venue_ids)

    return {
        vid: {
            "live_forecast": vid in live_map,
            "weekly_forecast": vid in weekly_present,
            "vibe_attributes": vid in vibe_map,
            "photos": bool(photos_map.get(vid)),
            "opening_hours": vid in hours_map,
            "instagram": vid in ig_map,
            "reviews": vid in reviews_map,
            "menu_photos": vid in menu_photos_map,
            "menu_data": vid in menu_data_map,
            "vibe_profile": vid in vibe_profile_map,
        }
        for vid in venue_ids
    }


@router.get("/venues/inventory")
def list_venue_inventory(
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

        # P4: one bulk presence lookup per key family for the whole page,
        # instead of ~16 reads PER page venue.
        cache_flags_by_id = _venue_cache_flags_bulk(
            venue_dao, [venue.venue_id for venue in page]
        )

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
                    "cache_flags": cache_flags_by_id.get(venue.venue_id, {}),
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
    require()
    try:
        return _container.engagement_service.activity_counts()
    except Exception as e:
        logger.error(f"[AdminTrigger] user activity counts failed: {e}")
        raise HTTPException(status_code=500, detail="user activity counts failed")


@router.post("/recount-discovery-points")
async def recount_discovery_points():
    """Recount venues per discovery point using GEORADIUS and update counters."""
    require()

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
def venue_type_breakdown():
    """Get a breakdown of all venues by BestTime type and Google Places type."""
    # Resolve the DAO through the shared helper (raises 503 when the container is
    # not initialized). The container exposes the RDS-backed repository as
    # `pipeline_repository`, so the previous direct `_container.venue_dao` access
    # always AttributeError'd into a 500. Kept OUTSIDE the try below so the
    # helper's 503 is not laundered into a 500 by the blanket handler.
    venue_dao = _get_venue_dao_from_container()

    try:
        besttime_types: dict[str, int] = {}
        google_types: dict[str, int] = {}
        total = 0
        with_google_type = 0

        # One bulk RDS row read (the pattern the inventory endpoint uses) instead
        # of list_all_venue_ids() + a per-id get_venue. The per-venue
        # get_vibe_attributes read stays for now — this is an admin-only,
        # low-traffic endpoint; the performance batch's bulk per-table readers
        # can later serve it in one query.
        for venue in venue_dao.list_all_venues():
            total += 1

            bt = venue.venue_type or "unknown"
            besttime_types[bt] = besttime_types.get(bt, 0) + 1

            # Check Google Places type from vibe attributes
            vibe_attrs = venue_dao.get_vibe_attributes(venue.venue_id)
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
