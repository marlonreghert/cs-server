"""Scheduled, incremental Instagram crawling — one target per Instagram
handle, resuming from a cursor instead of re-scraping the whole feed every
time. See plans/260809_scheduled-incremental-instagram-crawl.md.

Turns event crawling from an operator-triggered, full-re-scrape action
(`PromoterCrawlService.run` / `VenuePhotoArchiveService.run` with the
`instagram_posts` source, both still exactly as they were) into a scheduled
one: each `events.crawl_target` (§A) carries its own cron, its own cursor
per stream (§B posts, §E reels), and its own reels flag, so an unchanged
target costs nothing and a busy one is still bounded by its own result cap
and the shared monthly budget (§F).

## Why this does not call `PromoterCrawlService.run()` / `VenuePhotoArchive-
Service.run()` for the actual fetch

Both of those re-derive their OWN eligibility (an active `events.
promoter_account` row; the archive's own venue selection + skip-before-spend
logic) and would either re-fetch from Apify a second time (double-billing
the exact posts this module already paid for) or silently skip a
`crawl_target` whose promoter isn't ALSO marked `active` in the separate
promoter registry — which the plan is explicit is a DIFFERENT concern
(`events.promoter_account` answers "is this account worth crawling";
`events.crawl_target` answers "when and from where do we crawl it"). So this
module owns the fetch (bound + cursor + budget + pinned-post handling)
itself, then reuses the SAME low-level archiving/extraction primitives those
services already use for the posts it already has in hand — never re-fetching:

  - venue kind: downloads and CLASSIFIES photos through the same
    `PhotoClassificationService.annotate` call `VenuePhotoArchiveService.
    _classify_photos` makes (so an image-only flyer — no caption event-
    marker at all — still gets a `category`/`classification_confidence`
    `post_qualifies` can act on, exactly like a manually-archived venue's
    posts already do), writes photos + a per-venue manifest through the
    same `MediaArchiveStore.put_image`/`put_manifest` calls `archive_
    sources._fetch_instagram` uses, then calls the REAL, unmodified
    `EventExtractionService.run()` scoped to just this handle's venue(s)
    via its existing `eligibility.mode="venue_ids"` — the exact integration
    point `admin_events_router`'s manual-trigger config already exposes;
    nothing new is invented on the extraction side. Classification is
    on by default (`crawl_target.classify_images`) — a scheduled crawl
    REPLACES a manual archive run that already classifies, so defaulting
    to the weaker caption-only gate would be a coverage regression, not
    parity with the promoter path (which never classifies at all, by a
    SEPARATE and deliberate design choice covered in §Non-goals).
  - promoter kind: calls the injected `PromoterCrawlService` instance's own
    `_archive_post_images`/`_process_post` — the SAME per-post units its
    `run()` loop calls, just fed this module's already-fetched, already-
    cursor-bounded posts instead of letting `run()` fetch and gate them
    again. These are read-only over `self` (media_store/downloader/
    openai_client/venue_dao), so sharing the container's singleton instance
    across concurrently-running targets is safe.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.metrics import (
    CRAWL_BUDGET_REMAINING,
    CRAWL_CHAIN_CLASSIFICATION_TOTAL,
    CRAWL_CURSOR_AGE_SECONDS,
    CRAWL_RESULTS_TOTAL,
    CRAWL_RUNS_TOTAL,
    CRAWL_STREAM_OVERLAP_TOTAL,
    CRAWL_VENUE_ATTRIBUTION_TOTAL,
)
from app.services import job_lock
from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS, _parse_post_timestamp
from app.services.event_extraction_service import SOURCE_KIND_VENUE_POST
from app.services.event_reconciliation import update_events_gauge
from app.services.event_venue_resolution import (
    RESOLUTION_AUTO,
    build_handle_index,
    build_venue_catalog,
    candidate_venues_for_ids,
)
from app.services.instagram_handle_sources import (
    group_venue_ids_by_handle,  # noqa: F401 -- re-exported for existing importers
    normalize_handle,
)
from app.services.post_dedupe import dedupe_by_shortcode
from app.services.venue_photo_archive_service import new_run_id, run_prefix

logger = logging.getLogger(__name__)

KIND_VENUE = "venue"
KIND_PROMOTER = "promoter"
STREAM_POSTS = "posts"
STREAM_REELS = "reels"


def lock_name_for(handle: str) -> str:
    """Per-handle concurrency lock — the SAME namespace both `crawl_
    schedule_sync.py`'s per-target APScheduler job and
    `ScheduledInstagramCrawlService.start_run` (the admin run-now endpoint)
    acquire, so a scheduled fire and a manual trigger of the SAME handle can
    never run concurrently. §D requires "two crawls of the same handle can
    never overlap"; #164 gave the scheduler that guarantee but not run-now,
    which called `run_target` directly with no lock at all — a real defect
    this closes. Defined HERE (not in crawl_schedule_sync.py, which imports
    it from this module) so there is exactly one function computing this
    name, never two that could drift apart."""
    return f"crawl_target:{handle}"

OUTCOME_SUCCESS = "success"
OUTCOME_EMPTY = "empty"
OUTCOME_FAILED = "failed"
OUTCOME_CREDIT_EXHAUSTED = "credit_exhausted"
OUTCOME_SKIPPED_DISABLED = "skipped_disabled"
OUTCOME_SKIPPED_FAILURES = "skipped_failures"
OUTCOME_SKIPPED_BUDGET = "skipped_budget"
OUTCOME_NOT_FOUND = "not_found"
# The post-run bookkeeping write itself failed (see `run_target`) — results
# were already scraped and BILLED by the time this can happen, so it is
# reported distinctly from OUTCOME_FAILED (a failed *fetch*, which bills
# nothing): a target stuck here is spending money and landing nothing, which
# `consecutive_failures` alone would hide until an operator happened to
# notice `last_run_at` had stopped advancing.
OUTCOME_BOOKKEEPING_FAILED = "bookkeeping_failed"
# `start_run`-only reason: the admin run-now endpoint's fast refusal when
# the per-handle lock (`lock_name_for`) is already held — by a scheduled
# fire or by another run-now call for the same handle. Never an `OUTCOME_*`
# because `run_target`/`_run_stream` have no concept of "the lock" at all;
# they run WHILE the lock is held (by whichever caller acquired it), they
# never check it themselves.
REASON_ALREADY_RUNNING = "already_running"


class InvalidCrawlTargetConfig(ValueError):
    """A crawl target write failed validation (e.g. an unparseable crontab,
    or a day-of-week form this module refuses to guess at) — raised at
    WRITE time by `validate_crontab`, never discovered later at fire time."""


# `CronTrigger.from_crontab` looked like the right tool (it IS what
# `main.py` already registers every other cron job through) but it is a
# trap for this field specifically: its own source just splits the string
# and passes `values[4]` straight through as `day_of_week=`, with NO
# translation. APScheduler's `day_of_week` is 0=Monday..6=Sunday (it comes
# from `date.weekday()`); standard Unix crontab(5) — what every operator
# means when they write a cron string, and what this feature's own console
# plan promises to render back in words — is 0/7=Sunday..6=Saturday.
# Verified independently against a live APScheduler 3.10.4/America-Recife
# install: "0 22 * * 5" (intended Friday) actually fires Saturday.
#
# The fix is to never let a bare digit reach APScheduler's day_of_week field
# at all: translate every standard-cron day-of-week token to APScheduler's
# own UNAMBIGUOUS three-letter names (mon/tue/wed/thu/fri/sat/sun) before
# building the trigger, and build it from explicit fields
# (`build_cron_trigger`) rather than `from_crontab`. `validate_crontab`
# (write time, admin_crawl_router.py) and `CrawlScheduleSync` (fire time,
# crawl_schedule_sync.py) BOTH go through `build_cron_trigger` — the same
# function — so validation and execution can never disagree about what a
# `cron` string means.
_APSCHEDULER_DOW_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_APSCHEDULER_DOW_NAMES = frozenset(_APSCHEDULER_DOW_ORDER)
# Standard crontab(5): 0 AND 7 both mean Sunday.
_CRON_DOW_NAME_BY_NUMBER = {
    0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun",
}


def _translate_dow_atom(atom: str, cron: str) -> str:
    """One day-of-week token (a name or a standard-cron digit) to
    APScheduler's own three-letter name. Already-named input passes through
    unchanged (an operator who writes `fri` never hits the ambiguity this
    exists to fix)."""
    value = atom.strip().lower()
    if not value:
        raise InvalidCrawlTargetConfig(f"invalid crontab {cron!r}: empty day-of-week token")
    if value in _APSCHEDULER_DOW_NAMES:
        return value
    if not value.isdigit():
        raise InvalidCrawlTargetConfig(
            f"invalid crontab {cron!r}: unrecognized day-of-week value {atom!r}"
        )
    number = int(value)
    if number not in _CRON_DOW_NAME_BY_NUMBER:
        raise InvalidCrawlTargetConfig(
            f"invalid crontab {cron!r}: day-of-week value {atom!r} out of range 0-7"
        )
    return _CRON_DOW_NAME_BY_NUMBER[number]


def _translate_day_of_week_field(raw: str, cron: str) -> str:
    """The 5th crontab field, translated atom by atom. Handles `*`,
    comma-separated lists, and ascending ranges (`fri-sun`, or `5-0` written
    in standard-cron digits). REJECTS — never guesses at — a step expression
    (`*/2`) or a range that would wrap across the week boundary once
    translated (e.g. standard-cron `6-1`, Saturday-through-Monday): both are
    genuinely ambiguous to resolve silently, and this feature's own
    principle throughout is that an ambiguous bound is refused at write
    time, not guessed at fire time."""
    raw = raw.strip()
    if raw == "*":
        return raw
    translated_atoms = []
    for atom in raw.split(","):
        atom = atom.strip()
        if "/" in atom:
            raise InvalidCrawlTargetConfig(
                f"invalid crontab {cron!r}: day-of-week step expressions "
                f"({atom!r}) are not supported — list the days explicitly, "
                "e.g. '5,6,0' for Friday, Saturday, Sunday"
            )
        if "-" in atom:
            start_raw, _, end_raw = atom.partition("-")
            start = _translate_dow_atom(start_raw, cron)
            end = _translate_dow_atom(end_raw, cron)
            if _APSCHEDULER_DOW_ORDER.index(start) > _APSCHEDULER_DOW_ORDER.index(end):
                raise InvalidCrawlTargetConfig(
                    f"invalid crontab {cron!r}: day-of-week range {atom!r} wraps "
                    "across the week boundary — list the days explicitly instead"
                )
            translated_atoms.append(f"{start}-{end}")
        else:
            translated_atoms.append(_translate_dow_atom(atom, cron))
    return ",".join(translated_atoms)


def _translate_cron(cron: str) -> tuple[str, str, str, str, str]:
    """(minute, hour, day, month, day_of_week) with ONLY the day_of_week
    field translated — the other four fields carry no such ambiguity
    (APScheduler's minute/hour/day/month fields already match standard
    cron)."""
    fields = cron.split()
    if len(fields) != 5:
        raise InvalidCrawlTargetConfig(
            f"invalid crontab {cron!r}: expected 5 fields "
            f"(minute hour day month day_of_week), got {len(fields)}"
        )
    minute, hour, day, month, day_of_week = fields
    return minute, hour, day, month, _translate_day_of_week_field(day_of_week, cron)


def build_cron_trigger(cron: str, *, timezone: str = "UTC"):
    """The ONLY place a crawl target's `cron` string becomes an APScheduler
    trigger. `validate_crontab` (write time) and `CrawlScheduleSync` (fire
    time) both call this — never `CronTrigger.from_crontab` directly — so a
    string that validates is guaranteed to fire on the days it validated
    against."""
    from apscheduler.triggers.cron import CronTrigger

    minute, hour, day, month, day_of_week = _translate_cron(cron)
    try:
        return CronTrigger(
            minute=minute, hour=hour, day=day, month=month,
            day_of_week=day_of_week, timezone=timezone,
        )
    except InvalidCrawlTargetConfig:
        raise
    except Exception as e:
        raise InvalidCrawlTargetConfig(f"invalid crontab {cron!r}: {e}") from e


def validate_crontab(cron: str) -> None:
    """Reject a malformed or ambiguous crontab string immediately, at write
    time — the unit test plan's own requirement. The built trigger is
    discarded; this call is for its side effect of raising on a bad or
    ambiguous string. Timezone is irrelevant to whether a string PARSES, so
    a placeholder is used here — `CrawlScheduleSync` supplies the target's
    real timezone at fire time, through the same `build_cron_trigger`."""
    build_cron_trigger(cron, timezone="UTC")


def _as_utc_dt(value) -> Optional[datetime]:
    """A cursor value as an aware UTC datetime, however it comes back
    (a real `datetime` from SQLAlchemy/the in-memory fake, or an ISO string
    read back through a JSON boundary such as the admin API). None passes
    through as None; an unparseable string returns None rather than raising
    — a corrupt cursor must not crash the crawl, only be treated as absent
    (falling back to the seed-lookback path, the same as a brand-new
    target)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            text = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _format_utc(dt: datetime) -> str:
    """`onlyPostsNewerThan` in the exact ISO-8601 UTC shape Apify's own
    dataset items use (verified live in the plan's Step 0 probe:
    `2026-08-03T14:36:36.000Z`) — sent back in the same shape it is read in,
    so nothing about this value is ever assumed rather than round-tripped."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_RELATIVE_LOOKBACK_RE = re.compile(
    r"^\s*(\d+)\s*(day|days|month|months|year|years)\s*$", re.IGNORECASE
)
_RELATIVE_LOOKBACK_UNIT_DAYS = {"day": 1, "month": 30, "year": 365}


def _parse_relative_lookback(text: str, now: datetime) -> Optional[datetime]:
    """Best-effort local approximation of Apify's own relative-date parsing
    ("3 months" etc, the format `initial_lookback` is stored in and sent to
    the actor UNPARSED). Used ONLY to compute a local cutoff for the pinned-
    post drop (§G) — never sent anywhere — so a 30/365-day-per-month/year
    approximation is fine: Apify computes the REAL filter itself from the
    same string. An unparseable string returns None (no local cutoff; §G's
    drop is skipped for that stream rather than guessing)."""
    if not text:
        return None
    m = _RELATIVE_LOOKBACK_RE.match(text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower().rstrip("s")
    days = _RELATIVE_LOOKBACK_UNIT_DAYS.get(unit)
    if days is None:
        return None
    return now - timedelta(days=n * days)


@dataclass(frozen=True)
class CrawlBound:
    """What one stream's fetch is bounded by. `actor_value` is exactly what
    is sent as `onlyPostsNewerThan`; `cutoff_at` is this module's own best-
    effort absolute UTC cutoff for the SAME bound, used only to decide
    whether a pinned post (§G) is older than what was requested — never sent
    to Apify itself."""

    actor_value: str
    cutoff_at: Optional[datetime]


def compute_bound(
    cursor: Optional[datetime], *, overlap_hours: float, lookback_text: str, now: datetime,
) -> CrawlBound:
    """§B (cursor path) / §C (seed path).

    A target WITH a cursor sends `cursor - overlap`, always UTC, always an
    absolute timestamp — never the run's wall clock (§B: the actor filters
    in UTC, the scheduler fires in Recife time, and Instagram's own
    timestamps lag publication; a wall-clock cursor silently and PERMANENTLY
    drops any post that lands inside that gap).

    A target with NO cursor (brand new, or a stream that has never
    succeeded) sends its configured (or default) lookback UNPARSED, in
    Apify's own relative-date syntax — never converted to an absolute date
    on this side, so Apify's own "now" (not this process's) is what the
    seed window is actually measured from.
    """
    if cursor is not None:
        cutoff = cursor - timedelta(hours=overlap_hours)
        return CrawlBound(actor_value=_format_utc(cutoff), cutoff_at=cutoff)
    return CrawlBound(
        actor_value=lookback_text, cutoff_at=_parse_relative_lookback(lookback_text, now),
    )


def _split_kept_and_dropped(posts: list[dict], cutoff_at: Optional[datetime]) -> tuple[list[dict], int]:
    """§G: a post is dropped when it is PINNED (the only documented way a
    post bypasses `onlyPostsNewerThan` at all) AND older than our own
    cutoff for this bound. A non-pinned post is never dropped here — the
    actor's own filter already bounded it (mod the deliberate overlap). An
    unparseable/missing timestamp is KEPT, not dropped — mirrors this
    repo's existing "a bad value never disqualifies, it just sorts/behaves
    conservatively" convention (`archive_sources._sort_posts_newest_first`).
    Returns (kept, dropped_count) — the count is what the plan requires be
    recorded even though the post itself is discarded."""
    if cutoff_at is None:
        return list(posts), 0
    kept: list[dict] = []
    dropped = 0
    for post in posts:
        ts = _parse_post_timestamp(post.get("timestamp"))
        if post.get("is_pinned") and ts is not None and ts < cutoff_at:
            dropped += 1
            continue
        kept.append(post)
    return kept, dropped


def _newest_timestamp(posts: list[dict]) -> Optional[datetime]:
    """§B: the cursor is the newest POST timestamp among what was actually
    kept — never the run's wall clock, and never influenced by array order
    (a batch can return out of order; this is a max, not "the last one")."""
    best: Optional[datetime] = None
    for post in posts:
        ts = _parse_post_timestamp(post.get("timestamp"))
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def dedupe_posts_by_shortcode(posts_by_stream: dict[str, list[dict]]) -> list[dict]:
    """§A: merge every stream's KEPT posts into one set keyed by `shortcode`,
    preferring the richer payload when two copies differ, so the chain
    archives/classifies/extracts each post exactly once no matter how many
    streams returned it. §Evidence: a reel is also a grid post, so the posts
    and reels streams are not disjoint — without this, the same image was
    archived, classified, and extracted twice (a real, avoidable OpenAI
    cost), even though `uq_event_source_post` already made the DB write
    idempotent.

    Deterministic regardless of `posts_by_stream`'s own key order: streams
    are always considered in a FIXED priority (posts, then reels) — the
    order `run_target` itself always runs them in — so a richness tie always
    keeps the posts-stream copy, never an accident of dict iteration order.
    Cursor advancement is computed separately, per stream, from each
    stream's OWN kept list BEFORE this runs — deduping is about what gets
    processed, never about what a stream has seen, so it must never affect
    which timestamp a cursor moves to.

    A post with no shortcode cannot be deduplicated (nothing to key it on)
    and is always kept, from every stream — mirrors this codebase's existing
    "a bad/missing value never disqualifies" convention.

    A thin, fixed-priority wrapper over `post_dedupe.dedupe_by_shortcode` —
    see that module for why the mechanism itself was extracted (plans/
    260811_extract-by-handle.md §A reuses it for a different overlap: the
    same archived post found under two S3 prefixes)."""
    return dedupe_by_shortcode([
        posts_by_stream.get(STREAM_POSTS, []), posts_by_stream.get(STREAM_REELS, []),
    ])


CLASSIFICATION_OUTCOME_CLASSIFIED = "classified"
CLASSIFICATION_OUTCOME_FAILED = "classification_failed"
CLASSIFICATION_OUTCOME_SKIPPED_TARGET_DISABLED = "skipped_target_disabled"
CLASSIFICATION_OUTCOME_SKIPPED_NO_CLASSIFIER = "skipped_no_classifier"
CLASSIFICATION_OUTCOME_SKIPPED_NO_PHOTOS = "skipped_no_photos"


@dataclass
class _ChainReport:
    archived: int = 0
    extracted: bool = False
    # None for the promoter path (which never classifies — a separate,
    # pre-existing design choice, not something this feature changed).
    # Populated for the venue path so "we classified and found no flyer" is
    # never confused with "classification never ran for this crawl" — see
    # CRAWL_CHAIN_CLASSIFICATION_TOTAL, which is incremented from the same
    # value.
    classification_outcome: Optional[str] = None


class InstagramCrawlChainer:
    """§H: after a crawl that returned new posts, archive them and run
    extraction — "nothing new is invented: the chain calls the same
    services the operator's manual sequence calls today, in the same
    order." See the module docstring for exactly which existing pieces each
    kind reuses and why `.run()` itself is not the reuse point.

    Every collaborator is optional and independently dependency-aware,
    matching every other enrichment path in this codebase (CLAUDE.md
    "Architecture Guardrails"): missing media storage means no archiving
    happens; missing extraction/promoter services mean that half is
    skipped. A crawl never fails because chaining could not run — the posts
    are still fetched, cursored, and billed for either way.
    """

    def __init__(
        self,
        *,
        media_store=None,
        downloader=None,
        event_extraction_service=None,
        promoter_crawl_service=None,
        photo_classifier=None,
        archive_source: str = SOURCE_INSTAGRAM_POSTS,
    ):
        self.media_store = media_store
        self.downloader = downloader
        self.event_extraction_service = event_extraction_service
        self.promoter_crawl_service = promoter_crawl_service
        # The SAME classifier VenuePhotoArchiveService uses
        # (PhotoClassificationService.annotate) — reused, not reimplemented,
        # so an image-only flyer (no caption event-marker) still reaches
        # extraction the scheduled path replaces a manual archive run for.
        self.photo_classifier = photo_classifier
        self.archive_source = archive_source

    async def chain_venue(
        self, *, handle: str, venue_ids: list[str], new_posts: list[dict], now: datetime,
        classify_images: bool = True, venue_dao=None,
    ) -> _ChainReport:
        report = _ChainReport()
        if not venue_ids or not new_posts:
            return report
        # §C: a handle shared by several venues must have each post
        # attributed to ONE of them, from its own content — never fanned out
        # to every venue (the bug this branch replaces: every post archived
        # under every venue, and `EventExtractionService` extracting it once
        # per venue, with the LAST venue processed silently winning the
        # `uq_event_source_post` upsert — see the plan's §Evidence and the
        # PR for what was actually observed against real Postgres).
        # `len(venue_ids) == 1` — the common case — falls through to the
        # UNCHANGED code below untouched: this branch never runs for it, so
        # that path stays byte-for-byte identical to before this feature.
        if len(venue_ids) > 1:
            if venue_dao is None:
                logger.error(
                    f"[InstagramCrawl] {handle}: shared handle across "
                    f"{len(venue_ids)} venues but no venue_dao was supplied to "
                    "chain_venue -- refusing to guess an attribution; nothing "
                    "archived or extracted for this run"
                )
                return report
            return await self._chain_shared_handle(
                handle=handle, venue_ids=venue_ids, new_posts=new_posts, now=now,
                classify_images=classify_images, venue_dao=venue_dao,
            )
        # The common case — a handle mapping to exactly one venue never
        # resolves anything; §Error Handling still wants it counted, so the
        # `resolved`/`ambiguous` outcomes above have a baseline to compare
        # against. Observability only: no functional change to this branch.
        CRAWL_VENUE_ATTRIBUTION_TOTAL.labels(outcome="single_venue").inc(len(new_posts))
        if self.media_store is not None and self.downloader is not None:
            prefix = run_prefix(self.archive_source, now, new_run_id(now))
            classification_outcome = CLASSIFICATION_OUTCOME_SKIPPED_NO_PHOTOS
            for venue_id in venue_ids:
                manifest_entries, outcome = await self._archive_venue_posts(
                    prefix, venue_id, handle, new_posts, classify=classify_images,
                )
                classification_outcome = outcome
                if manifest_entries:
                    try:
                        await self.media_store.put_manifest(
                            prefix=prefix, venue_id=venue_id, manifest={"photos": manifest_entries},
                        )
                        report.archived += len(manifest_entries)
                    except Exception as e:
                        logger.error(
                            f"[InstagramCrawl] manifest write failed for venue {venue_id}: {e}"
                        )
            report.classification_outcome = classification_outcome
            CRAWL_CHAIN_CLASSIFICATION_TOTAL.labels(outcome=classification_outcome).inc()
            if classification_outcome in (
                CLASSIFICATION_OUTCOME_SKIPPED_NO_CLASSIFIER,
                CLASSIFICATION_OUTCOME_FAILED,
            ):
                logger.warning(
                    f"[InstagramCrawl] {handle}: archived without image "
                    f"classification ({classification_outcome}) — extraction "
                    "for this crawl can only qualify posts by caption; an "
                    "image-only flyer with no event-marker caption text will "
                    "be missed, not correctly judged not-an-event"
                )
        if self.event_extraction_service is not None:
            try:
                await self.event_extraction_service.run(
                    {"eligibility": {"mode": "venue_ids", "venue_ids": ",".join(venue_ids)}}
                )
                report.extracted = True
            except Exception as e:
                logger.error(f"[InstagramCrawl] event extraction failed for {handle}: {e}")
        return report

    async def _archive_venue_posts(
        self, prefix: str, venue_id: str, handle: str, posts: list[dict], *, classify: bool,
    ) -> tuple[list[dict], str]:
        """Downloads every post's images, classifies them (unless `classify`
        is False or no classifier is wired), then stores them and returns
        (manifest_entries, classification_outcome).

        Mirrors `VenuePhotoArchiveService._attach_download_bytes` ->
        `_classify_photos` -> `_store_photo`'s own sequence: bytes are
        downloaded ONCE and reused for both classification and storage
        (never fetched twice), and a classifier failure never costs a photo
        its archive — it is stored with no category, exactly like the
        manual path degrades.
        """
        photos: list[dict] = []
        for post in posts:
            shortcode = post.get("shortcode")
            urls = post.get("image_urls") or []
            for idx, url in enumerate(urls, start=1):
                photos.append({
                    "url": url, "shortcode": shortcode, "caption": post.get("caption"),
                    "permalink": post.get("permalink"),
                    "uploaded_at": post.get("timestamp") or None,
                    "likes_count": post.get("likes_count"),
                    "comments_count": post.get("comments_count"),
                    "post_type": post.get("post_type"),
                    "_photo_id": f"{shortcode}_{idx}" if len(urls) > 1 else (shortcode or f"post_{idx}"),
                })
        if not photos:
            return [], CLASSIFICATION_OUTCOME_SKIPPED_NO_PHOTOS

        for photo in photos:
            try:
                data, content_type = await self.downloader.download(photo["url"])
            except Exception as e:
                logger.warning(
                    f"[InstagramCrawl] image download failed for {handle}/{photo['shortcode']}: {e}"
                )
                continue
            photo["_bytes"] = data
            photo["_content_type"] = content_type

        downloaded = [p for p in photos if "_bytes" in p]
        if not downloaded:
            return [], CLASSIFICATION_OUTCOME_SKIPPED_NO_PHOTOS

        classification_outcome = CLASSIFICATION_OUTCOME_SKIPPED_TARGET_DISABLED
        if classify:
            if self.photo_classifier is not None:
                try:
                    await self.photo_classifier.annotate(
                        downloaded,
                        # The attribute schema is a VENUE-profile concept
                        # (vibe/interior attributes) this crawl has no use
                        # for; only `category`/`classification_confidence`
                        # (post_qualifies's flyer gate) are needed, so the
                        # attribute pass — most of the token cost — is off.
                        derive_attributes=False,
                        venue_id=venue_id, require_bytes=True,
                    )
                    classification_outcome = CLASSIFICATION_OUTCOME_CLASSIFIED
                except Exception as e:
                    logger.error(
                        f"[InstagramCrawl] classification failed for {handle}: {e}; "
                        "photos archived without a category"
                    )
                    classification_outcome = CLASSIFICATION_OUTCOME_FAILED
            else:
                classification_outcome = CLASSIFICATION_OUTCOME_SKIPPED_NO_CLASSIFIER

        entries: list[dict] = []
        for photo in downloaded:
            try:
                key = await self.media_store.put_image(
                    prefix=prefix, venue_id=venue_id, photo_id=photo["_photo_id"],
                    data=photo["_bytes"], content_type=photo["_content_type"],
                    category=photo.get("category"),
                )
            except Exception as e:
                logger.warning(
                    f"[InstagramCrawl] image archive failed for {handle}/{photo['shortcode']}: {e}"
                )
                continue
            entries.append({
                "key": key, "instagram_photo_id": photo["_photo_id"],
                "caption": photo.get("caption"), "permalink": photo.get("permalink"),
                "shortcode": photo.get("shortcode"), "uploaded_at": photo.get("uploaded_at"),
                "likes_count": photo.get("likes_count"), "comments_count": photo.get("comments_count"),
                "post_type": photo.get("post_type"), "photo_name": None,
                # What EventPostSource/post_qualifies actually read to
                # qualify an image-only flyer. `archive_sources._fetch_
                # instagram` fetches raw photos with no category of its own
                # (Instagram never provides one); `VenuePhotoArchiveService.
                # run()`'s OUTER loop is what classifies them, right before
                # storing — this method does the same two steps itself,
                # since it does not call that outer loop (see the module
                # docstring for why).
                "category": photo.get("category"),
                "classification_confidence": photo.get("classification_confidence"),
            })
        return entries, classification_outcome

    async def _chain_shared_handle(
        self, *, handle: str, venue_ids: list[str], new_posts: list[dict], now: datetime,
        classify_images: bool, venue_dao,
    ) -> _ChainReport:
        """plans/260810_post-kind-and-post-extraction-attribution.md §C:
        attribution now runs AFTER extraction, using the model's own
        `location_text` — falling back to the post's own caption when the
        model reported none — rather than guessing from the raw caption
        BEFORE the model has read the post. `260810_stream-dedupe-and-
        venue-attribution.md`'s own executing agent measured the old
        approach's weakness: a caption must closely echo a venue's own name
        to clear the confidence floor, so a post that merely mentions a
        branch in passing queued as unresolved even though the model, one
        step later, reads the location plainly.

        Every post is archived under the HANDLE — never per-venue, resolved
        or not, and never moved afterward: the promoter path already stores
        under a handle prefix and the console already renders those covers
        correctly, so a handle-prefixed key is a supported shape, not a
        compromise (moving objects would cost a copy per image and
        invalidate `cover_photo_key`s already handed out).

        Reuses `PromoterCrawlService`'s own per-post pipeline
        (`_archive_post_images`/`_process_post`) wholesale — the SAME
        machinery `chain_promoter` already calls for a real promoter
        account — bounded to THIS handle's own venues as the candidate set
        (`candidate_venues_for_ids`, the resolver
        `260810_stream-dedupe-and-venue-attribution.md` §C introduced;
        never a second matcher), with `location_text_fallback_to_caption=
        True` so a post whose `location_text` the model left null still
        resolves off its own caption. `EVENT_VENUE_LINK_TOTAL` (emitted by
        `_process_post` itself, per post) is now the sole attribution-
        outcome metric for this path — recording the same decision a step
        earlier, in a second counter, would be exactly the kind of drift
        this repo has already been bitten by (CLAUDE.md).

        `len(venue_ids) == 1` — the common case — never reaches this method
        at all (see `chain_venue`): that path stays byte-for-byte identical.
        """
        report = _ChainReport()
        svc = self.promoter_crawl_service
        if svc is None:
            logger.warning(
                f"[InstagramCrawl] {handle}: shared across {len(venue_ids)} venues "
                "but no promoter crawl service is wired -- refusing to guess an "
                "attribution; nothing archived or extracted for this run"
            )
            return report

        candidates = candidate_venues_for_ids(venue_dao, venue_ids)
        handle_index = build_handle_index(venue_dao)
        prefix = (
            run_prefix(svc.archive_source, now, new_run_id(now))
            if svc.media_store is not None else None
        )
        manifest_entries: list[dict] = []
        for post in new_posts:
            archived_images: list[dict] = []
            if prefix is not None:
                archived_images = await svc._archive_post_images(prefix, handle, post)
                manifest_entries.extend(archived_images)
                report.archived += len(archived_images)
            # plans/260810_date-correctness-review-reasons-and-path-parity.md
            # §D: `source_kind=SOURCE_KIND_VENUE_POST` — these posts come
            # from a VENUE's own handle, never a promoter's, and stamping
            # them `promoter_post` (the pre-existing default) is the
            # provenance bug this plan fixes. `attribution_outcomes`
            # collects every event this POST's resolution ladder ran for, so
            # CRAWL_VENUE_ATTRIBUTION_TOTAL can be bumped once per post
            # below — `resolved` when any of them auto-linked, `ambiguous`
            # otherwise (queued or unresolved); a post that never reached
            # attribution at all (filtered out, or extraction failed/
            # truncated) contributes nothing, exactly like `_process_post`'s
            # own docstring describes. `report_kind_metric=True` makes the
            # event/non-event kind split visible on this path too, matching
            # what `EventExtractionService.run()` already reports for the
            # single-venue path.
            attribution_outcomes: list[str] = []
            persisted = await svc._process_post(
                handle=handle, post=post, venues=candidates, handle_index=handle_index,
                now=now, archived_images=archived_images,
                location_text_fallback_to_caption=True,
                source_kind=SOURCE_KIND_VENUE_POST,
                attribution_outcomes=attribution_outcomes,
                report_kind_metric=True,
            )
            if persisted:
                report.extracted = True
            if attribution_outcomes:
                outcome = "resolved" if RESOLUTION_AUTO in attribution_outcomes else "ambiguous"
                CRAWL_VENUE_ATTRIBUTION_TOTAL.labels(outcome=outcome).inc()
        if prefix is not None and manifest_entries:
            try:
                await svc.media_store.put_promoter_manifest(
                    prefix=prefix, handle=handle,
                    manifest={"handle": handle, "photos": manifest_entries},
                )
            except Exception as e:
                logger.error(
                    f"[InstagramCrawl] shared-handle manifest write failed for {handle}: {e}"
                )
        # plans/260810_date-correctness-review-reasons-and-path-parity.md
        # §D: this path never calls EventExtractionService.run() (that is
        # the whole reason it needs its own metrics parity), so it must
        # refresh EVENTS_TOTAL itself — otherwise a handle that only ever
        # goes through this branch leaves the gauge permanently at zero.
        update_events_gauge(venue_dao)
        return report

    async def chain_promoter(
        self, *, handle: str, new_posts: list[dict], now: datetime,
    ) -> _ChainReport:
        report = _ChainReport()
        svc = self.promoter_crawl_service
        if svc is None or not new_posts:
            return report
        prefix = (
            run_prefix(svc.archive_source, now, new_run_id(now))
            if svc.media_store is not None else None
        )
        venues = build_venue_catalog(svc.venue_dao)
        handle_index = build_handle_index(svc.venue_dao)
        manifest_entries: list[dict] = []
        for post in new_posts:
            archived_images: list[dict] = []
            if prefix is not None:
                archived_images = await svc._archive_post_images(prefix, handle, post)
                manifest_entries.extend(archived_images)
            persisted = await svc._process_post(
                handle=handle, post=post, venues=venues, handle_index=handle_index,
                now=now, archived_images=archived_images,
            )
            if persisted:
                report.extracted = True
        if prefix is not None and manifest_entries:
            try:
                await svc.media_store.put_promoter_manifest(
                    prefix=prefix, handle=handle,
                    manifest={"handle": handle, "photos": manifest_entries},
                )
                report.archived = len(manifest_entries)
            except Exception as e:
                logger.error(f"[InstagramCrawl] promoter manifest write failed for {handle}: {e}")
        return report


@dataclass
class CrawlServiceConfig:
    overlap_hours: float = 6.0
    default_initial_lookback: str = "3 months"
    # The STEADY-STATE per-run cap (a run whose relevant cursor is already
    # set) — small on purpose, see `default_seed_results_limit` below for
    # why this is not also the seed's cap.
    default_results_limit: int = 10
    # The SEED cap — applied only when the relevant cursor is null, i.e. at
    # most once per target per stream, ever. Separate from
    # `default_results_limit` because the two want opposite values: steady
    # state wants small (stop a prolific account running away between
    # fires); the seed wants large (its whole purpose is depth). A shared
    # cap of 10 was silently undoing the 3-month date bound's own job.
    default_seed_results_limit: int = 200
    monthly_result_budget: int = 1000
    max_consecutive_failures: int = 5
    result_cost_usd: float = 0.0027


def resolve_results_limit(
    target: dict, stream: str, *, is_seed: bool, config: CrawlServiceConfig,
) -> int:
    """The exact fallback chain `_run_stream` resolves a stream's cap
    through — extracted to a pure function so the admin read model
    (`CrawlTargetOut`'s `effective_*_results_limit` fields) can expose the
    SAME resolution without re-implementing it and risking drift.

    Posts: `results_limit`/`seed_results_limit` -> the matching settings
    default. Unchanged from before reels-specific overrides existed —
    this is the byte-for-byte-unchanged path the operator asked to be kept
    that way.

    Reels: a THIRD, reels-specific column first, THEN the posts column,
    THEN the same settings default posts would use — never a separate
    `crawl_default_reels_*` setting. Deliberately rejected: three months
    of reels is far fewer items than three months of posts, so the
    existing seed default (200) will rarely bind on reels anyway, and a
    second pair of defaults would be config sprawl bought against no
    observed need. A target that sets neither reels column falls all the
    way through to exactly what posts would use, unchanged.
    """
    if stream == STREAM_REELS:
        if is_seed:
            value = (
                target.get("reels_seed_results_limit")
                or target.get("seed_results_limit")
                or config.default_seed_results_limit
            )
        else:
            value = (
                target.get("reels_results_limit")
                or target.get("results_limit")
                or config.default_results_limit
            )
    else:
        if is_seed:
            value = target.get("seed_results_limit") or config.default_seed_results_limit
        else:
            value = target.get("results_limit") or config.default_results_limit
    return int(value)


class ScheduledInstagramCrawlService:
    """Orchestrates one crawl target end to end: gate -> bound -> fetch ->
    pinned-post filtering -> cursor advance (success only) -> chaining. See
    `run_due_targets` for the multi-target, stop-on-credit-exhaustion entry
    point the plan's §Error Handling and the "stop the whole cycle" scenario
    describe."""

    def __init__(
        self,
        *,
        venue_dao,
        apify_client,
        budget_dao,
        config: CrawlServiceConfig,
        chainer: Optional[InstagramCrawlChainer] = None,
        now_provider=None,
    ):
        self.venue_dao = venue_dao
        self.apify_client = apify_client
        self.budget_dao = budget_dao
        self.config = config
        self.chainer = chainer
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        # `asyncio.create_task`'s return value is the ONLY strong reference
        # the event loop gives you — the loop itself holds a weak one, and
        # CPython's own asyncio docs warn that an unreferenced task "can
        # disappear mid-execution" via garbage collection. A crawl started
        # by `start_run` is the longest-lived task in this codebase (a
        # 3-month seed with downloads, classification, and chained
        # extraction — minutes of awaits, exactly the profile that gives
        # the collector opportunities), and if IT disappears mid-flight,
        # `finally` does not reliably run: the per-handle lock is never
        # released, and that handle becomes uncrawlable for the rest of the
        # process's life — every future scheduled fire hits `try_acquire`,
        # logs "already running", and silently skips, forever. This set is
        # the strong reference; `add_done_callback` drops each task the
        # moment it finishes so the set cannot grow unbounded.
        self._background_tasks: set[asyncio.Task] = set()

    # ── one stream (posts or reels) of one target ────────────────────────────
    async def _run_stream(self, target: dict, stream: str, *, now: datetime) -> dict:
        handle = target["handle"]
        kind = target["kind"]
        cursor_field = "cursor_posts_at" if stream == STREAM_POSTS else "cursor_reels_at"
        cursor = _as_utc_dt(target.get(cursor_field))
        lookback = target.get("initial_lookback") or self.config.default_initial_lookback
        bound = compute_bound(
            cursor, overlap_hours=self.config.overlap_hours, lookback_text=lookback, now=now,
        )
        # §C, split in two: a SEED (this stream's cursor still null — at most
        # once per target per stream, ever) wants a large cap for depth; a
        # steady-state run (cursor already set) wants the existing small one
        # to stop a prolific account running away between fires. One shared
        # cap silently undid the date bound's own job on every seed — a
        # venue posting daily has ~90 posts in the 3-month seed window, and
        # a cap of 10 took only the first 10 of them. Split again by STREAM
        # (posts vs reels each get their own fallback chain) — see
        # `resolve_results_limit`'s own docstring for the full chain and why
        # there is no separate `crawl_default_reels_*` setting.
        results_limit = resolve_results_limit(
            target, stream, is_seed=(cursor is None), config=self.config,
        )

        year_month = self.budget_dao.current_year_month_utc(now)
        spent = self.budget_dao.get_month_count(year_month)
        remaining = self.config.monthly_result_budget - spent
        CRAWL_BUDGET_REMAINING.set(max(remaining, 0))
        if remaining <= 0:
            CRAWL_RUNS_TOTAL.labels(handle_kind=kind, result_type=stream, outcome=OUTCOME_SKIPPED_BUDGET).inc()
            logger.warning(
                f"[InstagramCrawl] {handle} {stream}: monthly result budget exhausted "
                f"({spent}/{self.config.monthly_result_budget}); refusing to call Apify"
            )
            return {"outcome": OUTCOME_SKIPPED_BUDGET, "results": 0, "dropped_pinned": 0, "kept": []}

        # The cap itself is never allowed to exceed what remains this month
        # — a 200-result seed cap against 50 remaining must ask for at most
        # 50, not 200-then-hope. Refusing outright instead (remaining < cap)
        # was considered and rejected: a target with 150 left would never
        # seed at all, which is worse than seeding 150 deep. This is the
        # ONLY place the cap and the budget interact; `remaining <= 0` above
        # is unchanged and still the sole refusal condition.
        effective_limit = min(results_limit, remaining)
        if effective_limit < results_limit:
            logger.info(
                f"[InstagramCrawl] {handle} {stream}: cap reduced from "
                f"{results_limit} to {effective_limit} by the remaining "
                f"monthly budget ({remaining})"
            )

        try:
            raw_posts = await self.apify_client.fetch_recent_posts(
                handle, results_limit=effective_limit,
                only_posts_newer_than=bound.actor_value, results_type=stream,
            )
        except ApifyCreditExhaustedError:
            CRAWL_RUNS_TOTAL.labels(handle_kind=kind, result_type=stream, outcome=OUTCOME_CREDIT_EXHAUSTED).inc()
            logger.error(f"[InstagramCrawl] {handle} {stream}: Apify credit exhausted")
            return {"outcome": OUTCOME_CREDIT_EXHAUSTED, "results": 0, "dropped_pinned": 0, "kept": []}
        except Exception as e:
            CRAWL_RUNS_TOTAL.labels(handle_kind=kind, result_type=stream, outcome=OUTCOME_FAILED).inc()
            logger.error(f"[InstagramCrawl] {handle} {stream}: fetch failed: {e}")
            return {"outcome": OUTCOME_FAILED, "results": 0, "dropped_pinned": 0, "kept": [], "error": str(e)}

        result_count = len(raw_posts or [])
        if result_count:
            # The billed number — counted regardless of what happens to each
            # item next (a pinned drop is still a billed result — §G).
            CRAWL_RESULTS_TOTAL.labels(result_type=stream).inc(result_count)
            new_spent = self.budget_dao.increment_month(year_month, result_count)
            CRAWL_BUDGET_REMAINING.set(max(self.config.monthly_result_budget - new_spent, 0))

        kept, dropped = _split_kept_and_dropped(raw_posts or [], bound.cutoff_at)

        if not kept:
            CRAWL_RUNS_TOTAL.labels(handle_kind=kind, result_type=stream, outcome=OUTCOME_EMPTY).inc()
            return {
                "outcome": OUTCOME_EMPTY, "results": result_count,
                "dropped_pinned": dropped, "kept": [],
            }

        CRAWL_RUNS_TOTAL.labels(handle_kind=kind, result_type=stream, outcome=OUTCOME_SUCCESS).inc()
        return {
            "outcome": OUTCOME_SUCCESS, "results": result_count, "dropped_pinned": dropped,
            "kept": kept, "new_cursor": _newest_timestamp(kept),
        }

    # ── one target (all its streams) ─────────────────────────────────────────
    async def run_target(self, handle: str) -> dict:
        target = self.venue_dao.get_crawl_target(handle)
        if target is None:
            return {"handle": handle, "outcome": OUTCOME_NOT_FOUND, "streams": {}}

        kind = target["kind"]
        if not target.get("enabled", True):
            CRAWL_RUNS_TOTAL.labels(handle_kind=kind, result_type=STREAM_POSTS, outcome=OUTCOME_SKIPPED_DISABLED).inc()
            return {"handle": handle, "outcome": OUTCOME_SKIPPED_DISABLED, "streams": {}}
        if int(target.get("consecutive_failures") or 0) >= self.config.max_consecutive_failures:
            CRAWL_RUNS_TOTAL.labels(handle_kind=kind, result_type=STREAM_POSTS, outcome=OUTCOME_SKIPPED_FAILURES).inc()
            return {"handle": handle, "outcome": OUTCOME_SKIPPED_FAILURES, "streams": {}}

        now = self._now()
        streams = [STREAM_POSTS] + ([STREAM_REELS] if target.get("crawl_reels") else [])

        stream_reports: dict[str, dict] = {}
        credit_exhausted = False
        any_failed = False
        cursor_updates: dict[str, datetime] = {}
        new_posts_by_stream: dict[str, list[dict]] = {}

        for stream in streams:
            stream_report = await self._run_stream(target, stream, now=now)
            stream_reports[stream] = stream_report
            outcome = stream_report["outcome"]
            if outcome == OUTCOME_CREDIT_EXHAUSTED:
                credit_exhausted = True
                break
            if outcome == OUTCOME_FAILED:
                any_failed = True
                continue
            if outcome == OUTCOME_SUCCESS:
                field_name = "cursor_posts_at" if stream == STREAM_POSTS else "cursor_reels_at"
                cursor_updates[field_name] = stream_report["new_cursor"]
                new_posts_by_stream[stream] = stream_report["kept"]

        total_results = sum(r.get("results", 0) for r in stream_reports.values())
        updates: dict = dict(cursor_updates)
        if not credit_exhausted:
            updates["last_run_at"] = now
            updates["last_run_results"] = total_results
            updates["last_run_cost_usd"] = round(total_results * self.config.result_cost_usd, 6)
            updates["consecutive_failures"] = (
                int(target.get("consecutive_failures") or 0) + 1 if any_failed else 0
            )
            reels_report = stream_reports.get(STREAM_REELS)
            if reels_report is not None and reels_report["outcome"] in (OUTCOME_SUCCESS, OUTCOME_EMPTY):
                # §B: make the reels stream's marginal contribution visible.
                # "Fetched" is the billed count (what CRAWL_RESULTS_TOTAL
                # also counts); "new" is how many of the KEPT reel results
                # (post-pinned-drop) do not share a shortcode with a KEPT
                # posts-stream result THIS run. Written only when reels
                # actually ran and returned a trustworthy answer (success or
                # a genuine empty result) — never on failed/credit-exhausted/
                # skipped-budget, where there is nothing real to report and
                # the previous run's numbers are left standing rather than
                # overwritten with a guess.
                posts_shortcodes = {
                    p.get("shortcode") for p in new_posts_by_stream.get(STREAM_POSTS, [])
                    if p.get("shortcode")
                }
                reel_posts = new_posts_by_stream.get(STREAM_REELS, [])
                overlap_count = sum(
                    1 for p in reel_posts if p.get("shortcode") in posts_shortcodes
                )
                if overlap_count:
                    CRAWL_STREAM_OVERLAP_TOTAL.labels(result_type=STREAM_REELS).inc(overlap_count)
                updates["last_run_reels_fetched"] = reels_report["results"]
                updates["last_run_reels_new"] = len(reel_posts) - overlap_count
        if updates:
            try:
                self.venue_dao.update_crawl_target(handle, updates)
            except Exception as e:
                # Results for this run are already scraped and BILLED (see
                # `_run_stream`'s CRAWL_RESULTS_TOTAL/budget increment, which
                # already happened above) — this write only persists the
                # bookkeeping, so its failure must not look like a healthy
                # run. Count it as a failure (bumping the SAME
                # `consecutive_failures` counter `run_target`'s own
                # enabled/failure-threshold gate reads) via a second, minimal
                # write that carries none of the fields that may have caused
                # the first one to fail — otherwise a target stuck here would
                # keep `consecutive_failures` at 0 forever, look perfectly
                # healthy, and keep spending on every future run while
                # landing nothing (the exact production incident this guards
                # against: 2026-08-09, entreamigos.praia, $0.10 billed and
                # discarded with `consecutive_failures` still 0).
                CRAWL_RUNS_TOTAL.labels(
                    handle_kind=kind, result_type=STREAM_POSTS, outcome=OUTCOME_BOOKKEEPING_FAILED,
                ).inc()
                logger.error(
                    f"[InstagramCrawl] {handle}: post-run bookkeeping write failed "
                    f"AFTER {total_results} results were already scraped and billed "
                    f"(≈${updates.get('last_run_cost_usd', 0):.4f}): {e}"
                )
                failures = int(target.get("consecutive_failures") or 0) + 1
                try:
                    self.venue_dao.update_crawl_target(handle, {"consecutive_failures": failures})
                except Exception as e2:
                    logger.error(
                        f"[InstagramCrawl] {handle}: could not even record the "
                        f"bookkeeping failure itself — target will look healthy "
                        f"until this is fixed: {e2}"
                    )
                raise

        self._record_cursor_age(handle, target, updates, now)

        # §A: merge every stream's kept posts into one set keyed by
        # shortcode BEFORE chaining — deduping is about what gets
        # PROCESSED; the per-stream cursor advances above already used each
        # stream's OWN kept list, untouched by this.
        all_new_posts = dedupe_posts_by_shortcode(new_posts_by_stream)
        chained = None
        if all_new_posts and not credit_exhausted and self.chainer is not None:
            chained = await self._chain(target, all_new_posts, now)

        return {
            "handle": handle, "kind": kind, "streams": stream_reports,
            "credit_exhausted": credit_exhausted, "chained": chained,
        }

    async def _chain(self, target: dict, new_posts: list[dict], now: datetime):
        handle = target["handle"]
        if target["kind"] == KIND_PROMOTER:
            return await self.chainer.chain_promoter(handle=handle, new_posts=new_posts, now=now)
        handles = self.venue_dao.list_instagram_handles() or {}
        venue_ids = group_venue_ids_by_handle(handles).get(handle, [])
        if not venue_ids:
            logger.warning(
                f"[InstagramCrawl] {handle}: kind=venue crawl target has no venue "
                "currently pointing at this handle; nothing to archive/extract"
            )
            return None
        return await self.chainer.chain_venue(
            handle=handle, venue_ids=venue_ids, new_posts=new_posts, now=now,
            classify_images=bool(target.get("classify_images", True)),
            venue_dao=self.venue_dao,
        )

    def _record_cursor_age(self, handle: str, target: dict, updates: dict, now: datetime) -> None:
        for stream, field_name in ((STREAM_POSTS, "cursor_posts_at"), (STREAM_REELS, "cursor_reels_at")):
            cursor = _as_utc_dt(updates.get(field_name, target.get(field_name)))
            if cursor is not None:
                CRAWL_CURSOR_AGE_SECONDS.labels(handle=handle, result_type=stream).set(
                    max((now - cursor).total_seconds(), 0.0)
                )

    # ── several targets, one gated cycle ─────────────────────────────────────
    async def run_due_targets(self, handles: list[str]) -> dict:
        """Runs each handle in order via `run_target`, STOPPING before the
        next one the moment a target's scrape reports Apify credit
        exhaustion (§Error Handling: "a scheduled run that exhausts credit
        has to stop the whole cycle, not fail one target and continue into
        an empty balance for the rest") — mirrors the exact break-on-
        `ApifyCreditExhaustedError` shape `InstagramPostsEnrichmentService.
        enrich_all_venues` and `PromoterCrawlService.run` already use for
        their own catalog-wide loops.

        Each individually-scheduled per-target APScheduler job (main.py)
        calls this with a single-element list; an admin "run several due
        targets now" action is the natural multi-element caller."""
        reports = []
        stopped_early = False
        for handle in handles:
            report = await self.run_target(handle)
            reports.append(report)
            if report.get("credit_exhausted"):
                stopped_early = True
                break
        return {"targets": reports, "stopped_early": stopped_early}

    # ── admin run-now: backgrounded, locked, never blocks the caller ────────
    def is_running(self, handle: str) -> bool:
        """Whether a crawl of this handle is in flight right now — scheduled
        OR admin-triggered, since both hold the SAME lock (`lock_name_for`).
        Backs `CrawlTargetOut.running` in the admin API, the console's live
        indicator (polled the same way the Jobs page already polls
        `job.running`)."""
        return job_lock.is_running(lock_name_for(handle))

    def _start_refusal(self, target: dict) -> Optional[str]:
        """Whether `start_run` must refuse before doing anything, checked
        while its lock is already held so the caller can release it and
        answer immediately — never starting a background task that would
        only discover the same refusal itself a moment later.

        Deliberately NOT shared with `run_target`'s own inline enabled/
        failure-threshold checks or `_run_stream`'s own budget check: this
        is an ADDITIONAL fast-path gate for the synchronous response
        `start_run` owes its caller, not a replacement for either existing,
        already-tested gate — refactoring them together risked changing
        `run_target`'s tested outcome/streams shape for no behavioral gain.
        """
        if not target.get("enabled", True):
            return OUTCOME_SKIPPED_DISABLED
        if int(target.get("consecutive_failures") or 0) >= self.config.max_consecutive_failures:
            return OUTCOME_SKIPPED_FAILURES
        now = self._now()
        year_month = self.budget_dao.current_year_month_utc(now)
        spent = self.budget_dao.get_month_count(year_month)
        if (self.config.monthly_result_budget - spent) <= 0:
            return OUTCOME_SKIPPED_BUDGET
        return None

    async def start_run(self, handle: str) -> dict:
        """Starts `handle`'s crawl in the BACKGROUND and returns
        immediately — never blocks on the crawl finishing.

        Why this exists (found chasing an operator request for an
        immediate first crawl after setting a cron): `run_crawl_target_now`
        used to await `run_target` inline. A 3-month seed — scrape,
        download, classify, chain extraction — takes minutes; vibes_bot's
        admin proxy times out at 10s and Caddy at 180s. A synchronous call
        either times out while the crawl keeps running server-side (the
        operator sees a failure, clicks again, and now races their OWN
        first click) or simply cannot be shown as "in progress" at all. It
        also took NO lock, so a run-now could race a scheduled fire of the
        same handle — two Apify bills, two cursor writes, last-write-wins
        on a field whose whole purpose is to never move backwards. Both are
        closed here: the SAME per-handle lock the scheduler uses is held
        for the crawl's entire duration, and every gate that must run
        before anything is spent (enabled, failure threshold, the monthly
        budget) runs BEFORE the background task is even created — a
        backgrounded start must never become a way around them.

        Returns `{"started": True}` once the lock is held and the
        background task is scheduled, or `{"started": False, "reason":
        ...}` when it refuses to start at all (`not_found`,
        `already_running`, `skipped_disabled`, `skipped_failures`,
        `skipped_budget`) — a reason string the caller can surface
        verbatim. Completion stays observable through the fields
        `run_target` already writes: `last_run_at`, `last_run_results`,
        `last_run_cost_usd`, `consecutive_failures`.
        """
        target = self.venue_dao.get_crawl_target(handle)
        if target is None:
            return {"started": False, "reason": OUTCOME_NOT_FOUND}

        lock_name = lock_name_for(handle)
        if not job_lock.try_acquire(lock_name):
            return {"started": False, "reason": REASON_ALREADY_RUNNING}

        refusal = self._start_refusal(target)
        if refusal is not None:
            job_lock.release(lock_name)
            return {"started": False, "reason": refusal}

        async def _run_and_release() -> None:
            try:
                await self.run_target(handle)
            except Exception as e:
                logger.error(f"[InstagramCrawl] backgrounded run-now failed for {handle}: {e}")
            finally:
                job_lock.release(lock_name)

        task = asyncio.create_task(_run_and_release())
        # Hold a STRONG reference for the task's whole lifetime (see the
        # comment on `self._background_tasks` in `__init__` for why this is
        # not optional here) and drop it the moment the task finishes, so
        # the set never grows past however many crawls are truly in flight.
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return {"started": True}
