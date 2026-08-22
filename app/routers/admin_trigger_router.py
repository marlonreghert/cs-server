"""Admin trigger routes for on-demand enrichment jobs."""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Body, Query, Response
from pydantic import BaseModel, Field

from app.handlers.add_venue_handler import (
    AddVenueHandler,
    AddVenueByAddressRequest,
)
from app.models.batch_add import BatchAddRequest
from app.services.archive_sources import public_catalog
from app.services.venue_photo_archive_service import InvalidArchivePath, parse_venue_ids
from app.services.venue_profile_photo_service import InvalidProfilePhotoMode
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
from app.models.venue_category import effective_category_map, representative_google_type
from app.services import job_lock
from app.services.pipeline_run_registry import run_scope
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
    # Identifies THIS run so its logs (Loki) and its run record can be found
    # afterwards. Absent when nothing was started (e.g. already_running).
    job_id: Optional[str] = None


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
        "description": "Find Instagram handles cheapest-source-first: the venue's "
        "Google website, then the archived Google Maps payload, then the venue's "
        "own website (all free), then "
        "the paid Apify search. Every candidate is verified against the real "
        "profile. Turn the paid tier off for a zero-cost pass.",
        # Rendered by the vibesadmin Enrichments options modal — every key here
        # becomes a control on the Run dialog.
        "default_config": {
            "force_refresh": False,
            "tier_google_website_enabled": True,
            "tier_archived_gmaps_website_enabled": True,
            "tier_venue_website_enabled": True,
            "tier_apify_search_enabled": True,
            # Explicit, and explicitly OFF. Without this key _source_enabled
            # falls through to the PAID_SOURCES master switch, which this run
            # leaves True — so once the collaborator is built on credential
            # presence alone (plans/260811_instagram-discovery-admin-flags.md),
            # a whole-catalogue run would make a paid Google search for EVERY
            # venue. With judge_enabled False below, none of those candidates
            # could ever clear the accept bar (PROVENANCE_WEIGHT 0.20), so the
            # spend would buy nothing. An operator who wants this tier turns it
            # on here — it renders as its own control on the Run dialog — and
            # turns the judge on with it.
            "tier_google_search_enabled": False,
            "judge_enabled": False,
            "venue_ids": "",
        },
        "service_attr": "instagram_cascade_service",
        "unavailable_detail": "Instagram cascade not configured",
        "runner": lambda c, cfg: c.instagram_cascade_service.run(cfg),
    },
    "instagram_posts": {
        "label": "Instagram Posts Scraping",
        "description": "Scrape recent Instagram posts for venues with IG handles",
        "service_attr": "instagram_posts_enrichment_service",
        "unavailable_detail": "IG posts enrichment not configured (missing Apify API token)",
        "runner": lambda c, cfg: c.instagram_posts_enrichment_service.enrich_all_venues(),
    },
    "venue_photo_archive": {
        # The id stays `venue_photo_archive`: it is the API path, the admin
        # panel's dispatch key and the name in every test across both repos.
        # Renaming it would be a coordinated cross-repo break for no operator
        # benefit — the LABEL is what anyone actually reads.
        "label": "Venue Data Retrieval",
        "description": "Retrieve venue photos and place data into the S3 "
        "retrieved/ archive, from the Google Places API or the Apify Google "
        "Maps scraper. Each venue gets media/ for images and info/ for "
        "everything else. Venues already retrieved by the last run are skipped "
        "unless overwrite is set.",
        # Surfaced by GET /admin/jobs and rendered by the admin panel's pre-run
        # options modal; every key here becomes a control in that dialog.
        # Mirrors the settings defaults deliberately: 50 x 5 = 250 upper-bound
        # billed requests, so the default click stays inside Google's free tier
        # and scaling up is a conscious edit.
        "default_config": {
            "source": "google_photos",
            "path_mode": "new_run",
            "path_override": "",
            "max_venues": 50,
            "max_photos_per_venue": 5,
            "max_photos_per_category": "",
            "eligibility": {
                "mode": "all", "venue_ids": "", "lat": "", "lon": "", "radius_km": "",
            },
            "skip_scope": "latest_run",
            "overwrite": False,
            "dry_run": False,
        },
        "service_attr": "venue_photo_archive_service",
        "unavailable_detail": "Media archive not configured (needs Google Places + a bucket)",
        "runner": lambda c, cfg: c.venue_photo_archive_service.run(cfg),
    },
    "instagram_profile_photos": {
        "label": "Instagram Profile Photos (list hero)",
        "description": "Archive each venue's Instagram profile picture to the "
        "media bucket and project its CloudFront URL, so the venue list can "
        "show a thumbnail with no serve-time Google call. "
        "BACKFILL (the default, and the only mode the scheduler ever runs) "
        "skips every venue that already has a photo, at ANY age — a captured "
        "profile picture stays good indefinitely, so steady-state cost is "
        "about zero. REFRESH_ALL re-scrapes venues that already have one and "
        "costs one Apify unit each: price it first with "
        "POST /admin/trigger/instagram_profile_photos/estimate. Either mode "
        "also skips a venue whose last attempt failed inside "
        "INSTAGRAM_PROFILE_PHOTO_RETRY_DAYS (set it to 0 to retry every failed "
        "venue now, e.g. right after fixing the bucket policy). "
        "EDGE_COLOR is a third, FREE mode: it makes no Apify call and uploads "
        "nothing, re-reading each already-stored photo over its own public CDN "
        "URL to record the edge colour the app paints behind a fitted avatar. "
        "It only touches rows that have no colour yet, so it is safe to re-run "
        "and costs $0.00. Inert unless INSTAGRAM_PROFILE_PHOTO_ENABLED is on.",
        # The one knob this job takes. Every spend BOUND stays a setting, so a
        # trigger dialog can choose what to scrape but never how much.
        "default_config": {"mode": "backfill"},
        "service_attr": "venue_profile_photo_service",
        "unavailable_detail": "Instagram profile photos not configured "
        "(needs Apify API token + MEDIA_BUCKET + MEDIA_CDN_BASE_URL)",
        "runner": lambda c, cfg: c.venue_profile_photo_service.run(cfg),
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
    "event_targeting": {
        "label": "Event Venue Targeting",
        "description": "Two-stage targeting for which venues are worth crawling for "
        "events: a free category gate over the whole catalog, then a bounded "
        "evidence gate (archived flyer photos + Instagram caption event "
        "markers) over the top N by priority. Zero model calls, zero external "
        "API calls.",
        "default_config": {
            # Raised from 25 (2026-08-07 RCA) — see DEFAULT_MAX_EVIDENCE_VENUES
            # in app/services/event_venue_targeting.py for why: this stage is
            # free (zero model calls, zero external API calls), so a small
            # bound protected nothing and left most of the catalog unevaluated.
            "max_evidence_venues": 1000,
            "min_evidence_posts": 3,
            "lookback_days": 60,
            "recompute": False,
            "dry_run": False,
        },
        "service_attr": "event_venue_targeting_service",
        "unavailable_detail": "Event venue targeting not configured",
        "runner": lambda c, cfg: c.event_venue_targeting_service.run(cfg),
    },
    "event_extraction": {
        "label": "Instagram Event Extraction",
        "description": "Turn archived posts of event-candidate venues into "
        "structured events. A post is sent to the model only when an "
        "archived image classified 'flyer' or its caption matched an event "
        "marker — everything else costs nothing. One OpenAI vision call per "
        "qualifying post.",
        "default_config": {
            "eligibility": {"mode": "event_candidates", "venue_ids": ""},
            "max_venues": 25,
            "max_posts_per_venue": 20,
            "lookback_days": 60,
            "min_confidence": 0.5,
            "dry_run": False,
        },
        "service_attr": "event_extraction_service",
        "unavailable_detail": "Event extraction not configured (needs OpenAI key + media archive)",
        "runner": lambda c, cfg: c.event_extraction_service.run(cfg),
    },
    "promoter_discovery": {
        "label": "Promoter Account Discovery",
        "description": "Propose promoter accounts from @-mentions already observed "
        "in captions of already-extracted venue posts. Costs nothing — the "
        "captions were already scraped and the events already extracted. "
        "Never crawls a proposed account; an operator must activate it first.",
        "default_config": {"mention_threshold": 3},
        "service_attr": "promoter_registry_service",
        "unavailable_detail": "Promoter registry not configured",
        "runner": lambda c, cfg: c.promoter_registry_service.run_discovery(cfg),
    },
    "promoter_event_crawl": {
        "label": "Promoter Event Crawl",
        "description": "Crawl active promoter accounts (or a given handle list), "
        "extract events with the existing extractor, and resolve each event's "
        "venue through the resolution ladder — an exact handle mention or "
        "location tag first, then a name match gated by a confidence floor "
        "AND a margin over the runner-up. Anything short of both is queued "
        "for review; nothing is ever guessed onto the wrong venue.",
        "default_config": {
            "handles": "",
            "max_accounts": 10,
            "max_posts_per_account": 15,
            "lookback_days": 60,
            "dry_run": False,
        },
        "service_attr": "promoter_crawl_service",
        "unavailable_detail": "Promoter crawl not configured (needs Apify API token)",
        "runner": lambda c, cfg: c.promoter_crawl_service.run(cfg),
    },
    "reviews_deep_crawl": {
        "label": "Deep Review Corpus Crawl",
        "description": "Capture every Google review newer than a configurable "
        "window (default 180 days) for operator-selected venues, via a paid "
        "Apify actor. Gated by a local monthly review budget AND the shared "
        "Apify account's remaining headroom — a run never spends the "
        "production Instagram/events crawl's quota. Not scheduled: "
        "operator-driven only, no cron.",
        "default_config": {"venue_ids": "", "filter": None},
        "service_attr": "deep_review_crawl_service",
        "unavailable_detail": "Deep review crawl not configured (needs Apify API token)",
        "runner": lambda c, cfg: c.deep_review_crawl_service.run(
            venue_ids=parse_venue_ids(cfg.get("venue_ids")) or None,
            filter_spec=cfg.get("filter"),
            job_id=cfg.get("job_id"),
        ),
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

    # Same run scope as the scheduler's make_job, so an admin-triggered run is
    # indistinguishable from its scheduled twin to a dashboard: same metric,
    # same `job=<id>` log correlation, same registry entry.
    with run_scope(job_name) as run_id:
        await entry["runner"](c, cfg)
        duration = time.perf_counter() - start
        logger.info(
            f"[AdminTrigger] Job '{job_name}' completed in {duration:.1f}s "
            f"(config={cfg}) job={run_id}"
        )


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

        entry = {
            "name": name,
            "label": info["label"],
            "description": info["description"],
            "available": available,
            "running": running,
            "default_config": info.get("default_config"),
        }
        # The archive publishes its source catalog so the admin panel renders
        # the per-source controls from here — adding a source stays a backend
        # change.
        if name == "venue_photo_archive":
            entry["sources"] = public_catalog(_container)
        jobs.append(entry)

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

    # Mint the run's identity here, not inside the job: the caller must be told
    # what it started before the run finishes, and the same id has to reach the
    # log lines so a trigger can be traced to its output.
    job_id = uuid.uuid4().hex
    run_config = dict(config or {})
    run_config["job_id"] = job_id

    # Launch as background task
    async def _wrapper():
        try:
            await _run_job(job_name, config=run_config)
        except Exception as e:
            logger.error(f"[AdminTrigger] Job '{job_name}' job_id={job_id} failed: {e}")
        finally:
            _running_jobs.pop(job_name, None)
            if locked:
                job_lock.release(job_name)

    task = asyncio.create_task(_wrapper())
    _running_jobs[job_name] = task
    logger.info(f"[AdminTrigger] Job '{job_name}' started job_id={job_id}")

    return TriggerResponse(
        status="started",
        job=job_name,
        message=f"{JOB_REGISTRY[job_name]['label']} started in background",
        job_id=job_id,
    )


@router.post("/trigger/{job_name}/stop")
async def stop_job(job_name: str):
    """Cancel a run that is in flight.

    The run is an in-memory asyncio task, so cancelling it stops the work at
    the next await — which for this pipeline means before the next paid
    request. Previously the only way to stop a run that was spending money was
    to restart the container.
    """
    require()
    if job_name not in JOB_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")

    task = _running_jobs.get(job_name)
    if task is None or task.done():
        return TriggerResponse(
            status="not_running",
            job=job_name,
            message=f"{JOB_REGISTRY[job_name]['label']} is not running",
        )

    task.cancel()
    logger.warning(f"[AdminTrigger] Job '{job_name}' cancelled by an operator")
    return TriggerResponse(
        status="stopping",
        job=job_name,
        message=f"{JOB_REGISTRY[job_name]['label']} is being stopped",
    )


# ── photo archive: estimate before spending, and run records after ────────────
def _photo_archive_service():
    service = getattr(_container, "venue_photo_archive_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Media archive not configured (needs Google Places + a bucket)",
        )
    return service


def _deep_review_crawl_service():
    service = getattr(_container, "deep_review_crawl_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Deep review crawl not configured (needs Apify API token)",
        )
    return service


@router.post("/reviews-deep/estimate")
async def estimate_deep_review_crawl(config: Optional[dict] = None):
    """Price a deep review crawl WITHOUT making a single Apify request — the
    id-list-is-authoritative selection rule (a filter may narrow, never
    widen) applies here exactly as it does to the real run, and the reported
    review/cost figures are an explicit UPPER BOUND (see DeepReviewCrawlService.
    estimate), not a prediction: how many reviews actually fall inside the
    window is unknowable without crawling."""
    require()
    cfg = config or {}
    return _deep_review_crawl_service().estimate(
        venue_ids=parse_venue_ids(cfg.get("venue_ids")) or None,
        filter_spec=cfg.get("filter"),
    )


@router.post("/trigger/venue_photo_archive/estimate")
async def estimate_photo_archive(config: Optional[dict] = None):
    """Price a photo archive run WITHOUT making a single Google request.

    Declared before the generic `/trigger/{job_name}` handler would ever see it —
    FastAPI matches in declaration order, and this literal path is registered
    after that route, so it is reachable only because the path segment count
    differs. Kept as its own route rather than a flag on the trigger so an
    estimate can never accidentally start a run.
    """
    require()
    try:
        return await _photo_archive_service().estimate(config)
    except InvalidArchivePath as e:
        # Includes InvalidArchiveConfig: the operator's request was rejected and
        # nothing was spent.
        raise HTTPException(status_code=400, detail=str(e))


def _profile_photo_service():
    service = getattr(_container, "venue_profile_photo_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Instagram profile photos not configured "
            "(needs Apify API token + MEDIA_BUCKET + MEDIA_CDN_BASE_URL)",
        )
    return service


# Both spellings are registered. The underscore form is the one that matters:
# the admin panel builds its estimate URL generically as
# `/admin/trigger/{job.name}/estimate`, and this job's registry name is
# `instagram_profile_photos`. The hyphenated alias is the path the feature was
# specified with and is kept so a hand-written call or a doc link does not 404.
@router.post("/trigger/instagram-profile-photos/estimate")
@router.post("/trigger/instagram_profile_photos/estimate")
async def estimate_instagram_profile_photos(config: Optional[dict] = None):
    """Price an Instagram profile photo run WITHOUT making a single Apify call.

    Same guarantee, and the same reason, as
    `POST /trigger/venue_photo_archive/estimate` above: a SEPARATE endpoint
    rather than a flag on the trigger, so an estimate can never accidentally
    start a run. `VenueProfilePhotoService.estimate` reinforces that in code —
    it is synchronous, contains no `await`, and never touches a provider
    client.

    The count it returns is `len(selection.selected)` from the very same
    `select()` call the run makes, so the number an operator approves is the
    number of billed scrapes they get.

    `mode` is "backfill" (default, and free for venues that already have a
    photo) or "refresh_all", which re-scrapes stored photos and therefore comes
    back with a populated `warning` for the admin UI to put a warning sign on.
    """
    require()
    try:
        return _profile_photo_service().estimate(config)
    except InvalidProfilePhotoMode as e:
        # An unrecognised mode is rejected, never resolved to a default: the
        # two modes differ by the whole catalog's worth of Apify units.
        raise HTTPException(status_code=400, detail=str(e))


# Container attributes of every job-type service that persists its own run
# records (get_run_record(job_id) -> dict | None, same shape as
# VenuePhotoArchiveService's — see that service's "run records" section).
# GET /admin/jobs/runs/{job_id} tries each in turn: a job_id is opaque to the
# caller (it does not encode which job type minted it), so the lookup must
# not assume a single owning service. Extend this tuple, not the endpoint
# itself, when a new job type gains its own run-record store.
_RUN_RECORD_SERVICE_ATTRS = ("venue_photo_archive_service", "deep_review_crawl_service")


@router.get("/jobs/runs/{job_id}")
async def get_job_run(job_id: str):
    """What a specific run did, by the job id its trigger returned.

    Tries every _RUN_RECORD_SERVICE_ATTRS service in order and returns the
    first hit. A service that is entirely unconfigured (e.g. no Apify token,
    no Google Places key) is skipped rather than raising — with more than one
    possible source, "not configured" and "no record under this id" are no
    longer the same 503 one owning service used to report; both now correctly
    resolve to a plain 404.
    """
    require()
    for attr in _RUN_RECORD_SERVICE_ATTRS:
        service = getattr(_container, attr, None)
        if service is None:
            continue
        record = service.get_run_record(job_id)
        if record is not None:
            return record
    raise HTTPException(status_code=404, detail=f"No run record for {job_id}")


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


@router.post("/venues/add-job")
async def start_add_venue_job(request: AddVenueByAddressRequest, response: Response):
    """Start POST /venues/by-address as a pollable background job.

    Same request body as POST /venues/by-address. Returns immediately with a
    job_id — poll GET /venues/add-job/{job_id} for the outcome, which is
    exactly what the synchronous route would have returned for the same
    inputs, just delivered a poll away instead of holding the HTTP request
    open for the full BestTime-create + enrichment + Instagram-discovery
    duration. No new single-flight lock: see
    app/services/add_venue_job_service.py.
    """
    service = require(
        "add_venue_job_service", detail="add-venue job service not configured"
    )
    accepted = service.start_job(request)
    response.status_code = 202
    return accepted


@router.get("/venues/add-job/{job_id}")
async def get_add_venue_job(job_id: str):
    """Poll a single-add job: {status, started_at, venue_name, venue_address}
    while running; adds {finished_at, http_status, result} once done, or
    {finished_at, error} if the job runner crashed."""
    service = require(
        "add_venue_job_service", detail="add-venue job service not configured"
    )
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/venues/add-jobs/recent")
async def list_recent_add_venue_jobs(limit: int = Query(default=20, ge=1)):
    """Recent single-add jobs, newest first, capped at
    ADD_VENUE_RECENT_JOBS_CAP (50) regardless of `limit` — lets an operator
    see the outcome (or failure reason) of any add started in the last 50
    jobs / 24h without having kept a poll open on it."""
    service = require(
        "add_venue_job_service", detail="add-venue job service not configured"
    )
    return {"jobs": service.list_recent(limit=limit)}


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


class AddressCacheClearRequest(BaseModel):
    venue_name: str = Field(..., min_length=1, max_length=256)
    venue_address: str = Field(..., min_length=1, max_length=1024)


@router.post("/venues/address-cache/clear")
async def clear_venue_address_cache(
    request: AddressCacheClearRequest, response: Response
):
    """Admin repair: clear one venue_lookup_by_address_v1:* entry by
    venue_name + venue_address (never a raw Redis key) — the operator-facing
    fix for an add-venue attempt that was permanently (mis-)cached against
    the wrong venue id. See
    app/handlers/add_venue_handler.py:clear_address_cache and
    plans/260816_venue-address-cache-integrity.md.

    Returns 200 {"status": "cleared", "previous_venue_id": ...} when an entry
    existed, or 200 {"status": "not_cached"} when there was nothing to clear
    (never an error — safe to call repeatedly).
    """
    handler: AddVenueHandler = require(
        "add_venue_handler", detail="add-venue handler not configured"
    )
    outcome = handler.clear_address_cache(request.venue_name, request.venue_address)
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


@router.get("/venues/closed")
async def get_closed_venues():
    """List venues excluded from serving because their reviews report closure.

    Read-only operator visibility: closure is derived from review evidence and
    is reversible, so an operator must be able to see *why* a venue left the
    serving view and audit a false positive. Low-confidence signals are listed
    too — they do not affect serving, but they are exactly what an operator
    should review.
    """
    store = require("rds_store", detail="rds_store not configured")
    try:
        rows = store.list_closure_signals()
    except Exception as exc:
        logging.warning(f"[AdminTriggerRouter] closure signal listing failed: {exc}")
        raise HTTPException(status_code=503, detail="closure signals unavailable")

    venues = []
    for row in rows:
        if not row.get("closed"):
            continue
        venues.append(
            {
                "venue_id": row.get("venue_id"),
                "reason": row.get("reason"),
                "confidence": row.get("confidence"),
                "evidence_publish_time": row.get("evidence_publish_time"),
                "matched_phrase": row.get("matched_phrase"),
                "excluded_from_serving": row.get("confidence") == "high",
            }
        )
    venues.sort(key=lambda v: (v["confidence"] != "high", v["venue_id"] or ""))
    return {"venues": venues, "count": len(venues)}


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


# ── venue type → category mapping (admin-tunable, no redeploy) ────────────────
def _category_map_redis_client():
    """The raw Redis client used to read the effective category map (same source
    the serve path uses via load_category_map)."""
    return getattr(_get_venue_dao_from_container(), "client", None)


@router.get("/venues/category-map")
async def get_category_map():
    """Return the effective type→category mapping for the admin panel.

    The hardcoded defaults merged with any stored admin override, so the UI
    pre-populates with the live mapping.
    """
    return effective_category_map(_category_map_redis_client())


@router.post("/venues/category-map")
async def update_category_map(config: dict = Body(...)):
    """Persist an admin override of the Google/BestTime → category mapping.

    Validates that every category value is a known category (400 on unknown),
    normalizes key casing, writes RDS (system of record) then mirrors the Redis
    ``admin_config:venue_category_map`` key via AdminConfigService, and returns
    the resulting effective map. The change applies at serve time on the next
    request — no venue re-fetch, no re-projection.
    """
    svc = _admin_config_service()
    try:
        svc.set("venue_category_map", config, updated_by="admin")
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid category map: {e}")
    except Exception as e:
        logger.error(f"[AdminTrigger] Failed to persist category map: {e}")
        raise HTTPException(status_code=502, detail="failed to persist category map; retry")
    return effective_category_map(_category_map_redis_client())


# ── per-venue type override (correct a single mis-typed venue) ────────────────
async def _trigger_rebuild_redis_nonfatal() -> None:
    """Kick a re-projection so the override reaches serving fast. Non-fatal: the
    periodic projector re-asserts RDS→Redis anyway, so a failure here is logged,
    not surfaced."""
    c = _container
    proj = getattr(c, "redis_projection_service", None) if c is not None else None
    if proj is None:
        return
    try:
        await asyncio.get_event_loop().run_in_executor(None, proj.rebuild_redis_from_rds)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[AdminTrigger] type-override rebuild_redis failed (non-fatal): {e}")


@router.post("/venues/{venue_id}/type-override")
async def set_venue_type_override(venue_id: str, body: dict = Body(...)):
    """Correct a single venue's type by choosing its real category.

    Stores the representative google_primary_type for the category and locks it
    (primary_type_locked) so re-enrichment cannot overwrite it. Because category
    resolution and the eligibility view both key off google_primary_type, this
    recategorizes AND un-blocks the venue; a re-projection is triggered so it
    reaches serving without waiting for the periodic projector. 400 on an unknown
    category (or OTHER, which has no representative type); 404 when the venue has
    no vibe_attributes record.
    """
    category = (body or {}).get("category")
    if not isinstance(category, str):
        raise HTTPException(status_code=400, detail="category is required")
    representative = representative_google_type(category)
    if representative is None:
        raise HTTPException(
            status_code=400, detail=f"unknown or non-overridable category: {category!r}"
        )
    venue_dao = _get_venue_dao_from_container()
    existing = venue_dao.get_vibe_attributes(venue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"venue not found: {venue_id}")
    existing.google_primary_type = representative
    existing.primary_type_locked = True
    venue_dao.set_vibe_attributes(existing)
    logger.info(
        f"[AdminTrigger] type override: {venue_id} → {category.upper()} "
        f"(google_primary_type={representative}, locked)"
    )
    await _trigger_rebuild_redis_nonfatal()
    return {
        "venue_id": venue_id,
        "google_primary_type": representative,
        "category": category.upper(),
    }


@router.delete("/venues/{venue_id}/type-override")
async def clear_venue_type_override(venue_id: str):
    """Clear a per-venue type override: unlock the venue so a subsequent
    force_refresh re-enrichment restores Google's value. 404 when the venue has
    no vibe_attributes record."""
    venue_dao = _get_venue_dao_from_container()
    existing = venue_dao.get_vibe_attributes(venue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"venue not found: {venue_id}")
    existing.primary_type_locked = False
    venue_dao.set_vibe_attributes(existing)
    logger.info(f"[AdminTrigger] type override cleared: {venue_id} (unlocked)")
    await _trigger_rebuild_redis_nonfatal()
    return {"venue_id": venue_id, "status": "unlocked"}


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


def _venue_cache_flags_bulk(
    venue_dao, venue_ids: list[str]
) -> tuple[dict[str, dict[str, bool]], dict[str, object]]:
    """Cache-presence flags for a whole page of venues (P4): one round-trip per
    key family for the whole page instead of ~16 reads PER venue.

    `weekly_forecast` keeps its original "any of the 7 days" semantics (a
    7-MGET union) — distinct from `update_data_quality_metrics`'s Monday-only
    gauge, which must not be unified with this one.

    Returns the presence flags alongside the raw Instagram record map (the
    same `get_venue_instagram_bulk` result the `instagram` flag is derived
    from), so `list_venue_inventory` can project the handle/url/status/
    confidence/source onto each item without a second bulk read."""
    if not venue_ids:
        return {}, {}

    weekly_present: set[str] = set()
    for day_int in range(7):
        weekly_present |= set(venue_dao.get_week_raw_forecasts_bulk(venue_ids, day_int))

    live_map = venue_dao.get_live_forecasts_bulk(venue_ids)
    vibe_map = venue_dao.get_vibe_attributes_bulk(venue_ids)
    photos_map = venue_dao.get_venue_photos_bulk(venue_ids)
    hours_map = venue_dao.get_opening_hours_bulk(venue_ids)
    ig_map = venue_dao.get_venue_instagram_bulk(venue_ids)
    reviews_map = venue_dao.get_venue_reviews_bulk(venue_ids)
    reviews_deep_map = venue_dao.get_venue_reviews_deep_bulk(venue_ids)
    menu_photos_map = venue_dao.get_venue_menu_photos_bulk(venue_ids)
    menu_data_map = venue_dao.get_venue_menu_data_bulk(venue_ids)
    vibe_profile_map = venue_dao.get_venue_vibe_profile_bulk(venue_ids)

    flags = {
        vid: {
            "live_forecast": vid in live_map,
            "weekly_forecast": vid in weekly_present,
            "vibe_attributes": vid in vibe_map,
            "photos": bool(photos_map.get(vid)),
            "opening_hours": vid in hours_map,
            # Membership only — a `not_found` record still counts as
            # "present" here, exactly as before this field started being
            # returned alongside it. Do not change this to `has_instagram()`
            # or any status check; other panels and tests read this exact
            # boolean.
            "instagram": vid in ig_map,
            "reviews": vid in reviews_map,
            # Presence in the Redis serving projection only, matching every
            # other flag here — a venue can hold a much larger corpus in RDS
            # than this reflects (the projector caps what reaches Redis).
            "reviews_deep": vid in reviews_deep_map,
            "menu_photos": vid in menu_photos_map,
            "menu_data": vid in menu_data_map,
            "vibe_profile": vid in vibe_profile_map,
        }
        for vid in venue_ids
    }
    return flags, ig_map


def _venue_instagram_item(record) -> Optional[dict]:
    """Project a stored Instagram record onto the inventory item's public
    shape. `None` means "nobody has looked" (no cached record at all); a
    `not_found` record still returns an object (with a null handle) so the
    panel can tell "searched, found nothing" apart from "never searched".

    Defensive by construction: `get_venue_instagram_bulk` already drops
    records that fail JSON/pydantic parsing (they simply never appear in the
    map), but this listing is a troubleshooting surface and must survive any
    record that reaches here regardless of shape — `getattr` with a default
    never raises, and the whole projection is additionally wrapped so one bad
    row degrades to nulls instead of 500ing the entire page."""
    if record is None:
        return None
    try:
        return {
            "handle": getattr(record, "instagram_handle", None),
            "url": getattr(record, "instagram_url", None),
            "status": getattr(record, "status", None),
            "confidence": getattr(record, "confidence_score", None),
            "source": getattr(record, "source", None),
        }
    except Exception as e:
        logger.error(f"[AdminTrigger] Failed to project Instagram record: {e}")
        return {"handle": None, "url": None, "status": None, "confidence": None, "source": None}


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
        # instead of ~16 reads PER page venue. Also returns the raw Instagram
        # record map so the `instagram` field below reuses it instead of
        # issuing a second bulk read.
        cache_flags_by_id, instagram_by_id = _venue_cache_flags_bulk(
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
                    "instagram": _venue_instagram_item(instagram_by_id.get(venue.venue_id)),
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


@router.get("/events/targeting")
def event_targeting_summary(
    tier: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Tier counts + a paged listing of event_venue_profile rows.

    The admin-config CRUD route already handles arbitrary keys, so the
    category allow-list (admin_config:event_candidate_categories) is editable
    with no dedicated endpoint — this route only reports the targeting
    verdicts computed by the event_targeting job.
    """
    venue_dao = _get_venue_dao_from_container()
    from app.services.event_venue_targeting import ALL_TIERS

    tiers = [tier] if tier else None
    try:
        profiles = venue_dao.list_venue_event_profiles(tiers=tiers)
    except Exception as e:
        logger.error(f"[AdminTrigger] Event targeting summary failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    counts = {t: 0 for t in ALL_TIERS}
    for p in profiles:
        counts[p.get("tier")] = counts.get(p.get("tier"), 0) + 1

    profiles.sort(key=lambda p: p.get("venue_id") or "")
    page = profiles[offset:offset + limit]

    return {
        "tier_counts": counts,
        "total": len(profiles),
        "limit": limit,
        "offset": offset,
        "venues": page,
    }
