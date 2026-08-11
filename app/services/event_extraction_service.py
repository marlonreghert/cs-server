"""Turn archived Instagram posts of event-candidate venues into structured
events. See plans/260804_instagram-event-extraction.md and
plans/260806_venue-post-multi-event.md.

The cost gate (§A of the plan): a post is sent to the model ONLY when an
archived image of it classified `flyer` above the confidence floor, OR its
caption matched the event-marker matcher already built for event venue
targeting (app/services/event_caption_matcher.py, reused verbatim — not
duplicated). Everything else costs nothing; `post_qualifies` is a pure
function so that guarantee is directly unit-testable on call count.

One OpenAI call per post, never a batch (§B): this schema's output is
variable-length (a twelve-act line-up vs one DJ), and docs/venue-retrieval-
storage.md §4 already recorded a batch of 20 images losing 4 verdicts to a
flat `max_tokens` truncation. A per-post failure here is isolated to one post.

A venue's own post can announce SEVERAL events, not just one
(plans/260806_venue-post-multi-event.md) — the same multi-event extraction
call and per-item isolation `PromoterCrawlService` already established
(`extract_events()` -> (raw_text, truncated),
`parse_multi_event_extraction_response()`). Every event a venue post
announces is attributed to the POSTING venue: a venue post's own
`location_text` is recorded but never re-attributes the event elsewhere —
the resolution ladder is a promoter-path concept and never runs here.

Dates are resolved by app/services/event_date_resolver.py, a deterministic,
independently unit-tested function — never trusted to the model, and never
computed against the run clock (only the post's own timestamp).

The actual per-post reconciliation (identity by content-derived
`source_event_key`, confirmed/manually-linked preservation, upsert,
supersession) lives in app/services/event_reconciliation.py, SHARED with
`PromoterCrawlService` — not duplicated here. See that module's docstring
for what it owns and the one thing (venue attribution) it parameterises.

`EventPostSource` walks the SAME archived manifests
`app/services/event_venue_targeting.ArchivedFlyerEvidenceSource` already
established (`list_run_prefixes`/`read_manifest` over the `instagram_posts`
archive source), reusing its `_run_prefix_date`/`_parse_timestamp` helpers
rather than inventing a second S3-walking scheme. It differs in granularity
only: that class sums a per-venue flyer COUNT for evidence scoring; this
returns per-POST detail (which photo, if any, is the flyer), because
extraction has to isolate one image per post, not tally them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app.api.openai_event_extraction_client import (
    EventExtractionParseError,
    parse_multi_event_extraction_response,
)
from app.metrics import (
    EVENT_EXTRACTION_MALFORMED_ATTRACTIONS_TOTAL,
    EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL,
    EVENT_EXTRACTION_POSTS_TOTAL,
    EVENT_EXTRACTION_SUPERSEDED_TOTAL,
)
from app.models.event_kind import NON_EVENT_KINDS
from app.models.photo_taxonomy import CATEGORY_FLYER
from app.services.event_caption_matcher import matches_event_marker
from app.services.event_date_resolver import (
    REASON_DATE_RANGE,
    REASON_WEEKDAY_MISMATCH,
    REASON_YEAR_INFERRED,
    resolve_event_datetime,
    vote_on_sibling_years,
)
from app.services.event_merge import merge_touched_events
from app.services.event_reconciliation import (
    ALL_STATUSES,
    STATUS_CONFIRMED,
    STATUS_SUPERSEDED,
    new_event_id,
    reconcile_post_events,
    update_events_gauge,
)
from app.services.event_venue_targeting import (
    _parse_timestamp,
    _run_prefix_date,
    resolve_event_candidate_ids,
)
from app.services.instagram_handle_sources import group_venue_ids_by_handle, normalize_handle
from app.services.post_dedupe import dedupe_by_shortcode

logger = logging.getLogger(__name__)

SOURCE_KIND_VENUE_POST = "venue_post"
STATUS_EXTRACTION_FAILED = "extraction_failed"

REVIEW_REASON_LOW_CONFIDENCE = "low_confidence"
REVIEW_REASON_EXTRACTION_FAILED = "extraction_failed"
# The flyer named a time and the extractor produced none — an extraction
# defect, not a date-only event. Kept distinct from a genuinely time-less
# event (stored, marked unknown, never queued) so the review queue is not
# flooded with non-problems. See
# plans/260806_instagram-post-recency-and-unknown-time.md.
REVIEW_REASON_UNREAD_TIME = "unread_time"

OUTCOME_EXTRACTED = "extracted"
OUTCOME_NOT_EVENT_LIKE = "not_event_like"
OUTCOME_NO_DATE = "no_date"
OUTCOME_LOW_CONFIDENCE = "low_confidence"
OUTCOME_EXTRACTION_FAILED = "extraction_failed"
OUTCOME_SKIPPED_SEEN = "skipped_seen"
OUTCOME_UNREAD_TIME = "unread_time"
# The date resolved, but a stated weekday disagreed with the explicit date
# beside it (plans/260807_date-resolution-correctness.md, defect 2b) — a
# distinct outcome from OUTCOME_NO_DATE: `starts_at` is set (from the
# explicit date, the more precise claim), only the disagreement is flagged.
OUTCOME_WEEKDAY_MISMATCH = "weekday_mismatch"
# The year was inferred by rolling a date without a stated year across a
# year boundary (plans/260810_date-correctness-review-reasons-and-path-
# parity.md §A) — checked before the generic `no_date` branch below for the
# same reason weekday_mismatch is: `starts_at` IS set here (from the rolled
# or sibling-corrected date), so this is not a blank the way `no_date` is.
OUTCOME_YEAR_INFERRED = "year_inferred"
# Several dates were stated for one event and only the FIRST was kept as
# `starts_at` (§B) — also not a blank.
OUTCOME_DATE_RANGE = "date_range"
# A multi-event response cut off mid-list (finish_reason == "length"):
# persists nothing partial, distinct from extraction_failed (a truncated
# response means the output budget was too small, not a bad model answer).
# See plans/260806_venue-post-multi-event.md.
OUTCOME_TRUNCATED = "truncated"
# plans/260810_post-kind-and-post-extraction-attribution.md §A/§B: the model
# classified every event this post yielded as something other than `event`
# (promotion/menu/food/other) — no events.event row was created for any of
# them. Distinct from OUTCOME_NOT_EVENT_LIKE, which means the post never
# even reached the model (the caption/flyer pre-filter rejected it); this
# outcome means the model DID look and said "not an event".
OUTCOME_NOT_AN_EVENT = "not_an_event"
# A single-event post's extraction had nothing wrong AND cleared the
# auto-accept predicate (app.services.event_reconciliation.
# is_clean_extraction) — replaces OUTCOME_EXTRACTED for that exact case
# (plans/260807_auto-accept-and-field-level-protection.md). OUTCOME_EXTRACTED
# stays the generic multi-event-post fallback (several events, mixed
# outcomes, no single label applies) — this outcome is strictly the
# single-event success case that used to report "extracted".
OUTCOME_ACCEPTED = "accepted"
# ALL_STATUSES/update_events_gauge now live in event_reconciliation.py (see
# the import above) — plans/260810_date-correctness-review-reasons-and-
# path-parity.md §D: the EVENTS_TOTAL gauge must be refreshed from BOTH
# crawl paths, so this service can no longer be the sole owner of a helper
# the shared-handle path also needs.

# plans/260810_post-kind-and-post-extraction-attribution.md §Error Handling:
# `EVENT_EXTRACTION_POSTS_TOTAL` gains a `kind` label so the event/non-event
# split is visible from the first run — "watch the event share" is the
# whole point, since a misclassified event is silent everywhere else. This
# counter is per POST, but `kind` is a per-EVENT answer, so a value is
# chosen per post outcome:
#   - no event was even parsed (the pre-filter rejected it, or extraction
#     failed/truncated before any kind could be read) -> "not_applicable".
#   - exactly one event was parsed -> that event's own kind, or "unknown"
#     when the model left it missing/blank.
#   - several events were parsed (a roundup post) -> "mixed": no single
#     kind describes the whole post, and attributing one would hide exactly
#     the split this label exists to show.
KIND_LABEL_NOT_APPLICABLE = "not_applicable"
KIND_LABEL_UNKNOWN = "unknown"
KIND_LABEL_MIXED = "mixed"

# plans/260811_extract-by-handle.md §Error Handling: what triggered a post's
# reconciliation call, fed to EVENT_EXTRACTION_SUPERSEDED_TOTAL's `trigger`
# label so a re-extraction's own supersessions are never conflated with an
# ordinary run's.
TRIGGER_EXTRACTION = "extraction"
TRIGGER_HANDLE_REEXTRACTION = "handle_reextraction"

# plans/260811_extract-by-handle.md: per-handle outcome, reported on
# `run()`'s result dict (mode="handles" only) — mirrors
# PromoterCrawlService.run()'s own `report["accounts"]` shape, so an operator
# (or a test) can tell "ran, found nothing" apart from "never even looked"
# without inferring it from `qualifying_posts` alone.
HANDLE_OUTCOME_EXTRACTED = "extracted"
HANDLE_OUTCOME_NOTHING_ARCHIVED = "nothing_archived"
HANDLE_OUTCOME_NO_VENUE_MAPPED = "no_venue_mapped"
HANDLE_OUTCOME_AMBIGUOUS_VENUES = "ambiguous_venues"

DEFAULT_MAX_VENUES = 25
DEFAULT_MAX_POSTS_PER_VENUE = 20
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_FLYER_CONFIDENCE_FLOOR = 0.6
# Sanity bound on events per post — see
# app.config.settings.event_extraction_max_events_per_post, which the output
# token budget also scales from. Mirrors
# promoter_crawl_service.DEFAULT_MAX_EVENTS_PER_POST (kept as an independent
# constant, like every other default in these two sibling services).
DEFAULT_MAX_EVENTS_PER_POST = 20


# ── the archived corpus, one entry per post ──────────────────────────────────
@dataclass(frozen=True)
class ArchivedPost:
    shortcode: str
    permalink: Optional[str]
    caption: Optional[str]
    timestamp: Optional[datetime]
    flyer_photo_key: Optional[str]
    flyer_confidence: Optional[float]
    any_photo_key: Optional[str]
    # Whether the flyer photo's own classification said a time was printed on
    # it ("yes"/"no"), or None when no flyer photo was classified at all — see
    # plans/260806_instagram-post-recency-and-unknown-time.md. Read from the
    # SAME classified flyer already loaded for `flyer_photo_key`, so checking
    # it costs no extra model call.
    flyer_names_time: Optional[str] = None


class EventPostSource:
    """Groups archived `instagram_posts` manifest photo entries by shortcode
    into per-post records, within a lookback window. See the module
    docstring for why this walks the same manifests
    ArchivedFlyerEvidenceSource does rather than a new mechanism.

    plans/260811_extract-by-handle.md §A: a handle's own posts can be
    archived under EITHER of two prefix spellings — `venue_id=<v>` (the
    original per-venue layout) or `promoter=<handle>` (the layout
    `260810_post-kind-and-post-extraction-attribution.md` §C moved a shared
    handle's own posts under) — and a handle's history can span both, if it
    was crawled before and after that change. `posts_for_handle` reads both
    and merges them; `posts_for_venue` (unchanged) reads only the first, for
    the two pre-existing eligibility modes that were never ambiguous about
    where their posts live.
    """

    def __init__(self, media_store, archive_source: str = "instagram_posts"):
        self.media_store = media_store
        self.archive_source = archive_source

    async def _manifests_since(self, since: datetime, read_one) -> list[tuple[Optional[datetime], dict]]:
        """Every `(run_date, manifest)` pair for `self.archive_source`,
        chronologically ascending (oldest first — `list_run_prefixes`'s own
        guarantee), within the lookback window. `read_one(prefix)` reads one
        run's manifest (a venue's `read_manifest` or a handle's
        `read_promoter_manifest`) — the only thing that differs between a
        by-venue and a by-promoter read."""
        try:
            prefixes = await self.media_store.list_run_prefixes(self.archive_source)
        except Exception as e:
            logger.warning(f"[EventExtraction] listing archive runs failed: {e}")
            return []
        out: list[tuple[Optional[datetime], dict]] = []
        for prefix in prefixes:
            run_date = _run_prefix_date(prefix)
            if run_date is not None and run_date < since:
                continue
            try:
                manifest = await read_one(prefix)
            except Exception as e:
                logger.warning(f"[EventExtraction] manifest read failed: {e}")
                continue
            if not manifest:
                continue
            out.append((run_date, manifest))
        return out

    @staticmethod
    def _bucket_entries(manifests: list[tuple[Optional[datetime], dict]]) -> dict[str, dict]:
        """Group manifest photo entries by shortcode — the SAME per-entry
        accumulation `posts_for_venue` has always used (base fields
        first-seen across every run, best flyer evidence across all of
        them), extracted so `posts_for_handle`'s cross-prefix read builds
        identically-shaped buckets. Each bucket also carries `_run_date`,
        the LATEST run date that contributed an entry to it — read only by
        `posts_for_handle`'s cross-prefix "prefers the newest archived
        copy" tie-break, and otherwise inert."""
        grouped: dict[str, dict] = {}
        for run_date, manifest in manifests:
            for entry in manifest.get("photos") or []:
                shortcode = entry.get("shortcode")
                if not shortcode:
                    continue  # not an Instagram-sourced entry; nothing to group by
                bucket = grouped.setdefault(shortcode, {
                    "shortcode": shortcode,
                    "permalink": entry.get("permalink"),
                    "caption": entry.get("caption"),
                    "timestamp": entry.get("uploaded_at"),
                    "flyer_photo_key": None,
                    "flyer_confidence": None,
                    "flyer_names_time": None,
                    "any_photo_key": None,
                    "_run_date": run_date,
                })
                if run_date is not None and (
                    bucket["_run_date"] is None or run_date > bucket["_run_date"]
                ):
                    bucket["_run_date"] = run_date
                if bucket["any_photo_key"] is None:
                    bucket["any_photo_key"] = entry.get("key")
                if entry.get("category") == CATEGORY_FLYER:
                    confidence = entry.get("classification_confidence")
                    best_so_far = bucket["flyer_confidence"] or 0.0
                    if bucket["flyer_photo_key"] is None or (confidence or 0.0) > best_so_far:
                        bucket["flyer_photo_key"] = entry.get("key")
                        bucket["flyer_confidence"] = confidence
                        # Same classified flyer already loaded above — reading
                        # this costs no extra model call.
                        bucket["flyer_names_time"] = (entry.get("attributes") or {}).get(
                            "names_time"
                        )
        return grouped

    @staticmethod
    def _post_from_bucket(bucket: dict) -> ArchivedPost:
        return ArchivedPost(
            shortcode=bucket["shortcode"],
            permalink=bucket["permalink"],
            caption=bucket["caption"],
            timestamp=_parse_timestamp(bucket["timestamp"]),
            flyer_photo_key=bucket["flyer_photo_key"],
            flyer_confidence=bucket["flyer_confidence"],
            flyer_names_time=bucket["flyer_names_time"],
            any_photo_key=bucket["any_photo_key"],
        )

    async def posts_for_venue(self, venue_id: str, since: datetime) -> list[ArchivedPost]:
        if self.media_store is None:
            return []
        manifests = await self._manifests_since(
            since, lambda prefix: self.media_store.read_manifest(prefix, venue_id),
        )
        grouped = self._bucket_entries(manifests)
        return [self._post_from_bucket(b) for b in grouped.values()]

    async def posts_for_promoter(self, handle: str, since: datetime) -> list[ArchivedPost]:
        """The `promoter=<handle>` half of a handle's archive — the SAME
        prefix `PromoterCrawlService`/the shared-handle crawl path writes
        to, read back with zero Apify calls."""
        if self.media_store is None:
            return []
        manifests = await self._manifests_since(
            since, lambda prefix: self.media_store.read_promoter_manifest(prefix, handle),
        )
        grouped = self._bucket_entries(manifests)
        return [self._post_from_bucket(b) for b in grouped.values()]

    async def posts_for_handle(
        self, handle: str, venue_ids: list[str], since: datetime,
    ) -> list[ArchivedPost]:
        """Every post archived for `handle`, reading BOTH archive spellings
        — `promoter=<handle>` and each of `venue_ids`'s own `venue_id=`
        prefix — deduped by shortcode, preferring the newest archived copy
        (§A). Reuses `post_dedupe.dedupe_by_shortcode` — the SAME mechanism
        `instagram_crawl_service.dedupe_posts_by_shortcode` uses for the
        posts/reels stream overlap — rather than a second implementation;
        only the group order and the tie-break function differ.
        """
        if self.media_store is None:
            return []

        def _bucket_list(manifests) -> list[dict]:
            return list(self._bucket_entries(manifests).values())

        promoter_manifests = await self._manifests_since(
            since, lambda prefix: self.media_store.read_promoter_manifest(prefix, handle),
        )
        groups: list[list[dict]] = [_bucket_list(promoter_manifests)]
        for venue_id in dict.fromkeys(venue_ids or []):  # de-dup, preserve order
            venue_manifests = await self._manifests_since(
                since, lambda prefix, v=venue_id: self.media_store.read_manifest(prefix, v),
            )
            groups.append(_bucket_list(venue_manifests))

        def _prefer_newest(candidate: dict, current: dict) -> bool:
            epoch = datetime.min.replace(tzinfo=timezone.utc)
            return (candidate.get("_run_date") or epoch) > (current.get("_run_date") or epoch)

        merged = dedupe_by_shortcode(groups, prefer=_prefer_newest)
        return [self._post_from_bucket(b) for b in merged]

    async def image_data_uri(self, key: Optional[str]) -> Optional[str]:
        if not key or self.media_store is None:
            return None
        return await self.media_store.read_image_data_uri(key)


# ── the pre-filter: the whole cost guarantee ─────────────────────────────────
def post_qualifies(
    post: ArchivedPost, *, flyer_confidence_floor: float = DEFAULT_FLYER_CONFIDENCE_FLOOR,
) -> tuple[bool, str]:
    """(qualifies, reason) — a post is worth a model call when an archived
    image of it classified `flyer` ABOVE the confidence floor, or its caption
    matches an event marker. A flyer photo with no recorded confidence does
    not clear a floor above zero (missing evidence is not "above the floor").
    """
    flyer_confidence = post.flyer_confidence or 0.0
    flyer_qualifies = (
        post.flyer_photo_key is not None and flyer_confidence >= flyer_confidence_floor
    )
    if flyer_qualifies:
        return True, "flyer"
    if matches_event_marker(post.caption):
        return True, "caption"
    return False, OUTCOME_NOT_EVENT_LIKE


# ── run configuration ─────────────────────────────────────────────────────────
class InvalidEventExtractionConfig(ValueError):
    pass


def parse_event_extraction_config(
    config: Optional[dict], *, default_min_confidence: float,
) -> dict:
    cfg = dict(config or {})

    def _non_negative_int(value, field: str, default: int) -> int:
        if value in (None, ""):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise InvalidEventExtractionConfig(f"{field} must be an integer, got {value!r}")
        if parsed < 0:
            raise InvalidEventExtractionConfig(f"{field} must be >= 0, got {parsed}")
        return parsed

    def _fraction(value, field: str, default: float) -> float:
        if value in (None, ""):
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise InvalidEventExtractionConfig(f"{field} must be a number, got {value!r}")
        if not (0.0 <= parsed <= 1.0):
            raise InvalidEventExtractionConfig(f"{field} must be between 0 and 1, got {parsed}")
        return parsed

    eligibility = cfg.get("eligibility") or {}
    mode = eligibility.get("mode") or "event_candidates"
    if mode not in ("event_candidates", "venue_ids", "handles"):
        raise InvalidEventExtractionConfig(f"unknown eligibility mode: {mode!r}")
    venue_ids_raw = str(eligibility.get("venue_ids") or "").strip()
    venue_ids = [v.strip() for v in venue_ids_raw.split(",") if v.strip()]

    # `handles` accepts a comma-separated string OR a list — the SAME shape
    # promoter_crawl_service.parse_promoter_crawl_config already accepts for
    # its own `handles` key, so an operator does not meet two spellings of
    # the same idea (plans/260811_extract-by-handle.md §A).
    handles_raw = eligibility.get("handles")
    if isinstance(handles_raw, str):
        handles = [h.strip() for h in handles_raw.split(",") if h.strip()]
    else:
        handles = [str(h).strip() for h in (handles_raw or []) if str(h).strip()]

    return {
        "eligibility_mode": mode,
        "eligibility_venue_ids": venue_ids,
        "eligibility_handles": handles,
        "max_venues": _non_negative_int(cfg.get("max_venues"), "max_venues", DEFAULT_MAX_VENUES),
        "max_posts_per_venue": _non_negative_int(
            cfg.get("max_posts_per_venue"), "max_posts_per_venue", DEFAULT_MAX_POSTS_PER_VENUE,
        ),
        "lookback_days": _non_negative_int(
            cfg.get("lookback_days"), "lookback_days", DEFAULT_LOOKBACK_DAYS,
        ),
        "min_confidence": _fraction(
            cfg.get("min_confidence"), "min_confidence", default_min_confidence,
        ),
        "dry_run": bool(cfg.get("dry_run")),
    }


# ── orchestration ─────────────────────────────────────────────────────────────
class EventExtractionService:
    def __init__(
        self,
        venue_dao,
        post_source: EventPostSource,
        openai_client,
        *,
        min_confidence: float = 0.5,
        flyer_confidence_floor: float = DEFAULT_FLYER_CONFIDENCE_FLOOR,
        max_events_per_post: int = DEFAULT_MAX_EVENTS_PER_POST,
        now_provider=None,
    ):
        self.venue_dao = venue_dao
        self.post_source = post_source
        self.openai_client = openai_client
        self.min_confidence = min_confidence
        self.flyer_confidence_floor = flyer_confidence_floor
        self.max_events_per_post = max_events_per_post
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    def _resolve_venue_ids(self, cfg: dict) -> list[str]:
        if cfg["eligibility_mode"] == "venue_ids":
            venue_ids = cfg["eligibility_venue_ids"]
        else:
            venue_ids = resolve_event_candidate_ids(self.venue_dao)
        if cfg["max_venues"]:
            venue_ids = venue_ids[: cfg["max_venues"]]
        return venue_ids

    def _handle_for(self, venue_id: str) -> str:
        instagram = self.venue_dao.get_venue_instagram(venue_id)
        handle = getattr(instagram, "instagram_handle", None) if instagram else None
        return handle or venue_id

    async def run(self, config: Optional[dict] = None) -> dict:
        cfg = parse_event_extraction_config(config, default_min_confidence=self.min_confidence)
        now = self._now()
        since = now - timedelta(days=cfg["lookback_days"])

        outcome_counts: dict[str, int] = {}

        def _bump(outcome: str, kind: str = KIND_LABEL_NOT_APPLICABLE) -> None:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            EVENT_EXTRACTION_POSTS_TOTAL.labels(outcome=outcome, kind=kind).inc()

        handle_reports: list[dict] = []
        if cfg["eligibility_mode"] == "handles":
            qualifying_posts = await self._run_handles(cfg, since, _bump, handle_reports)
        else:
            qualifying_posts = 0
            venue_ids = self._resolve_venue_ids(cfg)
            for venue_id in venue_ids:
                posts = await self.post_source.posts_for_venue(venue_id, since)
                if cfg["max_posts_per_venue"]:
                    posts = posts[: cfg["max_posts_per_venue"]]
                handle = self._handle_for(venue_id)

                for post in posts:
                    if not post.shortcode:
                        continue
                    qualifies, _via = post_qualifies(
                        post, flyer_confidence_floor=self.flyer_confidence_floor,
                    )
                    if not qualifies:
                        _bump(OUTCOME_NOT_EVENT_LIKE)
                        continue

                    qualifying_posts += 1
                    if cfg["dry_run"]:
                        continue

                    outcome, kind_label = await self._extract_one(venue_id, handle, post, cfg)
                    _bump(outcome, kind_label)

        if not cfg["dry_run"]:
            update_events_gauge(self.venue_dao)

        return {
            "qualifying_posts": qualifying_posts,
            "outcomes": outcome_counts,
            "dry_run": cfg["dry_run"],
            # Never asserted as fact: measure a real run before trusting this.
            "estimated_cost_usd": None,
            # Additive, mode="handles" only — see HANDLE_OUTCOME_* above.
            "handles": handle_reports,
        }

    async def _run_handles(
        self, cfg: dict, since: datetime, bump, handle_reports: list[dict],
    ) -> int:
        """mode="handles": re-extract the posts already archived for each
        configured handle — reading BOTH archive spellings (§A) and never
        calling Apify. See plans/260811_extract-by-handle.md.

        Attribution is resolved the SAME way a first pass would: a handle
        mapping to exactly one venue attributes directly to it (the common
        case — every scenario this mode was built for). A handle shared by
        SEVERAL venues needs the SAME per-event, post-extraction resolution
        ladder the scheduled shared-handle crawl already runs
        (`InstagramCrawlChainer._chain_shared_handle` ->
        `PromoterCrawlService._process_post`) — a materially different
        pipeline (per-event attribution against `location_text`, not a
        constant `venue_id`) that this read-path feature does not wire in.
        Guessing which of the several venues owns a re-read post would risk
        exactly the silent mis-attribution CLAUDE.md and
        `event_venue_resolution` both forbid, so a multi-venue handle is
        logged and skipped here rather than guessed — a real, deliberate
        scope limit, not an oversight (see the plan's own review notes).
        """
        handle_map = group_venue_ids_by_handle(self.venue_dao.list_instagram_handles() or {})
        qualifying_posts = 0

        for raw_handle in cfg["eligibility_handles"]:
            archive_handle = normalize_handle(raw_handle)
            if not archive_handle:
                continue
            venue_ids = handle_map.get(archive_handle, [])

            if not venue_ids:
                logger.info(
                    f"[EventExtraction] handle {raw_handle!r}: no venue maps to it -- "
                    "nothing to re-extract"
                )
                handle_reports.append({
                    "handle": raw_handle, "outcome": HANDLE_OUTCOME_NO_VENUE_MAPPED,
                })
                continue

            if len(venue_ids) > 1:
                logger.warning(
                    f"[EventExtraction] handle {raw_handle!r} maps to "
                    f"{len(venue_ids)} venues ({', '.join(venue_ids)}) -- "
                    "extract-by-handle only supports a handle mapping to a "
                    "single venue; per-post attribution across several venues "
                    "needs the shared-handle crawl's own resolution ladder, "
                    "not this read path. Refusing to guess; nothing extracted "
                    "for this handle."
                )
                handle_reports.append({
                    "handle": raw_handle, "outcome": HANDLE_OUTCOME_AMBIGUOUS_VENUES,
                    "venue_ids": venue_ids,
                })
                continue

            venue_id = venue_ids[0]
            # The SAME handle string a first pass over this venue would have
            # used (see `_handle_for`, used identically by the venue_ids/
            # event_candidates branch above) — NEVER the operator's raw
            # config input — so a re-extraction's `source_handle` matches
            # the original rows exactly and `reconcile_post_events` can
            # supersede/update them instead of inserting duplicates (§B).
            handle = self._handle_for(venue_id)

            posts = await self.post_source.posts_for_handle(archive_handle, venue_ids, since)
            if not posts:
                logger.info(f"[EventExtraction] handle {raw_handle!r}: nothing archived for it")
                handle_reports.append({
                    "handle": raw_handle, "outcome": HANDLE_OUTCOME_NOTHING_ARCHIVED,
                })
                continue

            if cfg["max_posts_per_venue"]:
                posts = posts[: cfg["max_posts_per_venue"]]

            # §C: cost stated before it is spent -- extraction is free of
            # Apify but not of OpenAI (a vision call per qualifying post).
            qualifying_here = [
                p for p in posts if p.shortcode and post_qualifies(
                    p, flyer_confidence_floor=self.flyer_confidence_floor,
                )[0]
            ]
            logger.info(
                f"[EventExtraction] handle {raw_handle!r}: {len(qualifying_here)} of "
                f"{len(posts)} archived posts qualify for extraction (venue={venue_id})"
            )
            handle_reports.append({
                "handle": raw_handle, "outcome": HANDLE_OUTCOME_EXTRACTED,
                "posts_archived": len(posts), "posts_qualifying": len(qualifying_here),
            })

            for post in posts:
                if not post.shortcode:
                    continue
                qualifies, _via = post_qualifies(
                    post, flyer_confidence_floor=self.flyer_confidence_floor,
                )
                if not qualifies:
                    bump(OUTCOME_NOT_EVENT_LIKE)
                    continue

                qualifying_posts += 1
                if cfg["dry_run"]:
                    continue

                outcome, kind_label = await self._extract_one(
                    venue_id, handle, post, cfg, trigger=TRIGGER_HANDLE_REEXTRACTION,
                )
                bump(outcome, kind_label)

        return qualifying_posts

    async def _extract_one(
        self, venue_id: str, handle: str, post: ArchivedPost, cfg: dict,
        *, trigger: str = TRIGGER_EXTRACTION,
    ) -> tuple[str, str]:
        """Returns (outcome, kind_label) — see KIND_LABEL_* above for what
        the second element means. `trigger` labels
        EVENT_EXTRACTION_SUPERSEDED_TOTAL when this call's own reconciliation
        supersedes a stale row of THIS post (plans/260811_extract-by-
        handle.md §Error Handling) — TRIGGER_EXTRACTION for the two
        pre-existing modes (unchanged behaviour), TRIGGER_HANDLE_REEXTRACTION
        when `_run_handles` is the caller."""
        # ALL rows already extracted from this post, not just one — a post
        # can hold several events now, so get_event_by_source (at most one
        # row) is unsafe here. Mirrors PromoterCrawlService._process_post.
        existing_events = self.venue_dao.list_events_by_source(handle, post.shortcode)
        already_superseded = {
            row["event_id"] for row in existing_events if row.get("status") == STATUS_SUPERSEDED
        }
        image_key = post.flyer_photo_key or post.any_photo_key
        image_data_uri = await self.post_source.image_data_uri(image_key)

        raw_text: Optional[str] = None
        try:
            raw_text, truncated = await self.openai_client.extract_events(
                caption=post.caption, image_data_uri=image_data_uri,
                max_events=self.max_events_per_post,
            )
        except Exception as e:
            logger.warning(
                f"[EventExtraction] extraction failed for {handle}/{post.shortcode}: {e}"
            )
            self._record_failure(venue_id, handle, post, raw_text, str(e), existing_events)
            return OUTCOME_EXTRACTION_FAILED, KIND_LABEL_NOT_APPLICABLE

        if truncated:
            # A truncated response means the output budget is too small — a
            # different fix from a model error — and persisting a half-parsed
            # list would be worse than persisting nothing. Existing rows for
            # this post are left completely untouched: a truncated run is not
            # evidence any of them disappeared.
            logger.warning(
                f"[EventExtraction] extraction truncated for {handle}/{post.shortcode}: "
                f"budget too small for up to {self.max_events_per_post} events; "
                f"persisting nothing"
            )
            return OUTCOME_TRUNCATED, KIND_LABEL_NOT_APPLICABLE

        try:
            events_data, malformed_count, malformed_attractions_count = (
                parse_multi_event_extraction_response(
                    raw_text, max_events=self.max_events_per_post,
                )
            )
        except EventExtractionParseError as e:
            logger.warning(
                f"[EventExtraction] extraction failed for {handle}/{post.shortcode}: {e}"
            )
            self._record_failure(venue_id, handle, post, raw_text, str(e), existing_events)
            return OUTCOME_EXTRACTION_FAILED, KIND_LABEL_NOT_APPLICABLE

        # See KIND_LABEL_* above: computed here, once, from the parsed
        # events this post actually yielded — never from the outcome label,
        # which does not carry kind information for every branch below.
        if len(events_data) == 1:
            kind_label = events_data[0].get("kind") or KIND_LABEL_UNKNOWN
        elif len(events_data) > 1:
            kind_label = KIND_LABEL_MIXED
        else:
            kind_label = KIND_LABEL_NOT_APPLICABLE

        if malformed_count:
            EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL.inc(malformed_count)
        if malformed_attractions_count:
            EVENT_EXTRACTION_MALFORMED_ATTRACTIONS_TOTAL.inc(malformed_attractions_count)

        post_timestamp = post.timestamp or self._now()
        now = self._now()

        prepared_events: list[dict] = []
        # Only meaningful (and only ever computed) when this post yields
        # exactly one event — the single-event case must report the SAME
        # granular outcome it always has (no_date/unread_time/low_confidence/
        # extracted). A genuinely multi-event post reports "extracted"
        # overall, like the promoter path already does: per-event nuance is
        # visible on each row's own review_reason instead.
        single_event_outcome: Optional[str] = None
        # plans/260810_post-kind-and-post-extraction-attribution.md §B, re-
        # scoped mid-execution: events, promotions and menus are separate
        # entities (the operator's own correction) — this feature does not
        # build promotion/menu entities, so a post classified as anything
        # other than `event` produces NO events.event row at all, only a
        # counted, logged outcome. The fail-toward-visible rule still
        # applies: a MISSING or UNRECOGNISED kind is never in
        # NON_EVENT_KINDS, so it falls through and is treated as an event,
        # same as before.
        skipped_non_event = 0

        kept_events: list[dict] = []
        for parsed in events_data:
            kind = parsed.get("kind")
            if kind in NON_EVENT_KINDS:
                skipped_non_event += 1
                logger.info(
                    f"[EventExtraction] non-event post skipped for "
                    f"{handle}/{post.shortcode}: kind={kind!r} "
                    f"title={parsed.get('title')!r} -- no event row created"
                )
                continue
            kept_events.append(parsed)

        # Each event resolves its OWN date independently, against the post's
        # timestamp — never a sibling's raw text. `vote_on_sibling_years`
        # (plans/260810_date-correctness-review-reasons-and-path-parity.md
        # §A) then looks across THIS post's own events only — one flyer
        # describes one programme — and pulls a lone rolled-forward outlier
        # back onto the year the rest of the post already agrees on, still
        # flagged as an inference either way. Scoped to this post's
        # `kept_events` alone; never called across posts.
        resolved_dates = [
            resolve_event_datetime(
                date_text=parsed["date_text"], time_text=parsed["time_text"],
                post_timestamp=post_timestamp,
                is_recurring=parsed["is_recurring"], recurrence_text=parsed["recurrence_text"],
            )
            for parsed in kept_events
        ]
        resolved_dates = vote_on_sibling_years(resolved_dates)

        for parsed, resolved in zip(kept_events, resolved_dates):
            # A time is an extraction MISS (worth an operator's eye) only
            # when the flyer itself said one was there and none was read; a
            # flyer that names no time, or a caption-only post with no flyer
            # attribute at all, is a genuinely date-only event and must NOT
            # be queued — queueing every date-only event would flood the
            # review queue with non-problems. `flyer_names_time` is None
            # both when no flyer was classified and when the attribute did
            # not clear the classifier's own confidence floor; either way
            # that is an absent signal, never read as a positive one.
            unread_time = (
                not resolved.needs_review
                and not resolved.time_known
                and post.flyer_names_time == "yes"
            )

            reasons: list[str] = []
            if resolved.review_reason:
                reasons.append(resolved.review_reason)
            if resolved.year_inferred:
                reasons.append(REASON_YEAR_INFERRED)
            if resolved.date_range:
                reasons.append(REASON_DATE_RANGE)
            if unread_time:
                reasons.append(REVIEW_REASON_UNREAD_TIME)
            low_confidence = parsed["confidence"] < cfg["min_confidence"]
            if low_confidence:
                reasons.append(REVIEW_REASON_LOW_CONFIDENCE)
            review_reason = "; ".join(reasons) if reasons else None

            # `time_known` rides in the existing raw_extraction JSONB blob
            # rather than a new column — a copy, not the model's own dict, so
            # the shared reconciliation's confirmed-divergence check still
            # compares the model's actual, unmodified answer.
            raw_extraction = dict(parsed)
            raw_extraction["time_known"] = resolved.time_known

            prepared_events.append({
                "starts_at": resolved.starts_at,
                "ends_at": resolved.ends_at,
                "is_recurring": resolved.is_recurring or bool(parsed["is_recurring"]),
                "recurrence_text": resolved.recurrence_text or parsed["recurrence_text"],
                "title": parsed["title"],
                "description": parsed["description"],
                "lineup": parsed["lineup"],
                "attractions": parsed["attractions"],
                "ticket_url": parsed["ticket_url"],
                "ticket_info": parsed["ticket_info"],
                "price_text": parsed["price_text"],
                "location_text": parsed["location_text"],
                "cover_photo_key": image_key,
                "confidence": parsed["confidence"],
                "review_reason": review_reason,
                "raw_extraction": raw_extraction,
            })

            if len(events_data) == 1:
                # Checked before the general `needs_review` branch: a weekday
                # mismatch also sets `needs_review=True`, but it is NOT a
                # missing date (starts_at is set, from the explicit date) and
                # must not be filed under the same outcome as a genuine blank
                # — that would erase the distinction the metric exists to
                # surface (plans/260807_date-resolution-correctness.md).
                if resolved.review_reason == REASON_WEEKDAY_MISMATCH:
                    single_event_outcome = OUTCOME_WEEKDAY_MISMATCH
                elif resolved.year_inferred:
                    single_event_outcome = OUTCOME_YEAR_INFERRED
                elif resolved.date_range:
                    single_event_outcome = OUTCOME_DATE_RANGE
                elif resolved.needs_review:
                    single_event_outcome = OUTCOME_NO_DATE
                elif unread_time:
                    single_event_outcome = OUTCOME_UNREAD_TIME
                elif low_confidence:
                    single_event_outcome = OUTCOME_LOW_CONFIDENCE
                else:
                    # No reason queued it, a venue post's event always has
                    # `venue_id` (the posting venue), and `not low_confidence`
                    # here means confidence already cleared `cfg[
                    # "min_confidence"]` — every clause of `is_clean_
                    # extraction` holds, so this event is auto-`accepted`.
                    single_event_outcome = OUTCOME_ACCEPTED

        # The ONE thing parameterised: a venue post's events are always
        # attributed to the POSTING venue — never the resolution ladder, and
        # a named `location_text` is recorded but never re-attributes the
        # event elsewhere (plans/260806_venue-post-multi-event.md §D). No
        # side effect to defer, unlike the promoter ladder — always None.
        def _attribute(fields: dict, event_id: str) -> tuple[dict, Optional[Callable[[], None]]]:
            return {"venue_id": venue_id}, None

        touched_event_ids: list[str] = []
        reconcile_post_events(
            venue_dao=self.venue_dao,
            source_kind=SOURCE_KIND_VENUE_POST,
            source_handle=handle,
            source_shortcode=post.shortcode,
            source_permalink=post.permalink,
            prepared_events=prepared_events,
            now=now,
            attribute=_attribute,
            touched_event_ids=touched_event_ids,
            min_confidence=cfg["min_confidence"],
        )
        # Recognise a countdown campaign — several posts announcing the SAME
        # night — the moment this post's own events are persisted, so a
        # later post never re-fragments an identity this or an earlier post
        # already established. See plans/260807_one-event-many-posts.md.
        if touched_event_ids:
            merge_touched_events(self.venue_dao, touched_event_ids, now)

        # plans/260811_extract-by-handle.md §Error Handling: count a row THIS
        # call's own reconciliation moved to superseded, labeled by what
        # triggered the call — never conflated with an ordinary run's own
        # supersessions, since a §B regression (a corrected date failing to
        # supersede its stale sibling) must be visible on its own label, not
        # hidden inside a shared total. Diffed against `already_superseded`
        # (captured before reconcile ran) rather than threading a return
        # value through `reconcile_post_events` — that module is shared with
        # the promoter path and its own contract is deliberately not touched
        # here.
        newly_superseded = [
            row["event_id"]
            for row in self.venue_dao.list_events_by_source(handle, post.shortcode)
            if row.get("status") == STATUS_SUPERSEDED
            and row["event_id"] not in already_superseded
        ]
        if newly_superseded:
            EVENT_EXTRACTION_SUPERSEDED_TOTAL.labels(trigger=trigger).inc(len(newly_superseded))

        if prepared_events:
            outcome = single_event_outcome if single_event_outcome is not None else OUTCOME_EXTRACTED
        elif skipped_non_event:
            # Every parsed event was a recognised non-event kind — nothing
            # persisted, by design (see the loop above).
            outcome = OUTCOME_NOT_AN_EVENT
        else:
            # events_data itself was empty ({"events": []}) — unchanged,
            # pre-existing behaviour for that edge case.
            outcome = OUTCOME_EXTRACTED
        return outcome, kind_label

    def _record_failure(
        self, venue_id: str, handle: str, post: ArchivedPost,
        raw_text: Optional[str], error_text: str, existing_events: list[dict],
    ) -> None:
        """A total extraction failure (API error, or a response so malformed
        even the top-level 'events' list cannot be read) — never even a
        partial persist. Mirrors PromoterCrawlService._record_failure: a
        confirmed row only ever gets raw_extraction/last_seen_at touched;
        every other existing row for this post is marked extraction_failed
        so the failure is visible without losing the post. A post with NO
        prior rows gets one placeholder row recording the failure."""
        now = self._now()
        raw_extraction = (
            {"raw_response": raw_text} if raw_text is not None else {"error": error_text}
        )
        if not existing_events:
            fields = {
                "venue_id": venue_id,
                "source_kind": SOURCE_KIND_VENUE_POST,
                "source_handle": handle,
                "source_shortcode": post.shortcode,
                "source_permalink": post.permalink,
                "status": STATUS_EXTRACTION_FAILED,
                "review_reason": REVIEW_REASON_EXTRACTION_FAILED,
                "raw_extraction": raw_extraction,
                "last_seen_at": now,
                "event_id": new_event_id(),
                "first_seen_at": now,
            }
            self.venue_dao.insert_event(fields)
            return

        for row in existing_events:
            if row.get("status") == STATUS_CONFIRMED:
                # Not even a failed re-extraction reverts a confirmed record.
                self.venue_dao.update_event(row["event_id"], {
                    "raw_extraction": raw_extraction, "last_seen_at": now,
                    # Identifies which of this (possibly multi-source, post-
                    # merge) event's sources owns this refresh — see
                    # plans/260807_one-event-many-posts.md.
                    "source_handle": handle, "source_shortcode": post.shortcode,
                })
            else:
                self.venue_dao.update_event(row["event_id"], {
                    "status": STATUS_EXTRACTION_FAILED,
                    "review_reason": REVIEW_REASON_EXTRACTION_FAILED,
                    "raw_extraction": raw_extraction, "last_seen_at": now,
                    "source_handle": handle, "source_shortcode": post.shortcode,
                })


__all__ = [
    "ArchivedPost", "EventPostSource", "EventExtractionService",
    "InvalidEventExtractionConfig", "parse_event_extraction_config",
    "post_qualifies", "new_event_id",
    "OUTCOME_EXTRACTED", "OUTCOME_NOT_EVENT_LIKE", "OUTCOME_NO_DATE",
    "OUTCOME_LOW_CONFIDENCE", "OUTCOME_EXTRACTION_FAILED", "OUTCOME_SKIPPED_SEEN",
    "OUTCOME_UNREAD_TIME", "OUTCOME_TRUNCATED", "OUTCOME_WEEKDAY_MISMATCH",
    "OUTCOME_YEAR_INFERRED", "OUTCOME_DATE_RANGE",
    "OUTCOME_ACCEPTED", "OUTCOME_NOT_AN_EVENT",
    "REVIEW_REASON_UNREAD_TIME", "DEFAULT_MAX_EVENTS_PER_POST", "ALL_STATUSES",
    "KIND_LABEL_NOT_APPLICABLE", "KIND_LABEL_UNKNOWN", "KIND_LABEL_MIXED",
    "TRIGGER_EXTRACTION", "TRIGGER_HANDLE_REEXTRACTION",
    "HANDLE_OUTCOME_EXTRACTED", "HANDLE_OUTCOME_NOTHING_ARCHIVED",
    "HANDLE_OUTCOME_NO_VENUE_MAPPED", "HANDLE_OUTCOME_AMBIGUOUS_VENUES",
]
