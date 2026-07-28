"""Archive venue photos to the versioned S3 media prefix.

An admin-triggered pipeline that downloads Google Places photos for a chosen set
of venues and stores the image bytes under a run-scoped `media/` prefix.

Two orderings carry the cost guarantees and must not be rearranged:

1. Configuration is validated and the target prefix resolved BEFORE any Google
   call, so a bad request costs nothing.
2. The already-archived check runs BEFORE the Google call, because Google bills
   per photo request. A skip that happens after the fetch has already spent the
   money it was meant to save.

The second guarantee is why `skip_scope` exists. Every run writes to its own
`run_id=` prefix, so "have I already archived this venue?" cannot be asked of the
prefix being written to — it is empty by construction. Asked there, the check
would always say no and every run would re-buy the entire catalog. It is asked of
the most recent EXISTING run instead.

Failure isolation is per venue and per photo — one bad venue, or one bad photo
within a venue, never aborts a run that may be paying for thousands of others.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional

import httpx

from app.dao.media_archive_store import MEDIA_ROOT, RETRIEVED_ROOT
from app.metrics import (
    MEDIA_ARCHIVE_BYTES_STORED_TOTAL,
    MEDIA_ARCHIVE_ESTIMATED_COST_USD,
    MEDIA_ARCHIVE_GOOGLE_CALLS_TOTAL,
    MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP,
    MEDIA_ARCHIVE_PHOTOS_STORED_TOTAL,
    MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL,
    MEDIA_ARCHIVE_RATE_LIMIT_WAIT_SECONDS,
    MEDIA_ARCHIVE_RUNS_TOTAL,
    MEDIA_ARCHIVE_RUN_DURATION_SECONDS,
    MEDIA_ARCHIVE_THROTTLED_TOTAL,
    MEDIA_ARCHIVE_VENUES_SELECTED,
    MEDIA_ARCHIVE_VENUES_TOTAL,
    MEDIA_ARCHIVE_VENUES_TRUNCATED_TOTAL,
    MEDIA_ARCHIVE_VENUES_WITH_MEDIA,
)
from app.services.venue_eligibility import haversine_km
from app.utils.rate_limiter import AsyncRateLimiter, backoff_delay, is_throttled
from app.utils.recife_time import recife_today

logger = logging.getLogger(__name__)

# Sources live in their own module: each owns its config, its fetcher and its
# cost model, and the admin panel renders the catalog from there.
from app.services.archive_sources import (  # noqa: E402
    ARCHIVE_SOURCES,
    SOURCE_APIFY_GMAPS,
    SOURCE_GOOGLE_PHOTOS,
    SUPPORTED_SOURCES,
    ArchiveCreditExhausted,
    get_source,
)

PATH_MODE_NEW_RUN = "new_run"
# Retained with its ORIGINAL day-scoped meaning rather than aliased to new_run: a
# saved config that says new_day must keep writing where it always did. New runs
# default to new_run; the two layouts coexist and both are discoverable.
PATH_MODE_NEW_DAY = "new_day"
PATH_MODE_APPEND_LATEST = "append_latest"
PATH_MODE_OVERRIDE = "override"
PATH_MODES = (
    PATH_MODE_NEW_RUN, PATH_MODE_NEW_DAY, PATH_MODE_APPEND_LATEST, PATH_MODE_OVERRIDE
)

SKIP_SCOPE_LATEST_RUN = "latest_run"
SKIP_SCOPE_THIS_RUN = "this_run"
SKIP_SCOPE_NONE = "none"
SKIP_SCOPES = (SKIP_SCOPE_LATEST_RUN, SKIP_SCOPE_THIS_RUN, SKIP_SCOPE_NONE)

ELIGIBILITY_ALL = "all"
ELIGIBILITY_VENUE_IDS = "venue_ids"
ELIGIBILITY_POINT_RADIUS = "point_radius"
ELIGIBILITY_MODES = (ELIGIBILITY_ALL, ELIGIBILITY_VENUE_IDS, ELIGIBILITY_POINT_RADIUS)

MAX_RADIUS_KM = 500.0

# Returned when a fetch exhausted its retries. Distinct from None/[], which
# mean the source answered and simply had no photos — a failure and a miss
# are different outcomes and must not be counted together.
FETCH_FAILED = object()

# Crockford base32 — no I/L/O/U, so a run id can be read aloud or copied out of a
# bucket listing without ambiguity.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ESTIMATE_CAVEAT = (
    "This is an upper-bound estimate and may be wrong: it assumes every selected "
    "venue returns the maximum number of photos, and the per-request price is a "
    "configured value that has not been verified against Google's current rate "
    "card. Actual cost is usually lower, never higher."
)


class InvalidArchivePath(Exception):
    """The requested target prefix is unusable.

    Raised BEFORE any Google call: the writer's IAM policy only covers `media/*`,
    so a prefix outside it would fail every put — after the photos had already
    been paid for.
    """


class InvalidArchiveConfig(InvalidArchivePath):
    """The run configuration is unusable.

    Subclasses InvalidArchivePath deliberately: both mean "this request was
    rejected and nothing was spent", and callers that already guard the path case
    keep working unchanged.
    """


class PhotoTooLarge(Exception):
    """A single image exceeded the per-photo byte cap and was not stored."""


def parse_venue_ids(raw: Any) -> list[str]:
    """Parse the comma-separated venue id field.

    Trims, drops empties, and de-duplicates while preserving the operator's
    order, so a hand-pasted list behaves predictably. A list is accepted too, for
    callers that already have one.
    """
    if raw is None:
        return []
    items: Iterable[str]
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        vid = str(item).strip()
        if vid and vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


def day_prefix(source: str, day: str) -> str:
    """Legacy day-scoped prefix. Kept for the pre-run-scoping partitions."""
    return f"{MEDIA_ROOT}/source={source}/dt={day}/"


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_run_id(when: Optional[datetime] = None) -> str:
    """A ULID: 48-bit millisecond timestamp, then 80 bits of randomness.

    Time-ordered ON PURPOSE. The run id is the only thing distinguishing two
    runs on the same day, and `_latest_archive_prefix` finds the newest run by
    taking the last key from a LISTING — the writer role has no `GetObject`, so
    it can never read a pointer to find out. A random id (uuid4) sorts randomly
    and picks the wrong run about two thirds of the time, which would silently
    break both `append_latest` and the `skip_scope` cost gate.

    The timestamp is encoded independently of the random half so the two never
    share a base32 character; interleaving them reintroduces the mis-ordering
    this exists to prevent.
    """
    moment = when or datetime.now(timezone.utc)
    millis = int(moment.timestamp() * 1000)
    return _b32(millis, 10) + _b32(int.from_bytes(os.urandom(10), "big"), 16)


def run_prefix(source: str, when: datetime, run_id: str) -> str:
    """Run-scoped prefix.

    Every segment is fixed-width and zero-padded, and the run id is itself
    time-ordered, so the keys sort chronologically as plain strings — which is
    what lets the latest run be found by listing alone, without `GetObject`.
    """
    return (
        f"{RETRIEVED_ROOT}/source={source}/"
        f"year={when:%Y}/month={when:%m}/day={when:%d}/"
        f"run_id={run_id}/"
    )


def validate_override(prefix: Any) -> str:
    """Normalise and bounds-check an operator-supplied prefix.

    Must be non-empty and must resolve inside `media/`. Traversal is rejected on
    the NORMALISED path, so `media/../raw/` is caught rather than passing a naive
    prefix check.
    """
    text = str(prefix or "").strip()
    if not text:
        raise InvalidArchivePath("path_override is required when path_mode is 'override'")
    normalised = posixpath.normpath(text.lstrip("/"))
    if normalised in (".", "/") or not (
        normalised == MEDIA_ROOT or normalised.startswith(f"{MEDIA_ROOT}/")
    ):
        raise InvalidArchivePath(
            f"path_override must stay under '{MEDIA_ROOT}/': {prefix!r}"
        )
    return normalised.rstrip("/") + "/"


def photo_token(url: str) -> str:
    """The size-independent part of a Google photo URL.

    A lh3.googleusercontent.com URL is `<token>=<size spec>`, and the size spec
    varies by who asked: the extractor returns `=w1920-h1080-k-no` where the
    photos engine returns `=s0` for the very same image. The token is the photo;
    the suffix is a rendering request.
    """
    return str(url or "").split("=")[0]


def photo_id_for(photo: dict) -> str:
    """Stable id for a photo.

    Derived from Google's photo resource name when available, else from the URL
    TOKEN rather than the whole URL — so one image gets one id no matter which
    source fetched it or at what size. That is what lets the same photo be
    recognised across sources: SearchApi knows a photo's category and Apify
    knows its upload date, and neither knows the other, so the shared id is the
    only thing that can join them.

    Hashed to a fixed length because the raw resource name is long and not
    key-safe.
    """
    seed = photo.get("photo_name") or photo_token(photo.get("url")) or ""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _positive_int(value: Any, field: str, *, default: int, maximum: int = 100_000) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise InvalidArchiveConfig(f"{field} must be a whole number, got {value!r}")
    if parsed < 1:
        raise InvalidArchiveConfig(f"{field} must be at least 1, got {parsed}")
    if parsed > maximum:
        raise InvalidArchiveConfig(f"{field} must be at most {maximum}, got {parsed}")
    return parsed


def _float_in(value: Any, field: str, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise InvalidArchiveConfig(f"{field} must be a number, got {value!r}")
    if not (low <= parsed <= high):
        raise InvalidArchiveConfig(
            f"{field} must be between {low} and {high}, got {parsed}"
        )
    return parsed


def parse_config(config: Optional[dict], *, default_max_venues: int,
                 default_max_photos: int) -> dict:
    """Validate and normalise a run configuration.

    Runs in full before anything is resolved or fetched, so an operator's mistake
    is reported as a rejection rather than as a partial, already-billed run.
    Accepts the pre-run-scoping shape (`sources` list, top-level `venue_ids`,
    `new_day`) so saved configs keep working.
    """
    cfg = dict(config or {})

    source = cfg.get("source")
    if not source:
        sources = cfg.get("sources") or [SOURCE_GOOGLE_PHOTOS]
        source = str(sources[0]) if sources else SOURCE_GOOGLE_PHOTOS
    source = str(source)
    if source not in SUPPORTED_SOURCES:
        raise InvalidArchiveConfig(
            f"unsupported source {source!r}; expected one of {list(SUPPORTED_SOURCES)}"
        )

    path_mode = str(cfg.get("path_mode") or PATH_MODE_NEW_RUN).strip()
    if path_mode not in PATH_MODES:
        raise InvalidArchiveConfig(
            f"unknown path_mode {path_mode!r}; expected one of {list(PATH_MODES)}"
        )

    skip_scope = str(cfg.get("skip_scope") or SKIP_SCOPE_LATEST_RUN).strip()
    if skip_scope not in SKIP_SCOPES:
        raise InvalidArchiveConfig(
            f"unknown skip_scope {skip_scope!r}; expected one of {list(SKIP_SCOPES)}"
        )
    overwrite = bool(cfg.get("overwrite"))
    if skip_scope == SKIP_SCOPE_NONE and not overwrite:
        # Disabling the skip is how a run re-buys the whole catalog. It must be a
        # deliberate two-key action, not one dropdown.
        raise InvalidArchiveConfig(
            "skip_scope 'none' re-fetches every selected venue and requires "
            "overwrite to be enabled"
        )

    eligibility = dict(cfg.get("eligibility") or {})
    if not eligibility:
        # Pre-run-scoping shape: a top-level id list meant "only these venues".
        legacy_ids = parse_venue_ids(cfg.get("venue_ids"))
        eligibility = (
            {"mode": ELIGIBILITY_VENUE_IDS, "venue_ids": legacy_ids}
            if legacy_ids else {"mode": ELIGIBILITY_ALL}
        )
    mode = str(eligibility.get("mode") or ELIGIBILITY_ALL).strip()
    if mode not in ELIGIBILITY_MODES:
        raise InvalidArchiveConfig(
            f"unknown eligibility mode {mode!r}; expected one of {list(ELIGIBILITY_MODES)}"
        )
    resolved_eligibility: dict[str, Any] = {"mode": mode}
    if mode == ELIGIBILITY_VENUE_IDS:
        ids = parse_venue_ids(eligibility.get("venue_ids"))
        if not ids:
            raise InvalidArchiveConfig(
                "eligibility mode 'venue_ids' requires at least one venue id"
            )
        resolved_eligibility["venue_ids"] = ids
    elif mode == ELIGIBILITY_POINT_RADIUS:
        resolved_eligibility["lat"] = _float_in(eligibility.get("lat"), "lat", -90, 90)
        resolved_eligibility["lon"] = _float_in(eligibility.get("lon"), "lon", -180, 180)
        radius = eligibility.get("radius_km")
        try:
            radius_value = float(radius)
        except (TypeError, ValueError):
            raise InvalidArchiveConfig(f"radius_km must be a number, got {radius!r}")
        if not (0 < radius_value <= MAX_RADIUS_KM):
            raise InvalidArchiveConfig(
                f"radius_km must be greater than 0 and at most {MAX_RADIUS_KM}, "
                f"got {radius_value}"
            )
        resolved_eligibility["radius_km"] = radius_value

    return {
        "source": source,
        "path_mode": path_mode,
        "path_override": cfg.get("path_override") or "",
        "max_venues": _positive_int(
            cfg.get("max_venues"), "max_venues", default=default_max_venues
        ),
        "max_photos_per_venue": _positive_int(
            cfg.get("max_photos_per_venue"), "max_photos_per_venue",
            default=default_max_photos, maximum=100,
        ),
        # Optional second cap, applied WITHIN each category. The venue cap still
        # bounds the total, so this can only ever reduce a run, never enlarge it.
        "max_photos_per_category": (
            _positive_int(
                cfg.get("max_photos_per_category"), "max_photos_per_category",
                default=0, maximum=100,
            )
            if cfg.get("max_photos_per_category") not in (None, "", 0)
            else None
        ),
        "eligibility": resolved_eligibility,
        "skip_scope": skip_scope,
        "overwrite": overwrite,
        "dry_run": bool(cfg.get("dry_run")),
        # Per-source fields, validated by the source that owns them.
        "source_config": dict(cfg.get("source_config") or {}),
    }


class HttpPhotoDownloader:
    """Downloads image bytes, bounded by a timeout and a byte cap.

    Reads the body in chunks and aborts as soon as the cap is exceeded, so a
    pathological image cannot consume memory or stall a run that has thousands of
    venues left to process.
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(follow_redirects=True)

    async def download(
        self, url: str, *, timeout: float = 15.0, max_bytes: Optional[int] = None
    ) -> tuple[bytes, str]:
        async with self._client.stream("GET", url, timeout=timeout) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise PhotoTooLarge(f"{url} exceeds {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks), content_type

    async def close(self) -> None:
        await self._client.aclose()


def _media_summary(entries: list[dict]) -> dict:
    """A per-venue digest of what was stored.

    Exists so "is this venue's imagery stale?" and "did we get any menu shots?"
    are answerable from the summary alone, without walking every photo entry.

    `uploaded_at` is the date Google received the photo, NOT when it was taken —
    Google strips EXIF, so no source here can offer a capture date. It is a good
    freshness proxy and a bad provenance one. Absent for sources that do not
    return it (SearchApi returns only image and thumbnail), in which case the
    date fields are null rather than invented.
    """
    dates = sorted(e["uploaded_at"] for e in entries if e.get("uploaded_at"))
    by_category: dict[str, int] = {}
    for entry in entries:
        key = entry.get("category") or "uncategorised"
        by_category[key] = by_category.get(key, 0) + 1
    return {
        "photos": len(entries),
        "by_category": by_category,
        "bytes": sum(e.get("bytes") or 0 for e in entries),
        "oldest_uploaded_at": dates[0] if dates else None,
        "newest_uploaded_at": dates[-1] if dates else None,
        "photos_with_date": len(dates),
    }


class VenuePhotoArchiveService:
    """Downloads venue photos into the versioned media archive."""

    def __init__(
        self,
        *,
        google_places_client,
        venue_dao,
        media_store,
        downloader=None,
        apify_gmaps_extractor_client=None,
        searchapi_photos_client=None,
        settings=None,
        max_photos_per_venue: int = 10,
        photo_timeout_seconds: float = 15.0,
        max_photo_bytes: int = 10 * 1024 * 1024,
        today_provider=None,
        now_provider=None,
        default_max_venues: int = 50,
        rate_per_second: float = 5.0,
        concurrency: int = 4,
        max_retries: int = 3,
        cost_per_1k_usd: float = 7.0,
        run_record_store=None,
        rate_limiter=None,
        sleeper=None,
    ):
        self.google_places_client = google_places_client
        # Named to match ArchiveSource.requires_attr, so availability and
        # dispatch both resolve by attribute rather than by an if-ladder.
        self.apify_gmaps_extractor_client = apify_gmaps_extractor_client
        self.searchapi_photos_client = searchapi_photos_client
        self.venue_dao = venue_dao
        self.media_store = media_store
        self.downloader = downloader or HttpPhotoDownloader()
        self.max_photos_per_venue = max_photos_per_venue
        self.photo_timeout_seconds = photo_timeout_seconds
        self.max_photo_bytes = max_photo_bytes
        self._today = today_provider or (lambda: recife_today().isoformat())
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self.default_max_venues = default_max_venues
        self.max_retries = max(1, int(max_retries))
        self.cost_per_1k_usd = float(cost_per_1k_usd)
        # Sources read their unit prices off settings via getattr with their own
        # defaults, so an absent settings object degrades to those defaults
        # rather than failing. The constructor's cost_per_1k_usd still wins for
        # Google when no settings are injected.
        self._settings = settings if settings is not None else SimpleNamespace(
            google_photo_cost_per_1k_usd=self.cost_per_1k_usd,
        )
        self.concurrency = max(1, int(concurrency))
        self._limiter = rate_limiter or AsyncRateLimiter(rate_per_second)
        self._sleep = sleeper or asyncio.sleep
        # Redis in production; an in-memory dict keeps the pipeline (and its
        # tests) working when no store is wired, since a lost run record must
        # never fail a run that already spent money.
        self._records = run_record_store if run_record_store is not None else {}

    # ── target prefix ────────────────────────────────────────────────────────
    async def resolve_prefix(self, source: str, config: dict) -> str:
        mode = str(config.get("path_mode") or PATH_MODE_NEW_RUN).strip()
        if mode not in PATH_MODES:
            raise InvalidArchiveConfig(
                f"unknown path_mode {mode!r}; expected one of {list(PATH_MODES)}"
            )
        if mode == PATH_MODE_OVERRIDE:
            return validate_override(config.get("path_override"))
        if mode == PATH_MODE_NEW_DAY:
            return day_prefix(source, self._today())
        if mode == PATH_MODE_APPEND_LATEST:
            latest = await self._latest_archive_prefix(source)
            if latest:
                return latest
            # Defined fallback rather than an error: the first-ever run has no
            # partition to append to, and failing it would be surprising.
            logger.info(
                "[VenuePhotoArchive] append_latest found no existing partition; "
                "starting a new run"
            )
        now = self._now()
        return run_prefix(source, now, new_run_id(now))

    async def _latest_archive_prefix(self, source: str) -> Optional[str]:
        """The most recent EXISTING partition for a source, either layout.

        Run-scoped partitions win when present; day-scoped ones remain
        discoverable so a catalog archived before run scoping is still found by
        `append_latest` and, crucially, still suppresses re-buying its venues.
        """
        try:
            runs = await self.media_store.list_run_prefixes(source)
            if runs:
                return runs[-1]
        except Exception as e:
            logger.error(f"[VenuePhotoArchive] listing run prefixes failed: {e}")
        try:
            days = await self.media_store.list_day_partitions(source)
        except Exception as e:
            logger.error(f"[VenuePhotoArchive] listing day partitions failed: {e}")
            return None
        return day_prefix(source, days[-1]) if days else None

    async def _skip_reference_prefix(self, source: str, cfg: dict, write_prefix: str):
        """Where "have I already archived this venue?" is asked.

        `latest_run` deliberately resolves to the most recent EXISTING run, which
        for a fresh `new_run` is the previous one and for `append_latest` is the
        prefix being written to. Both are the answer the operator means.
        """
        scope = cfg["skip_scope"]
        if scope == SKIP_SCOPE_NONE:
            return None
        if scope == SKIP_SCOPE_THIS_RUN:
            return write_prefix
        return await self._latest_archive_prefix(source) or write_prefix

    # ── venue selection ──────────────────────────────────────────────────────
    def _catalog(self) -> list[str]:
        try:
            return list(self.venue_dao.list_active_venue_ids() or [])
        except Exception as e:
            logger.error(f"[VenuePhotoArchive] listing active venues failed: {e}")
            return []

    def _coords_for(self, venue_id: str):
        try:
            venue = self.venue_dao.get_venue(venue_id)
        except Exception:
            return None
        if venue is None:
            return None
        lat = getattr(venue, "venue_lat", None)
        lng = getattr(venue, "venue_lng", None)
        if lat is None and isinstance(venue, dict):
            lat, lng = venue.get("venue_lat"), venue.get("venue_lng")
        return (lat, lng) if lat is not None and lng is not None else None

    def select_venues(self, cfg: dict) -> tuple[list[str], list[str], int]:
        """Return (selected, unknown_ids, eligible_before_truncation)."""
        eligibility = cfg["eligibility"]
        mode = eligibility["mode"]
        catalog = self._catalog()
        unknown: list[str] = []

        if mode == ELIGIBILITY_VENUE_IDS:
            known = set(catalog)
            requested = eligibility["venue_ids"]
            # An unknown id is reported, never fatal: one typo in a 40-id paste
            # must not throw away the whole run.
            eligible = [vid for vid in requested if vid in known]
            unknown = [vid for vid in requested if vid not in known]
        elif mode == ELIGIBILITY_POINT_RADIUS:
            lat, lon, radius = (
                eligibility["lat"], eligibility["lon"], eligibility["radius_km"]
            )
            eligible = []
            for vid in catalog:
                coords = self._coords_for(vid)
                if coords is None:
                    continue  # no coordinates cannot be inside a radius
                if haversine_km(lat, lon, coords[0], coords[1]) <= radius:
                    eligible.append(vid)
        else:
            eligible = catalog

        cap = cfg["max_venues"]
        return eligible[:cap], unknown, len(eligible)

    # ── estimate ─────────────────────────────────────────────────────────────
    async def estimate(self, config: Optional[dict] = None) -> dict:
        """Price a run without making a single Google request."""
        cfg = parse_config(
            config,
            default_max_venues=self.default_max_venues,
            default_max_photos=self.max_photos_per_venue,
        )
        source = cfg["source"]
        selected, unknown, eligible_total = self.select_venues(cfg)

        after_skip = len(selected)
        if not cfg["overwrite"] and cfg["skip_scope"] != SKIP_SCOPE_NONE:
            reference = await self._latest_archive_prefix(source)
            if reference:
                remaining = []
                for vid in selected:
                    try:
                        if not await self.media_store.exists_for_venue(reference, vid):
                            remaining.append(vid)
                    except Exception:
                        remaining.append(vid)
                after_skip = len(remaining)

        photos_max = cfg["max_photos_per_venue"]
        descriptor = get_source(source)
        # Each source prices differently: Google per request (1 Details + N
        # photos per venue), the extractor per place scraped regardless of how
        # many photos come back. One formula cannot describe both.
        calls, unit_label = descriptor.estimate_units(after_skip, cfg)
        cost = round(calls * descriptor.unit_cost_usd(self._settings, cfg), 4)
        estimate = {
            "source": source,
            "venues_eligible": eligible_total,
            "venues_selected": len(selected),
            "venues_after_skip": after_skip,
            "photos_max": photos_max,
            "est_google_calls": calls,   # kept: the admin panel reads this name
            "est_units": calls,
            "est_unit_label": unit_label,
            "est_cost_usd": cost,
            "est_bytes": calls * 300_000,  # ~300KB per photo, observed order
            "est_duration_seconds": round(calls / max(self._limiter.rate, 0.001), 1),
            "unknown_venue_ids": unknown,
            "assumptions": [
                f"every venue returns the maximum of {photos_max} photos",
                descriptor.cost_note,
                "~300KB per stored image",
            ],
            "caveat": ESTIMATE_CAVEAT,
        }
        MEDIA_ARCHIVE_ESTIMATED_COST_USD.labels(source=source).set(cost)
        logger.info(
            f"[VenuePhotoArchive] estimate: source={source} "
            f"selected={len(selected)} after_skip={after_skip} "
            f"calls={calls} cost=${cost}"
        )
        return estimate

    # ── run records ──────────────────────────────────────────────────────────
    def get_run_record(self, job_id: str) -> Optional[dict]:
        try:
            return self._records.get(job_id)
        except Exception as e:
            logger.warning(f"[VenuePhotoArchive] run record read failed: {e}")
            return None

    def _save_run_record(self, job_id: str, record: dict) -> None:
        try:
            self._records[job_id] = record
        except Exception as e:
            # A lost record must never fail a run whose photos are already stored.
            logger.warning(f"[VenuePhotoArchive] run record write failed: {e}")

    # ── run ──────────────────────────────────────────────────────────────────
    async def run(self, config: Optional[dict] = None) -> dict:
        cfg = parse_config(
            config,
            default_max_venues=self.default_max_venues,
            default_max_photos=self.max_photos_per_venue,
        )
        source = cfg["source"]
        # A source whose dependency is unwired must fail here — loudly and for
        # free — rather than partway through a run the operator is paying for.
        if self._source_client(source) is None:
            raise InvalidArchiveConfig(
                f"source {source!r} is unavailable: "
                f"{ARCHIVE_SOURCES[source].unavailable_reason}"
            )
        # The job id IS the run id: it appears in the path, and resolving the
        # latest run by listing depends on it sorting chronologically, which a
        # uuid4 does not. One time-ordered id serves both.
        job_id = str((config or {}).get("job_id") or new_run_id(self._now()))

        started = time.perf_counter()
        # Resolve the prefix before selecting or fetching: an invalid override
        # must abort before a single billable Google call is made.
        prefix = await self.resolve_prefix(source, cfg)
        run_id = prefix.split("run_id=")[1].rstrip("/") if "run_id=" in prefix else None
        selected, unknown, eligible_total = self.select_venues(cfg)
        reference_prefix = await self._skip_reference_prefix(source, cfg, prefix)

        MEDIA_ARCHIVE_VENUES_SELECTED.labels(source=source).set(len(selected))
        truncated_from = eligible_total if eligible_total > len(selected) else 0
        if truncated_from:
            MEDIA_ARCHIVE_VENUES_TRUNCATED_TOTAL.labels(source=source).inc(
                truncated_from - len(selected)
            )

        summary: dict[str, Any] = {
            "job_id": job_id,
            "run_id": run_id,
            "source": source,
            "prefix": prefix,
            "overwrite": cfg["overwrite"],
            "skip_scope": cfg["skip_scope"],
            "dry_run": cfg["dry_run"],
            "considered": len(selected),
            "eligible": eligible_total,
            "truncated_from": truncated_from,
            "archived": 0,
            "skipped_existing": 0,
            "no_place_id": 0,
            "failed": 0,
            "photos_stored": 0,
            "photo_failures": 0,
            "bytes_stored": 0,
            "source_calls": 0,
            "no_match": 0,
            "info_stored": 0,
            "info_only": 0,
            "credit_exhausted": False,
            "aborted": False,
            "throttled": 0,
            "unknown_venue_ids": unknown,
            "config": cfg,
        }
        if unknown:
            logger.warning(
                f"[VenuePhotoArchive] job={job_id} {len(unknown)} unknown venue "
                f"id(s) ignored: {unknown[:10]}"
            )

        logger.info(
            f"[VenuePhotoArchive] job={job_id} starting: source={source} "
            f"prefix={prefix} venues={len(selected)} of {eligible_total} "
            f"max_photos={cfg['max_photos_per_venue']} skip_scope={cfg['skip_scope']} "
            f"overwrite={cfg['overwrite']} dry_run={cfg['dry_run']}"
        )

        if cfg["dry_run"]:
            # The safe rehearsal: everything that costs nothing, nothing that does.
            summary["estimate"] = await self.estimate(config)
            summary["duration_seconds"] = round(time.perf_counter() - started, 2)
            logger.info(
                f"[VenuePhotoArchive] job={job_id} dry run complete; nothing written"
            )
            self._save_run_record(job_id, summary)
            return summary

        semaphore = asyncio.Semaphore(self.concurrency)

        async def _guarded(venue_id: str) -> None:
            # Once the source's budget is exhausted, stop starting new venues
            # rather than generating one failure per remaining venue.
            if summary.get("credit_exhausted"):
                return
            async with semaphore:
                try:
                    await self._archive_venue(
                        venue_id, source, prefix, reference_prefix, cfg, summary
                    )
                except ArchiveCreditExhausted as e:
                    # Not a per-venue failure: the budget is gone, so every
                    # remaining venue would fail the same way. Record it and let
                    # the run finish reporting what it did manage.
                    if not summary.get("credit_exhausted"):
                        logger.error(
                            f"[VenuePhotoArchive] job={job_id} stopping: {e}"
                        )
                    summary["credit_exhausted"] = True
                except Exception as e:  # noqa: BLE001 — one venue must not end the run
                    summary["failed"] += 1
                    MEDIA_ARCHIVE_VENUES_TOTAL.labels(
                        source=source, result="failed"
                    ).inc()
                    logger.error(
                        f"[VenuePhotoArchive] job={job_id} venue {venue_id} failed: {e}"
                    )

        try:
            await asyncio.gather(*(_guarded(vid) for vid in selected))
        except asyncio.CancelledError:
            # An operator stopped the run. Record what it managed to do before
            # re-raising, so a cancelled run is still accounted for rather than
            # vanishing — it already spent money on the venues it finished.
            summary["aborted"] = True
            summary["duration_seconds"] = round(time.perf_counter() - started, 2)
            self._save_run_record(job_id, summary)
            logger.warning(
                f"[VenuePhotoArchive] job={job_id} cancelled after "
                f"{summary['archived']} archived / {summary['considered']} selected"
            )
            raise

        duration = time.perf_counter() - started
        MEDIA_ARCHIVE_RUN_DURATION_SECONDS.labels(source=source).observe(duration)
        MEDIA_ARCHIVE_RUNS_TOTAL.labels(source=source, status="success").inc()
        MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP.set_to_current_time()
        MEDIA_ARCHIVE_VENUES_WITH_MEDIA.labels(source=source).set(summary["archived"])
        summary["duration_seconds"] = round(duration, 2)

        await self._write_latest_marker(source, prefix, run_id, summary)
        self._save_run_record(job_id, summary)

        logger.info(
            f"[VenuePhotoArchive] job={job_id} done in {duration:.1f}s: "
            f"considered={summary['considered']} archived={summary['archived']} "
            f"skipped={summary['skipped_existing']} no_place_id={summary['no_place_id']} "
            f"failed={summary['failed']} photos={summary['photos_stored']} info={summary['info_stored']} "
            f"source_calls={summary['source_calls']} throttled={summary['throttled']}"
        )
        return summary

    async def _write_latest_marker(self, source, prefix, run_id, summary) -> None:
        """Record where the most recent COMPLETED run landed.

        Written with the run's own prefix, so it tracks the latest dump whatever
        the layout is — it never parses the path.

        It is not what the pipeline itself reads: `_latest_archive_prefix`
        resolves the newest run by LISTING, because the writer role has no
        `GetObject`. The two agree for normal runs and can differ for an
        `override` run, whose prefix sits outside the run partitions — hence
        `path_mode` here, so a reader can tell which kind of run wrote it.

        A dry run never reaches this: a preview must not move the pointer.
        """
        marker = {
            "source": source,
            "prefix": prefix,
            "run_id": run_id,
            "path_mode": summary["config"]["path_mode"],
            "job_id": summary["job_id"],
            "completed_at": self._now().isoformat(),
            "venues_archived": summary["archived"],
            "photos_stored": summary["photos_stored"],
            "bytes_stored": summary["bytes_stored"],
        }
        try:
            await self.media_store.put_latest_marker(source, marker)
        except Exception as e:
            # The images are already durable; losing the marker costs discovery
            # convenience, not data.
            logger.warning(f"[VenuePhotoArchive] latest marker write failed: {e}")

    def _source_client(self, source_id: str):
        """The dependency the chosen source needs, or None when unwired."""
        return getattr(self, ARCHIVE_SOURCES[source_id].requires_attr, None)

    async def _fetch_photos(self, venue_id, venue_ctx, source, cfg, summary):
        """Fetch through the CHOSEN source, paced and retried.

        The pacing, retry and throttle accounting are source-independent; only
        the call itself differs, which is what the registry owns.
        """
        descriptor = get_source(source)
        client = self._source_client(source)
        for attempt in range(1, self.max_retries + 1):
            waited = await self._limiter.acquire()
            if waited:
                MEDIA_ARCHIVE_RATE_LIMIT_WAIT_SECONDS.labels(source=source).observe(waited)
            try:
                summary["source_calls"] += 1
                MEDIA_ARCHIVE_GOOGLE_CALLS_TOTAL.labels(source=source).inc()
                return await descriptor.fetch(client, venue_ctx, cfg)
            except ArchiveCreditExhausted:
                # The source's own budget is gone. Stop the whole run rather
                # than keep calling into an exhausted balance.
                raise
            except Exception as e:  # noqa: BLE001
                if is_throttled(e):
                    summary["throttled"] += 1
                    MEDIA_ARCHIVE_THROTTLED_TOTAL.labels(
                        source=source,
                        reason="429" if getattr(e, "status_code", None) == 429
                        or getattr(getattr(e, "response", None), "status_code", None) == 429
                        else "5xx",
                    ).inc()
                    if attempt < self.max_retries:
                        delay = backoff_delay(attempt)
                        logger.warning(
                            f"[VenuePhotoArchive] {venue_id} throttled "
                            f"(attempt {attempt}/{self.max_retries}); "
                            f"retrying in {delay:.1f}s"
                        )
                        await self._sleep(delay)
                        continue
                logger.error(
                    f"[VenuePhotoArchive] job={summary['job_id']} {source} fetch "
                    f"failed for {venue_id}: {e}"
                )
                return FETCH_FAILED
        return FETCH_FAILED

    async def _archive_venue(
        self, venue_id: str, source: str, prefix: str,
        reference_prefix: Optional[str], cfg: dict, summary: dict,
    ) -> None:
        # 1. Skip BEFORE spending. This ordering is the cost guarantee, and the
        #    reference prefix is why it still holds once runs are versioned.
        if not cfg["overwrite"] and reference_prefix:
            if await self.media_store.exists_for_venue(reference_prefix, venue_id):
                summary["skipped_existing"] += 1
                MEDIA_ARCHIVE_VENUES_TOTAL.labels(
                    source=source, result="skipped_existing"
                ).inc()
                logger.debug(
                    f"[VenuePhotoArchive] job={summary['job_id']} {venue_id} already archived; skipping"
                )
                return

        # 2. A venue the source cannot address can never be fetched — and must
        #    cost nothing. Google needs a place id; the scraper needs a search
        #    string, so the context carries both and the source picks.
        venue_ctx = self._venue_context(venue_id)
        if not venue_ctx.get("google_place_id") and source == SOURCE_GOOGLE_PHOTOS:
            summary["no_place_id"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="no_place_id").inc()
            return
        if not venue_ctx.get("search_query") and source == SOURCE_APIFY_GMAPS:
            summary["no_match"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="no_match").inc()
            return

        # 3. Now, and only now, spend.
        result = await self._fetch_photos(venue_id, venue_ctx, source, cfg, summary)
        if result is FETCH_FAILED:
            summary["failed"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="google_error").inc()
            return
        if not result:
            summary["no_match"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="no_match").inc()
            return

        photos = result.get("photos") or []
        info = result.get("info") or {}

        # 4. Store the info FIRST. It is the cheap half of what was already paid
        #    for, so a venue whose images all fail still keeps the data that did
        #    come back.
        if info:
            try:
                await self.media_store.put_info(
                    prefix=prefix,
                    venue_id=venue_id,
                    info={
                        "venue_id": venue_id,
                        "source": source,
                        "job_id": summary["job_id"],
                        "retrieved_at": self._now().isoformat(),
                        "search_query": venue_ctx.get("search_query"),
                        "place": info,
                    },
                )
                summary["info_stored"] += 1
            except Exception as e:
                logger.error(
                    f"[VenuePhotoArchive] job={summary['job_id']} info write "
                    f"failed for {venue_id}: {e}"
                )

        if not photos:
            # Info kept, no images available. Not a failure and not a miss.
            summary["info_only"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="info_only").inc()
            return

        entries = []
        per_category = cfg.get("max_photos_per_category")
        per_venue = cfg["max_photos_per_venue"]
        taken: dict[str, int] = {}
        for photo in photos or []:
            # "Photos per venue" must mean per VENUE. A category-aware source
            # returns up to its own limit for EACH category, so without this the
            # total silently multiplied by the number of categories chosen.
            if len(entries) >= per_venue:
                break
            category = photo.get("category")
            if per_category:
                # Ordering is Google's own ranking, so taking the first N of a
                # category takes its best N.
                key = category or "uncategorised"
                if taken.get(key, 0) >= per_category:
                    continue
                taken[key] = taken.get(key, 0) + 1
            entry = await self._store_photo(venue_id, source, prefix, photo, summary)
            if entry is not None:
                entries.append(entry)

        if entries:
            try:
                await self.media_store.put_manifest(
                    prefix=prefix,
                    venue_id=venue_id,
                    manifest={
                        "venue_id": venue_id,
                        "source": source,
                        "google_place_id": venue_ctx.get("google_place_id"),
                        "search_query": venue_ctx.get("search_query"),
                        "job_id": summary["job_id"],
                        "media": _media_summary(entries),
                        "photos": entries,
                    },
                )
            except Exception as e:
                # The images are already stored; losing the manifest costs the
                # attribution, so it is loud but does not fail the venue.
                logger.error(
                    f"[VenuePhotoArchive] job={summary['job_id']} manifest write failed for {venue_id}: {e}"
                )

        summary["archived"] += 1
        MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="archived").inc()

    def _venue_context(self, venue_id: str) -> dict:
        """What a source needs to address this venue.

        Google works from a place id; the extractor is a search API and works
        from a name + address string. Both are resolved here so the sources
        stay ignorant of the DAO.
        """
        ctx: dict = {"venue_id": venue_id, "google_place_id": self._place_id_for(venue_id)}
        try:
            venue = self.venue_dao.get_venue(venue_id)
        except Exception:
            venue = None
        name = getattr(venue, "venue_name", None) if venue else None
        address = getattr(venue, "venue_address", None) if venue else None
        query = " ".join(part for part in (name, address) if part).strip()
        ctx["search_query"] = query or None
        ctx["venue_name"] = name
        return ctx

    def _place_id_for(self, venue_id: str) -> Optional[str]:
        try:
            vibe = self.venue_dao.get_vibe_attributes(venue_id)
        except Exception as e:
            logger.warning(
                f"[VenuePhotoArchive] vibe-attrs read failed for {venue_id}: {e}"
            )
            return None
        return getattr(vibe, "google_place_id", None) if vibe else None

    async def _store_photo(
        self, venue_id: str, source: str, prefix: str, photo: dict, summary: dict
    ) -> Optional[dict]:
        url = photo.get("url")
        if not url:
            return None
        photo_id = photo_id_for(photo)
        try:
            data, content_type = await self.downloader.download(
                url,
                timeout=self.photo_timeout_seconds,
                max_bytes=self.max_photo_bytes,
            )
        except PhotoTooLarge as e:
            summary["photo_failures"] += 1
            MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL.labels(
                source=source, reason="too_large"
            ).inc()
            logger.warning(f"[VenuePhotoArchive] job={summary['job_id']} {venue_id} photo too large: {e}")
            return None
        except Exception as e:
            summary["photo_failures"] += 1
            MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL.labels(
                source=source, reason="download_error"
            ).inc()
            logger.warning(f"[VenuePhotoArchive] job={summary['job_id']} {venue_id} photo download failed: {e}")
            return None

        try:
            key = await self.media_store.put_image(
                prefix=prefix,
                venue_id=venue_id,
                photo_id=photo_id,
                data=data,
                content_type=content_type,
                category=photo.get("category"),
            )
        except Exception as e:
            summary["photo_failures"] += 1
            MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL.labels(
                source=source, reason="store_error"
            ).inc()
            logger.warning(f"[VenuePhotoArchive] job={summary['job_id']} {venue_id} photo store failed: {e}")
            return None

        summary["photos_stored"] += 1
        summary["bytes_stored"] += len(data)
        MEDIA_ARCHIVE_PHOTOS_STORED_TOTAL.labels(source=source).inc()
        MEDIA_ARCHIVE_BYTES_STORED_TOTAL.labels(source=source).inc(len(data))
        return {
            "photo_id": photo_id,
            "key": key,
            "content_type": content_type,
            "bytes": len(data),
            "author_name": photo.get("author_name"),
            "category": photo.get("category"),
            "uploaded_at": photo.get("uploaded_at"),
            "source_url": url,
            "photo_name": photo.get("photo_name"),
        }
