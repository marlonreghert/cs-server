"""Archive each venue's Instagram profile picture and make it app-servable.

See plans/260816_instagram-profile-photo-hero.md.

For every servable venue holding a confirmed Instagram handle: scrape the
profile through Apify, download the picture, content-address it, upload it to
the media bucket, and persist the resulting CloudFront URL to
`instagram.profile_photo`. The off-loop projector then mirrors that row into
`venue_profile_photo_v1:{venue_id}` so vibes_bot can put a photo on a list card
with no serve-time Google call at all.

## The scheduled job is BACKFILL-ONLY (operator decision, 2026-08-16)

A profile picture, once captured, is good indefinitely. There is therefore NO
time-based refresh window: a venue that already has a photo row is skipped
however old that row is (`skipped_has_photo`). The 30-day re-scrape this job
originally shipped with bought almost nothing and cost the whole catalog in
Apify units every month, so it was deleted outright rather than defaulted off —
a dead `instagram_profile_photo_refresh_days` setting would still read like a
promise that a monthly refresh happens, and it would not.

Steady state after the backfill completes is therefore ≈$0/month. The only
recurring spend left is genuinely new venues, retries of past failures once
their negative-cache window expires, and venues whose handle was corrected.

Replacing photos the catalog already holds is an explicit operator action —
`MODE_REFRESH_ALL`, reachable only from the admin trigger and priced first by
`estimate()`. `run()` defaults to `MODE_BACKFILL`, and the APScheduler job in
`main.py` passes `MODE_BACKFILL` explicitly, so a cron can never widen the
spend.

## The ordering that carries the cost guarantee (do not rearrange)

Inherited verbatim from `VenuePhotoArchiveService`, whose module docstring
states it as a rule the archive pipeline must not violate:

1. Configuration is validated BEFORE any billed call, so a misconfigured run
   costs nothing.
2. **The already-stored check runs BEFORE the paid scrape.** A skip that
   happens after the fetch has already spent the money it was meant to save.

Here that means every gate runs inside `select()` — bulk RDS reads, zero
provider calls — and only the venues that survive it are ever handed to Apify.
`venue_profile_photo_apify_calls_total` is the proof: a venue skipped by the
gate cannot move that counter, which is why the acceptance criterion is written
against the counter and not against a log line.

The per-run cap is applied to the survivors of those gates, not to the raw
servable list. Capping earlier would let a catalog of already-photographed
venues consume the whole run budget and back-fill nothing.

## One selection function, shared by the estimate and the run

`select()` is the ONLY place that decides which venues cost money, and both
`run()` and `estimate()` call it. That is deliberate and load-bearing: an
estimate the operator approves is worthless if the run can scrape a different
set, and two copies of a gate drift the moment one of them is edited. The BDD
asserts the equality directly — the number of billed Apify calls a run makes
equals the `venues_to_scrape` the estimate reported for the same fixture.

`estimate()` performs no `await` at all and touches no provider client, so
"the estimate spends nothing" is a structural property of the code, not a
convention. It also lives on its own admin route rather than as a flag on the
trigger, so an estimate can never accidentally start a run.

## The negative cache (the other half of the cost gate)

A venue with NO photo row is unconditionally due, so an absence has to be
recorded somewhere or it is bought again every run. A profile with no picture,
a handle that 404s, an image that will not download — none of those ever
produce a photo row, and without a record of the attempt the same venue is
re-scraped and re-billed on every tick, forever. Worse, once
`max_venues_per_run` such venues accumulate they fill the whole run budget and
no venue that COULD get a photo ever gets one.

So every attempt that produced no photo writes a row to
`instagram.profile_photo_attempt`, and the selection gate skips a venue whose
last attempt is younger than `instagram_profile_photo_retry_days` — the same
negative-caching shape handle discovery already uses via
`instagram_not_found_cache_ttl_days`. It is a SEPARATE table from the photo
row on purpose: a failed refresh must never overwrite the row a venue's live
hero is projected from, and `venue_profile_photo_v1:{venue_id}` must keep
meaning exactly one thing — this venue has a real stored photo. The attempt
table has no entry in `RedisProjectionService._REBUILD_MODELS`, so it cannot
produce a Redis key at all.

A stored photo clears the attempt row, and a handle change ignores it: an old
failure says nothing about a handle that has since been corrected.

The negative cache applies in BOTH modes, `refresh_all` included. Reasoning,
recorded because the opposite choice is defensible: `refresh_all` exists to
replace photos the catalog HAS, and a venue in the negative cache has none —
so bypassing it would buy a scrape that the next scheduled backfill will make
anyway once the window expires, while inflating the very bill the operator is
being asked to approve. The escape hatch for the other intent already exists
and is deliberately a settings change rather than a dialog click: set
`instagram_profile_photo_retry_days` to 0 to retry every failed venue now
(e.g. straight after fixing a bucket policy that failed the whole catalog).
The estimate reports the suppressed count under `skipped_recent_failure`, so
the choice is visible rather than silent.

## What is never a failure

A venue with no handle, a profile with no picture, or a failed fetch produces
an ABSENCE — no photo row, no key, no error (only the attempt row above, which
is projected nowhere). Nothing partial is ever persisted: the photo row is
written only after the S3 upload has returned, so a Redis key can never point
at an object that does not exist.

Failure isolation is per venue. One bad venue never aborts a run that is paying
for the others. The single exception is an exhausted Apify balance, which stops
the run deliberately — continuing would only produce more 402s.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.config import settings
from app.metrics import (
    VENUE_PROFILE_PHOTO_APIFY_CALLS_TOTAL,
    VENUE_PROFILE_PHOTO_BYTES_STORED_TOTAL,
    VENUE_PROFILE_PHOTO_ESTIMATED_COST_USD,
    VENUE_PROFILE_PHOTO_ESTIMATE_COST_USD,
    VENUE_PROFILE_PHOTO_RUNS_TOTAL,
    VENUE_PROFILE_PHOTO_RUN_DURATION_SECONDS,
    VENUE_PROFILE_PHOTO_VENUES_TOTAL,
)
from app.models.instagram import (
    VenueInstagramProfilePhoto,
    VenueInstagramProfilePhotoAttempt,
)
from app.services.image_edge_color import sample_edge_color
from app.services.pipeline_run_registry import new_run_id

logger = logging.getLogger(__name__)

PROFILE_PHOTO_TABLE = "instagram.profile_photo"
PROFILE_PHOTO_ATTEMPT_TABLE = "instagram.profile_photo_attempt"

# ── modes ───────────────────────────────────────────────────────────────────
# The scheduled job runs BACKFILL and nothing else. `refresh_all` is manual by
# construction: `main.py` passes MODE_BACKFILL explicitly to the APScheduler
# job, and the only other way in is the admin trigger, which an operator
# reaches after `estimate()` has priced the run.
MODE_BACKFILL = "backfill"
MODE_REFRESH_ALL = "refresh_all"
# Fill `edge_color` on photo rows that already exist. FREE: it never touches
# Apify and never uploads — it re-reads each object over the public CloudFront
# URL the row already holds. cs-server has `s3:PutObject` and nothing else on
# that bucket (infra/media/main.tf:279), so an S3 read-back is not merely
# undesirable here, it is impossible without a Terraform change. The CDN is the
# route, and it is the same anonymous HTTPS GET every installed app already
# makes.
#
# Manual only. `main.py` passes MODE_BACKFILL explicitly to the APScheduler
# job, so a cron can never enter this mode either.
MODE_BACKFILL_EDGE_COLOR = "edge_color"
MODES = (MODE_BACKFILL, MODE_REFRESH_ALL, MODE_BACKFILL_EDGE_COLOR)


class InvalidProfilePhotoMode(ValueError):
    """An unrecognised mode. Raised instead of quietly defaulting: the two
    modes differ by the entire catalog's worth of Apify units, so a typo must
    be rejected rather than resolved."""


def parse_mode(config: Optional[dict]) -> str:
    """The one place a mode is read, shared by the run and the estimate.

    Absent config means MODE_BACKFILL — that is the scheduler's call shape, so
    the default has to be the free one.
    """
    raw = (config or {}).get("mode")
    if raw is None or raw == "":
        return MODE_BACKFILL
    mode = str(raw).strip().lower()
    if mode not in MODES:
        raise InvalidProfilePhotoMode(
            f"unknown mode {raw!r}; expected one of {', '.join(MODES)}"
        )
    return mode


# Outcome buckets. Every venue the run touches lands in exactly one, and the
# set is closed — an outcome label that never appears in Prometheus is itself
# the diagnostic that its code path never ran.
OUTCOME_STORED = "stored"
OUTCOME_UNCHANGED = "unchanged"
# Skipped because the venue ALREADY has a stored profile photo for its current
# handle. Not "fresh": there is no clock any more. A captured avatar is good
# indefinitely, so this venue is done — permanently, in backfill mode.
OUTCOME_SKIPPED_HAS_PHOTO = "skipped_has_photo"
# Skipped because the venue's LAST attempt produced no photo and is still
# inside the retry window. Distinct from skipped_has_photo on purpose: one
# venue is skipped because it already has what we wanted, the other because we
# already paid to find out it has nothing, and only the second one is
# coverage that is missing rather than coverage that is done. With the refresh
# window gone this is the ONLY recurring spend the job makes, so its size is
# the number to watch.
OUTCOME_SKIPPED_RECENT_FAILURE = "skipped_recent_failure"
OUTCOME_NO_HANDLE = "no_handle"
OUTCOME_NO_PIC = "no_pic"
OUTCOME_FETCH_FAILED = "fetch_failed"
OUTCOME_DOWNLOAD_FAILED = "download_failed"
OUTCOME_UPLOAD_FAILED = "upload_failed"
OUTCOME_CREDIT_EXHAUSTED = "credit_exhausted"

# ── add-time capture outcome (`capture_for_venue` only) ─────────────────────
# The job has no per-venue equivalent: when the feature is off or the media
# store is unconfigured, `select()` returns a run-level `status` and scrapes
# NOTHING, so there is no venue to label. The add-time path is called for one
# venue at a time and still has to say what happened to it, hence a label the
# job can never emit.
OUTCOME_SKIPPED_UNAVAILABLE = "skipped_unavailable"

# ── edge-colour backfill outcomes (mode `edge_color` only) ──────────────────
# A closed set, like the ones above: an outcome label that never appears in
# Prometheus is itself the diagnostic that its code path never ran. None of
# these can move `venue_profile_photo_apify_calls_total`, which is the cost
# guarantee stated as a metric rather than as a comment.
OUTCOME_EDGE_COLOR_SAMPLED = "edge_color_sampled"
OUTCOME_EDGE_COLOR_NO_URL = "edge_color_no_url"
OUTCOME_EDGE_COLOR_FETCH_FAILED = "edge_color_fetch_failed"
OUTCOME_EDGE_COLOR_DECODE_FAILED = "edge_color_decode_failed"

# The outcomes that produced NO photo and were paid for anyway. Each writes an
# attempt row so the next run can skip the venue before spending again.
# credit_exhausted is NOT here: the 402 never ran the actor, so nothing was
# learned about that venue and nothing was billed for it.
ATTEMPT_RECORDED_OUTCOMES = frozenset({
    OUTCOME_NO_PIC,
    OUTCOME_FETCH_FAILED,
    OUTCOME_DOWNLOAD_FAILED,
    OUTCOME_UPLOAD_FAILED,
})

# What we are willing to store and serve. A datacenter IP asking Instagram for
# an image very often gets an HTML login wall with HTTP 200, so a content-type
# allowlist is a correctness check, not paranoia: without it the "photo" on a
# venue card would be a few KB of markup.
#
# PNG and WebP are admitted even though the key's extension is fixed at `.jpg`
# by the cross-repo contract. The object's Content-Type is what a browser and
# React Native actually honour, so such a photo renders correctly; rejecting it
# would throw away a scrape that was already paid for over a cosmetic mismatch.
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp"}
)


@dataclass
class ProfilePhotoSelection:
    """What `select()` decided, and the ONLY description of who costs money.

    Returned to both `run()` and `estimate()` so neither can hold its own idea
    of the venue set. `selected` is the exact list the run hands to Apify, so
    `len(selected)` is simultaneously the estimate's headline count and the
    run's billed-call count — the two cannot drift without this list changing
    for both of them at once.

    `skipped` is returned rather than recorded here on purpose: `select()` must
    stay free of side effects so an estimate does not move a Prometheus
    counter. `run()` is what turns those entries into metrics.
    """

    mode: str
    # "ok", or the terminal run status that stopped selection before it began
    # ("disabled", "not_configured", "error").
    status: str = "ok"
    venues_servable: int = 0
    venues_with_handle: int = 0
    # Survivors of every gate, BEFORE the per-run cap.
    due: list[tuple[str, str]] = field(default_factory=list)
    # Survivors after the cap: precisely what will be scraped.
    selected: list[tuple[str, str]] = field(default_factory=list)
    # venue_id -> outcome, for venues the gates removed (no metric touched).
    skipped: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    # Bulk-read rows the run still needs: the existing photo row feeds the
    # unchanged-hash short-circuit, and the attempt ids tell `_note_attempt`
    # whether there is a negative-cache row to clear on success.
    existing: dict[str, dict] = field(default_factory=dict)
    attempt_ids: frozenset = field(default_factory=frozenset)

    @property
    def deferred(self) -> int:
        """Due venues the per-run cap left for a later run."""
        return max(len(self.due) - len(self.selected), 0)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_dt(value) -> Optional[datetime]:
    """Coerce an `updated_at` to an aware datetime. Real Postgres yields a
    tz-aware datetime; the in-memory fake and JSON yield an ISO string. A naive
    timestamp is read as UTC."""
    if value is None:
        return None
    ts = value
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _normalize_handle(value) -> str:
    """Compare handles the way Instagram itself treats them: case-insensitive,
    no leading `@`, no surrounding whitespace.

    Comparing raw strings would let a purely cosmetic re-casing by the
    discovery cascade read as "different business" and re-buy a scrape for
    every venue in the catalog at once.
    """
    if not value:
        return ""
    return str(value).strip().lstrip("@").strip().lower()


async def download_image(url: str, *, max_bytes: Optional[int] = None) -> tuple[bytes, str]:
    """Download an image, refusing to buffer more than `max_bytes` (+1).

    Streamed, and stopped one byte past the cap rather than read to completion:
    the cap exists to bound memory as well as to reject oversized files, and a
    caller that reads the whole body before checking its length has already
    lost the first half of that. The extra byte is what lets the SERVICE make
    the reject/accept decision — it can see the body exceeded the limit without
    this function deciding policy on its own.
    """
    limit = max_bytes or settings.instagram_profile_photo_max_bytes
    timeout = settings.instagram_profile_photo_download_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    break
            return b"".join(chunks), content_type


class VenueProfilePhotoService:
    def __init__(
        self,
        repo,
        apify_client,
        media_store,
        image_fetcher=None,
        now_fn=None,
        run_record_store=None,
    ):
        self.repo = repo  # VenueRepository: RDS system of record + serving DAO
        self.rds_store = getattr(repo, "rds_store", None)
        self.apify_client = apify_client
        self.media_store = media_store
        self._fetch_image = image_fetcher or download_image
        self._now = now_fn or _now
        # Same pattern as VenuePhotoArchiveService/DeepReviewCrawlService: an
        # in-memory dict by default (one process, no worker fan-out),
        # injectable so a future shared store can back all three.
        self._records = run_record_store if run_record_store is not None else {}

    # ── run records (GET /admin/jobs/runs/{job_id}) ──────────────────────────
    def get_run_record(self, job_id: str) -> Optional[dict]:
        try:
            return self._records.get(job_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[ProfilePhoto] run record read failed: {e}")
            return None

    def _save_run_record(self, job_id: str, record: dict) -> None:
        # A lost record must never fail a run that already spent money.
        try:
            self._records[job_id] = record
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[ProfilePhoto] run record write failed: {e}")

    # ── selection: the one gate, shared by the run and the estimate ─────────
    def select(self, mode: str = MODE_BACKFILL) -> ProfilePhotoSelection:
        """Decide exactly which venues this run would pay to scrape.

        Free by construction: bulk RDS reads only, no provider client is even
        referenced, and nothing is recorded or persisted. That is what lets
        `estimate()` call it and what makes "the estimate spends nothing" a
        property of the code rather than a promise.

        `run()` calls the same function, so the count an operator approved in
        the estimate is the count of Apify units the run buys. Two copies of
        this gate would drift the first time either was edited; there is one.
        """
        selection = ProfilePhotoSelection(mode=mode)

        # ── gate 1: configuration, BEFORE anything can be billed ────────────
        if not settings.instagram_profile_photo_enabled:
            # Inert, not broken: no scrape, no upload, no row. The flag ships
            # false and prod turns it on only after infra/media/ is applied and
            # verified, because a bucket-policy gap fails the write AFTER the
            # scrape has been paid for.
            selection.status = "disabled"
            return selection

        if self.media_store is None or not settings.media_cdn_base_url:
            logger.warning(
                "[ProfilePhoto] not configured (media bucket / CDN base URL "
                "missing); no venue was scraped"
            )
            selection.status = "not_configured"
            return selection

        if self.rds_store is None:  # pragma: no cover - defensive
            logger.error("[ProfilePhoto] no RDS store on the repository")
            selection.status = "error"
            return selection

        # ── the reads: free ────────────────────────────────────────────────
        try:
            servable_ids = list(self.rds_store.list_servable_venue_ids())
            handles = self.rds_store.list_instagram_handles()
            existing = self.rds_store.get_enrichment_bulk(
                PROFILE_PHOTO_TABLE, servable_ids
            )
            # The negative cache. One more bulk read, still zero provider
            # calls — and it is what keeps a permanently photo-less venue from
            # being re-billed on every run.
            attempts = self.rds_store.get_enrichment_bulk(
                PROFILE_PHOTO_ATTEMPT_TABLE, servable_ids
            )
        except Exception as e:
            logger.error(f"[ProfilePhoto] selection failed; nothing spent: {e}")
            selection.status = "error"
            return selection

        selection.venues_servable = len(servable_ids)
        selection.existing = existing
        selection.attempt_ids = frozenset(attempts.keys())
        retry_days = settings.instagram_profile_photo_retry_days
        # retry_days <= 0 disables the negative cache outright: the escape
        # hatch for retrying the whole catalog right after fixing an
        # infrastructure-wide failure, instead of waiting the window out. It
        # is also the deliberate way to make refresh_all re-try known failures,
        # which refresh_all alone does not do (see the module docstring).
        retry_cutoff = (
            self._now() - timedelta(days=retry_days) if retry_days > 0 else None
        )

        def skip(venue_id: str, outcome: str) -> None:
            selection.skipped[venue_id] = outcome
            selection.counts[outcome] = selection.counts.get(outcome, 0) + 1

        if mode == MODE_BACKFILL_EDGE_COLOR:
            # A separate gate, not an extra condition on the one below, because
            # it selects on a different question entirely: not "does this venue
            # need a photo bought" but "does the photo we ALREADY own carry a
            # colour". A handle is irrelevant (the row holds its own URL) and
            # the negative cache is irrelevant too (nothing is bought, so there
            # is no spend to suppress; a failed fetch is simply retried next
            # run, which costs nothing).
            for venue_id in servable_ids:
                row = existing.get(venue_id)
                if row is None:
                    continue
                if (row.get("payload") or {}).get("edge_color"):
                    continue
                selection.due.append((venue_id, handles.get(venue_id) or ""))
            cap = settings.instagram_profile_photo_max_venues_per_run
            selection.selected = (
                selection.due[:cap] if cap and cap > 0 else list(selection.due)
            )
            return selection

        for venue_id in servable_ids:
            handle = handles.get(venue_id)
            if not handle:
                # Never fetched, never billed — the venue simply has nothing to
                # scrape. Handle discovery is a different pipeline's job.
                skip(venue_id, OUTCOME_NO_HANDLE)
                continue
            selection.venues_with_handle += 1
            row = existing.get(venue_id)
            # BACKFILL: a venue that already has a photo for its current handle
            # is done, whatever the row's age. REFRESH_ALL: the operator asked
            # for a new photo regardless, so this gate is skipped entirely.
            if (
                mode != MODE_REFRESH_ALL
                and row is not None
                and self._has_current_photo(row, handle)
            ):
                skip(venue_id, OUTCOME_SKIPPED_HAS_PHOTO)
                continue
            attempt = attempts.get(venue_id)
            if (
                retry_cutoff is not None
                and attempt is not None
                and self._attempt_suppresses(attempt, retry_cutoff, handle)
            ):
                skip(venue_id, OUTCOME_SKIPPED_RECENT_FAILURE)
                continue
            selection.due.append((venue_id, handle))

        # The cap bounds the survivors, not the raw servable list: capping
        # earlier would let already-photographed venues eat the run budget.
        cap = settings.instagram_profile_photo_max_venues_per_run
        selection.selected = (
            selection.due[:cap] if cap and cap > 0 else list(selection.due)
        )
        return selection

    # ── the estimate: price a run without buying one ────────────────────────
    def estimate(self, config: Optional[dict] = None) -> dict:
        """What a run in this mode would scrape, and what it would cost.

        Synchronous and provider-free on purpose: there is no `await` in this
        method and no client is touched, so it cannot spend even by accident.
        It also lives behind its own admin route rather than a flag on the
        trigger, mirroring `POST /trigger/venue_photo_archive/estimate` — an
        estimate must never be one mistyped field away from starting a run.

        `venues_to_scrape` is `len(selection.selected)`, the same list `run()`
        iterates, which is why the two can never disagree.
        """
        mode = parse_mode(config)
        selection = self.select(mode)
        to_scrape = len(selection.selected)
        # The edge-colour backfill buys nothing: no actor run, no upload, only
        # an anonymous CDN GET of an object we already paid for. Pricing it at
        # the Apify unit rate would misreport a free job as a paid one and
        # invite an operator to defer it.
        unit = (
            0.0 if mode == MODE_BACKFILL_EDGE_COLOR
            else settings.apify_instagram_profile_cost_usd
        )
        cost = round(to_scrape * unit, 4)

        warning = None
        if selection.status == "disabled":
            warning = (
                "The job is disabled (INSTAGRAM_PROFILE_PHOTO_ENABLED=false); a "
                "run right now would scrape nothing."
            )
        elif selection.status == "not_configured":
            warning = (
                "The media bucket / CDN base URL is not configured; a run right "
                "now would scrape nothing."
            )
        elif selection.status != "ok":
            warning = (
                "Venue selection failed, so this estimate is not a real figure; "
                "check the logs before triggering a run."
            )
        elif mode == MODE_BACKFILL_EDGE_COLOR:
            warning = (
                f"edge_color re-reads {to_scrape} already-stored photo(s) over "
                "the public CDN to record their edge colour. It makes no Apify "
                "call and uploads nothing, so it costs $0.00 and is safe to "
                "re-run."
            )
        elif mode == MODE_REFRESH_ALL:
            # The "warning sign with cost estimative" the operator asked for.
            # Backfill deliberately carries none: it is the free, routine mode,
            # and a warning shown on every run is a warning nobody reads.
            warning = (
                f"refresh_all re-scrapes {to_scrape} venue(s) that already have "
                f"a stored profile photo — about ${cost:.2f} of Apify spend that "
                "backfill would not make. A captured profile photo stays valid "
                "indefinitely, so run this only when the stored photos are known "
                "to be wrong or outdated."
            )
            if selection.deferred:
                warning += (
                    f" The per-run cap of "
                    f"{settings.instagram_profile_photo_max_venues_per_run} leaves "
                    f"{selection.deferred} more for later runs, at the same unit "
                    "cost each."
                )

        estimate = {
            "mode": mode,
            "status": selection.status,
            "venues_servable": selection.venues_servable,
            "venues_with_handle": selection.venues_with_handle,
            "venues_due": len(selection.due),
            # THE number: what this run would actually scrape, and what the
            # run's billed-call count is asserted equal to.
            "venues_to_scrape": to_scrape,
            # Same number under a mode-neutral name. `venues_to_scrape` would
            # be a lie for edge_color, which scrapes nothing, but the admin
            # panel's generic renderer already reads it — so both are emitted
            # from the one selection rather than either being dropped.
            "venues_to_process": to_scrape,
            "venues_deferred": selection.deferred,
            "max_venues_per_run": settings.instagram_profile_photo_max_venues_per_run,
            "unit_cost_usd": unit,
            "est_cost_usd": cost,
            "est_cost_usd_all_due": round(len(selection.due) * unit, 4),
            "skipped": dict(selection.counts),
            "warning": warning,
            # `venues_selected` / `venues_after_skip` / `caveat` mirror the
            # field names POST /trigger/venue_photo_archive/estimate already
            # returns, because the admin panel's generic estimate renderer
            # reads exactly those. Aliases, not a second source of truth: both
            # are derived from the same selection.
            "venues_selected": len(selection.due),
            "venues_after_skip": to_scrape,
            "caveat": warning or (
                "Backfill only: venues that already have a profile photo are "
                "not re-scraped at any age, so this cost does not recur."
            ),
        }
        VENUE_PROFILE_PHOTO_ESTIMATE_COST_USD.labels(mode=mode).set(cost)
        logger.info(
            f"[ProfilePhoto] estimate mode={mode} status={selection.status} "
            f"due={len(selection.due)} to_scrape={to_scrape} cost=${cost}"
        )
        return estimate

    # ── the run ─────────────────────────────────────────────────────────────
    async def run(self, config: Optional[dict] = None) -> dict:
        """Backfill by default; `{"mode": "refresh_all"}` only from an operator.

        `config` carries exactly ONE knob — the mode. Every spend bound (the
        enable flag, the retry window, the venue cap, the byte cap) stays a
        setting, so a trigger dialog can choose what to scrape but can never
        widen how much.
        """
        started = time.perf_counter()
        job_id = new_run_id()
        summary: dict = {
            "job_id": job_id,
            "mode": MODE_BACKFILL,
            "status": "success",
            "stopped_reason": None,
            "venues_servable": 0,
            "venues_selected": 0,
            "apify_calls": 0,
            "bytes_stored": 0,
            "estimated_cost_usd": 0.0,
            "counts": {},
            "outcomes": {},
        }

        def record(venue_id: str, outcome: str) -> None:
            summary["outcomes"][venue_id] = outcome
            summary["counts"][outcome] = summary["counts"].get(outcome, 0) + 1
            VENUE_PROFILE_PHOTO_VENUES_TOTAL.labels(outcome=outcome).inc()

        try:
            mode = parse_mode(config)
        except InvalidProfilePhotoMode as e:
            # Rejected before selection, so nothing was read and nothing spent.
            logger.error(f"[ProfilePhoto] job={job_id} not started: {e}")
            summary["stopped_reason"] = str(e)
            return self._finish(summary, "invalid_mode", started, job_id)
        summary["mode"] = mode

        selection = self.select(mode)
        if selection.status != "ok":
            return self._finish(summary, selection.status, started, job_id)

        summary["venues_servable"] = selection.venues_servable
        # The gates' verdicts become metrics HERE and only here: `select()`
        # stays side-effect free so the estimate cannot move a counter.
        for venue_id, outcome in selection.skipped.items():
            record(venue_id, outcome)

        existing = selection.existing
        selected = selection.selected
        summary["venues_selected"] = len(selected)
        logger.info(
            f"[ProfilePhoto] job={job_id} mode={mode} "
            f"servable={selection.venues_servable} "
            f"due={len(selection.due)} selected={len(selected)} "
            "skipped_has_photo="
            f"{summary['counts'].get(OUTCOME_SKIPPED_HAS_PHOTO, 0)} "
            "skipped_recent_failure="
            f"{summary['counts'].get(OUTCOME_SKIPPED_RECENT_FAILURE, 0)} "
            f"no_handle={summary['counts'].get(OUTCOME_NO_HANDLE, 0)}"
        )

        # ── the FREE mode: no scrape, no upload, no attempt bookkeeping ────
        if mode == MODE_BACKFILL_EDGE_COLOR:
            summary["deferred"] = selection.deferred
            for venue_id, _handle in selected:
                try:
                    outcome = await self._process_edge_color(
                        venue_id, existing.get(venue_id)
                    )
                except Exception as e:
                    # Failure isolation, same rule as the paid run: one bad row
                    # must never abort a pass over the rest.
                    logger.warning(
                        f"[ProfilePhoto] edge colour for {venue_id} failed "
                        f"unexpectedly: {e}"
                    )
                    outcome = OUTCOME_EDGE_COLOR_FETCH_FAILED
                record(venue_id, outcome)
            # `_note_attempt` is deliberately NOT called: the negative cache
            # exists to stop re-BUYING a scrape, and nothing here is bought.
            # Writing an attempt row would also suppress a later real scrape
            # for a venue whose only problem was an unreachable CDN.
            failed = sum(
                summary["counts"].get(o, 0)
                for o in (
                    OUTCOME_EDGE_COLOR_FETCH_FAILED,
                    OUTCOME_EDGE_COLOR_DECODE_FAILED,
                    OUTCOME_EDGE_COLOR_NO_URL,
                )
            )
            return self._finish(
                summary, "partial" if failed else "success", started, job_id
            )

        # ── spend ──────────────────────────────────────────────────────────
        for venue_id, handle in selected:
            try:
                outcome = await self._process_venue(
                    venue_id, handle, existing.get(venue_id), summary
                )
            except ApifyCreditExhaustedError:
                # Stop the run. Every further venue would buy another 402.
                record(venue_id, OUTCOME_CREDIT_EXHAUSTED)
                summary["stopped_reason"] = OUTCOME_CREDIT_EXHAUSTED
                logger.error(
                    f"[ProfilePhoto] job={job_id} stopped at {venue_id}: "
                    "Apify credit exhausted"
                )
                break
            except Exception as e:
                # The unforeseen-error net. Every realistic failure is already
                # bucketed inside _process_venue; anything reaching here is a
                # bug, so the LOG (with the venue named) is the diagnostic and
                # the label only keeps the run summary total-preserving.
                logger.warning(
                    f"[ProfilePhoto] venue {venue_id} (@{handle}) failed "
                    f"unexpectedly: {e}"
                )
                outcome = OUTCOME_FETCH_FAILED
            record(venue_id, outcome)
            self._note_attempt(
                venue_id, handle, outcome,
                had_attempt=venue_id in selection.attempt_ids,
            )

        failed = sum(
            summary["counts"].get(o, 0)
            for o in (
                OUTCOME_FETCH_FAILED, OUTCOME_DOWNLOAD_FAILED, OUTCOME_UPLOAD_FAILED,
            )
        )
        if summary["stopped_reason"] == OUTCOME_CREDIT_EXHAUSTED:
            status = OUTCOME_CREDIT_EXHAUSTED
        elif failed:
            status = "partial"
        else:
            status = "success"
        return self._finish(summary, status, started, job_id)

    # ── one venue ───────────────────────────────────────────────────────────
    # ── add-time capture: one venue, now ────────────────────────────────────
    async def capture_for_venue(
        self, venue_id: str, handle: Optional[str]
    ) -> dict:
        """Capture ONE venue's profile photo immediately, at add time.

        The scheduled backfill runs on a 24h interval behind a 200-venue cap,
        so a venue added today could wait a day or more for its picture and
        show an emoji placeholder in the list until then. This is the same
        capture, run inline for the venue that was just added.

        Every gate `select()` applies per venue is applied here too, and for
        the same reasons — a venue that already holds a photo for this handle
        is not re-bought, and one that failed inside the retry window is not
        re-billed. What is NOT shared is the bulk read: `select()` reads the
        whole catalog to decide a run, this reads one row.

        The per-run cap deliberately does not apply. It bounds a sweep of the
        catalog; this is a single venue the operator just paid to add, and
        capping it at anything would only ever mean "sometimes silently skip".

        Returns a one-venue summary. Raises nothing the caller must handle:
        the outcome string carries the failure, exactly as the job's does.
        """
        summary: dict = {
            "venue_id": venue_id,
            "outcome": None,
            "apify_calls": 0,
            "bytes_stored": 0,
            "estimated_cost_usd": 0.0,
        }

        def done(outcome: str) -> dict:
            summary["outcome"] = outcome
            VENUE_PROFILE_PHOTO_VENUES_TOTAL.labels(outcome=outcome).inc()
            return summary

        # ── the config gates, BEFORE anything can be billed ─────────────────
        if not settings.instagram_profile_photo_enabled:
            return done(OUTCOME_SKIPPED_UNAVAILABLE)
        if self.media_store is None or not settings.media_cdn_base_url:
            return done(OUTCOME_SKIPPED_UNAVAILABLE)
        if self.rds_store is None:  # pragma: no cover - defensive
            return done(OUTCOME_SKIPPED_UNAVAILABLE)

        if not _normalize_handle(handle):
            # The caller had no handle to offer. That is the NORMAL shape for a
            # recovered or geo-linked add, where discovery reports "skipped"
            # precisely because the venue already carries one — so look it up
            # rather than treating the caller's None as "no handle exists".
            # `Venue` has no handle field (it lives on `MinifiedVenue`), which
            # is why the lookup belongs here and not in the handler.
            try:
                handle = self.rds_store.list_instagram_handles().get(venue_id)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    f"[ProfilePhoto] add-time handle lookup failed for "
                    f"{venue_id}; nothing spent: {e}"
                )
                return done(OUTCOME_SKIPPED_UNAVAILABLE)
        if not _normalize_handle(handle):
            # Genuinely nothing to scrape. Handle discovery is a different
            # pipeline's job.
            return done(OUTCOME_NO_HANDLE)

        # ── the per-venue gates, one row each, still free ───────────────────
        try:
            existing = self.rds_store.get_enrichment_bulk(
                PROFILE_PHOTO_TABLE, [venue_id]
            )
            attempts = self.rds_store.get_enrichment_bulk(
                PROFILE_PHOTO_ATTEMPT_TABLE, [venue_id]
            )
        except Exception as e:
            # Read failed, so the gates cannot be evaluated. Skipping is the
            # only safe answer: scraping anyway could re-buy a photo we already
            # own, and the scheduled backfill will pick this venue up.
            logger.warning(
                f"[ProfilePhoto] add-time gate read failed for {venue_id}; "
                f"nothing spent: {e}"
            )
            return done(OUTCOME_SKIPPED_UNAVAILABLE)

        row = existing.get(venue_id)
        if row is not None and self._has_current_photo(row, handle):
            return done(OUTCOME_SKIPPED_HAS_PHOTO)

        retry_days = settings.instagram_profile_photo_retry_days
        attempt = attempts.get(venue_id)
        if retry_days > 0 and attempt is not None:
            cutoff = self._now() - timedelta(days=retry_days)
            if self._attempt_suppresses(attempt, cutoff, handle):
                return done(OUTCOME_SKIPPED_RECENT_FAILURE)

        outcome = await self._process_venue(venue_id, handle, row, summary)
        return done(outcome)

    async def _process_venue(
        self, venue_id: str, handle: str, existing_row: Optional[dict], summary: dict
    ) -> str:
        result = await self.apify_client.fetch_profile(handle)
        # Counted only on a RETURN: a 402 never ran the actor, so it is not a
        # billed call and must not appear in the cost figure. A returned
        # dataset — even an empty or error one — did run.
        summary["apify_calls"] += 1
        VENUE_PROFILE_PHOTO_APIFY_CALLS_TOTAL.inc()
        cost = settings.apify_instagram_profile_cost_usd
        summary["estimated_cost_usd"] = round(summary["estimated_cost_usd"] + cost, 6)
        VENUE_PROFILE_PHOTO_ESTIMATED_COST_USD.inc(cost)

        if getattr(result, "error_code", None):
            logger.warning(
                f"[ProfilePhoto] {venue_id} (@{handle}) scrape failed: "
                f"{result.error_code}"
            )
            return OUTCOME_FETCH_FAILED

        pic_url = getattr(result, "profile_pic_url", None)
        if not pic_url:
            # An absence, not an error: this profile has no picture to store.
            logger.info(f"[ProfilePhoto] {venue_id} (@{handle}) has no profile picture")
            return OUTCOME_NO_PIC

        max_bytes = settings.instagram_profile_photo_max_bytes
        try:
            data, content_type = await self._fetch_image(pic_url, max_bytes=max_bytes)
        except Exception as e:
            logger.warning(f"[ProfilePhoto] {venue_id} download failed: {e}")
            return OUTCOME_DOWNLOAD_FAILED

        normalized_type = (content_type or "").split(";")[0].strip().lower()
        if normalized_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            logger.warning(
                f"[ProfilePhoto] {venue_id} discarded: content-type "
                f"{normalized_type!r} is not an allowed image type"
            )
            return OUTCOME_DOWNLOAD_FAILED
        if not data or len(data) > max_bytes:
            logger.warning(
                f"[ProfilePhoto] {venue_id} discarded: {len(data)} bytes "
                f"exceeds the {max_bytes}-byte cap"
            )
            return OUTCOME_DOWNLOAD_FAILED

        content_hash = hashlib.sha256(data).hexdigest()
        key = self.media_store.profile_photo_key(venue_id, content_hash)
        photo_url = self.media_store.cdn_url(key)
        # Sampled from the bytes already in memory: no extra network call, and
        # it happens AFTER the download so it cannot move the Apify counter or
        # change which venues `select()` chose. `None` is a normal result — the
        # photo is stored either way (see the module's "what is never a
        # failure").
        edge_color = sample_edge_color(data)

        unchanged = (
            existing_row is not None
            and (existing_row.get("payload") or {}).get("content_hash") == content_hash
        )
        if unchanged:
            # Identical bytes -> identical key -> identical URL. Re-uploading
            # would replace an object with a byte-identical copy and buy
            # nothing; the row IS re-asserted, and that is deliberate — it
            # restarts the freshness clock so an unchanged avatar is not
            # re-scraped on every single cycle.
            # The re-persist also fills a MISSING colour for free: the bytes
            # are already here, so a row stored before this field existed
            # gains one the next time its venue is legitimately re-scraped.
            self._persist(venue_id, handle, photo_url, key, content_hash,
                          normalized_type, len(data), edge_color)
            return OUTCOME_UNCHANGED

        try:
            key, photo_url = await self.media_store.put_profile_photo(
                venue_id=venue_id,
                content_hash=content_hash,
                data=data,
                content_type=normalized_type,
            )
        except Exception as e:
            logger.error(f"[ProfilePhoto] {venue_id} upload failed: {e}")
            return OUTCOME_UPLOAD_FAILED

        summary["bytes_stored"] += len(data)
        VENUE_PROFILE_PHOTO_BYTES_STORED_TOTAL.inc(len(data))
        # Written only AFTER the upload returned: a row is a promise that the
        # object exists, and the projector turns that row into a URL the app
        # will render.
        self._persist(venue_id, handle, photo_url, key, content_hash,
                      normalized_type, len(data), edge_color)
        return OUTCOME_STORED

    # ── one venue, edge-colour backfill (free) ──────────────────────────────
    async def _process_edge_color(self, venue_id: str, row: Optional[dict]) -> str:
        """Fill `edge_color` on a row that already has a photo.

        Reads the object back over the row's own **public CloudFront URL** —
        the one field of the row that is already a durable, anonymously
        readable address. cs-server holds `s3:PutObject` on this bucket and
        nothing else, so this is not a shortcut around an S3 read, it is the
        only route there is.

        Every other field of the row is carried over verbatim.
        `content_hash` and `instagram_handle` are the load-bearing pair:
        `_has_current_photo` reads both, so rewriting either would make the
        next scheduled backfill re-buy an Apify scrape for every row this job
        touched. `fetched_at` is preserved too — this job did not fetch a
        photo, it read one we already had.

        A failure leaves the row COMPLETELY untouched rather than stamping a
        null colour over it, so the next run simply picks it up again. That is
        affordable precisely because the run is free.
        """
        payload = (row or {}).get("payload") or {}
        url = payload.get("photo_url")
        if not url:
            logger.warning(
                f"[ProfilePhoto] {venue_id} has a photo row with no URL; "
                "nothing to read an edge colour from"
            )
            return OUTCOME_EDGE_COLOR_NO_URL

        try:
            data, _content_type = await self._fetch_image(url)
        except Exception as e:
            logger.warning(
                f"[ProfilePhoto] {venue_id} edge colour: fetching {url} failed "
                f"({type(e).__name__}: {e}); the row is left untouched and will "
                "be retried"
            )
            return OUTCOME_EDGE_COLOR_FETCH_FAILED

        edge_color = sample_edge_color(data)
        if not edge_color:
            return OUTCOME_EDGE_COLOR_DECODE_FAILED

        self.repo.set_venue_profile_photo(
            VenueInstagramProfilePhoto(
                venue_id=venue_id,
                instagram_handle=payload.get("instagram_handle"),
                photo_url=url,
                s3_key=payload.get("s3_key") or "",
                content_hash=payload.get("content_hash") or "",
                content_type=payload.get("content_type") or "image/jpeg",
                byte_size=payload.get("byte_size") or 0,
                edge_color=edge_color,
                fetched_at=_coerce_dt(payload.get("fetched_at")) or self._now(),
            )
        )
        return OUTCOME_EDGE_COLOR_SAMPLED

    # ── helpers ─────────────────────────────────────────────────────────────
    def _has_current_photo(self, row: dict, handle: str) -> bool:
        """Whether this venue already holds a usable photo for its CURRENT
        handle — the backfill gate, and deliberately age-blind.

        There is no refresh window and no cutoff argument. A profile picture,
        once captured, is good indefinitely, so a row's age is not evidence of
        anything and re-scraping on a clock is recurring spend that buys
        almost nothing.

        The handle comparison, however, stays — and it is not a refresh in
        disguise. Handle discovery revises itself; a corrected handle is a
        normal event, and a stored row is a photo scraped from whatever handle
        was believed correct at the time. Keeping such a row means ANOTHER
        BUSINESS'S LOGO sits on this venue's card, indefinitely now that
        nothing else would ever dislodge it. That is a wrong answer served to
        a user, not a stale one, so a handle that no longer matches makes the
        row worthless regardless of its age.

        A stored row with NO handle recorded is treated as still good: it is
        unknown, not mismatched, and re-scraping every such row would re-buy
        the catalog to resolve an ambiguity that only pre-service-written rows
        can even have (`_persist` always records the handle).

        The `deleted_at` check is belt-and-braces: `get_enrichment_bulk`
        already excludes soft-deleted rows on both the real store and the
        fake. It is kept because reading it the other way round — treating a
        withdrawn hero as still present — would freeze that venue out of the
        pipeline permanently, and that is too quiet a failure to leave to one
        layer.
        """
        if row.get("deleted_at") is not None:
            return False
        stored_handle = _normalize_handle(
            (row.get("payload") or {}).get("instagram_handle")
        )
        if stored_handle and stored_handle != _normalize_handle(handle):
            return False
        return True

    def _attempt_suppresses(self, attempt: dict, cutoff: datetime, handle: str) -> bool:
        """Whether a recorded failed attempt still blocks a billed retry.

        Symmetric with `_has_current_photo`, and for the same reasons: a
        soft-deleted or unreadable attempt suppresses nothing (erring towards
        one Apify unit beats freezing a venue out of the pipeline), and an
        attempt made against a DIFFERENT handle suppresses nothing either —
        whatever we learned about the old handle says nothing about the
        corrected one.

        Unlike `_has_current_photo` this one IS time-bounded, and that
        asymmetry is the point: a stored photo stays true indefinitely, while a
        failure only tells you what was true when it happened.
        """
        if attempt.get("deleted_at") is not None:
            return False
        attempted_handle = _normalize_handle(
            (attempt.get("payload") or {}).get("instagram_handle")
        )
        if attempted_handle and attempted_handle != _normalize_handle(handle):
            return False
        updated_at = _coerce_dt(attempt.get("updated_at"))
        if updated_at is None:
            return False
        return updated_at > cutoff

    def _note_attempt(
        self, venue_id: str, handle: str, outcome: str, *, had_attempt: bool
    ) -> None:
        """Maintain the negative cache after a venue was actually processed.

        An outcome that produced no photo writes (or refreshes) the attempt
        row, which is what makes the next run skip this venue BEFORE spending.
        A success clears any row that was there, so an old failure can never
        shadow a venue that now works.

        Wrapped: this is bookkeeping for a run that has already spent money,
        and losing it must degrade to "we re-scrape this venue next window",
        never to a failed run. The log line is the diagnostic — silently
        losing the negative cache is exactly how the spend bug comes back.
        """
        try:
            if outcome in ATTEMPT_RECORDED_OUTCOMES:
                self.repo.set_venue_profile_photo_attempt(
                    VenueInstagramProfilePhotoAttempt(
                        venue_id=venue_id,
                        instagram_handle=handle,
                        outcome=outcome,
                        attempted_at=self._now(),
                    )
                )
            elif had_attempt:
                self.repo.delete_venue_profile_photo_attempt(venue_id)
        except Exception as e:
            logger.warning(
                f"[ProfilePhoto] {venue_id} (@{handle}) attempt bookkeeping "
                f"failed ({outcome}); it may be re-scraped next run: {e}"
            )

    def _persist(
        self, venue_id: str, handle: str, photo_url: str, key: str,
        content_hash: str, content_type: str, byte_size: int,
        edge_color: Optional[str] = None,
    ) -> None:
        self.repo.set_venue_profile_photo(
            VenueInstagramProfilePhoto(
                venue_id=venue_id,
                instagram_handle=handle,
                photo_url=photo_url,
                s3_key=key,
                content_hash=content_hash,
                content_type=content_type,
                byte_size=byte_size,
                edge_color=edge_color,
                fetched_at=self._now(),
            )
        )

    def _finish(self, summary: dict, status: str, started: float, job_id: str) -> dict:
        duration = time.perf_counter() - started
        summary["status"] = status
        summary["duration_seconds"] = round(duration, 3)
        VENUE_PROFILE_PHOTO_RUN_DURATION_SECONDS.observe(duration)
        VENUE_PROFILE_PHOTO_RUNS_TOTAL.labels(result=status).inc()
        self._save_run_record(job_id, summary)
        logger.info(f"[ProfilePhoto] job={job_id} {status}: {summary['counts']}")
        return summary
