"""Turn archived Instagram posts of event-candidate venues into structured
events. See plans/260804_instagram-event-extraction.md.

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

Dates are resolved by app/services/event_date_resolver.py, a deterministic,
independently unit-tested function — never trusted to the model, and never
computed against the run clock (only the post's own timestamp).

A `confirmed` event is the operator's word and is never reverted by a
re-extraction (§D of the plan): only `raw_extraction`/`last_seen_at` change,
and a divergence from the model's new answer is flagged, not applied.

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
from typing import Optional

from app.api.openai_event_extraction_client import (
    EventExtractionParseError,
    parse_extraction_response,
)
from app.metrics import EVENT_EXTRACTION_POSTS_TOTAL, EVENTS_TOTAL
from app.models.photo_taxonomy import CATEGORY_FLYER
from app.services.event_caption_matcher import matches_event_marker
from app.services.event_date_resolver import resolve_event_datetime
from app.services.event_venue_targeting import (
    _parse_timestamp,
    _run_prefix_date,
    resolve_event_candidate_ids,
)
from app.services.pipeline_run_registry import new_run_id

logger = logging.getLogger(__name__)

SOURCE_KIND_VENUE_POST = "venue_post"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_CONFIRMED = "confirmed"
STATUS_EXTRACTION_FAILED = "extraction_failed"

REVIEW_REASON_LOW_CONFIDENCE = "low_confidence"
REVIEW_REASON_EXTRACTION_FAILED = "extraction_failed"
REVIEW_REASON_DIVERGES_FROM_CONFIRMED = "model_diverges_from_confirmed_record"

OUTCOME_EXTRACTED = "extracted"
OUTCOME_NOT_EVENT_LIKE = "not_event_like"
OUTCOME_NO_DATE = "no_date"
OUTCOME_LOW_CONFIDENCE = "low_confidence"
OUTCOME_EXTRACTION_FAILED = "extraction_failed"
OUTCOME_SKIPPED_SEEN = "skipped_seen"
ALL_STATUSES = (
    "pending_review", "confirmed", "rejected", "superseded", "extraction_failed",
)

DEFAULT_MAX_VENUES = 25
DEFAULT_MAX_POSTS_PER_VENUE = 20
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_FLYER_CONFIDENCE_FLOOR = 0.6


def new_event_id() -> str:
    """A ULID: same time-ordered rationale as the archive run id — see
    app/services/pipeline_run_registry.new_run_id."""
    return f"evt_{new_run_id()}"


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


class EventPostSource:
    """Groups archived `instagram_posts` manifest photo entries by shortcode
    into per-post records, within a lookback window. See the module
    docstring for why this walks the same manifests
    ArchivedFlyerEvidenceSource does rather than a new mechanism.
    """

    def __init__(self, media_store, archive_source: str = "instagram_posts"):
        self.media_store = media_store
        self.archive_source = archive_source

    async def posts_for_venue(self, venue_id: str, since: datetime) -> list[ArchivedPost]:
        if self.media_store is None:
            return []
        try:
            prefixes = await self.media_store.list_run_prefixes(self.archive_source)
        except Exception as e:
            logger.warning(f"[EventExtraction] listing archive runs failed: {e}")
            return []

        grouped: dict[str, dict] = {}
        for prefix in prefixes:
            run_date = _run_prefix_date(prefix)
            if run_date is not None and run_date < since:
                continue
            try:
                manifest = await self.media_store.read_manifest(prefix, venue_id)
            except Exception as e:
                logger.warning(f"[EventExtraction] manifest read failed: {e}")
                continue
            if not manifest:
                continue
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
                    "any_photo_key": None,
                })
                if bucket["any_photo_key"] is None:
                    bucket["any_photo_key"] = entry.get("key")
                if entry.get("category") == CATEGORY_FLYER:
                    confidence = entry.get("classification_confidence")
                    best_so_far = bucket["flyer_confidence"] or 0.0
                    if bucket["flyer_photo_key"] is None or (confidence or 0.0) > best_so_far:
                        bucket["flyer_photo_key"] = entry.get("key")
                        bucket["flyer_confidence"] = confidence

        posts = []
        for bucket in grouped.values():
            posts.append(ArchivedPost(
                shortcode=bucket["shortcode"],
                permalink=bucket["permalink"],
                caption=bucket["caption"],
                timestamp=_parse_timestamp(bucket["timestamp"]),
                flyer_photo_key=bucket["flyer_photo_key"],
                flyer_confidence=bucket["flyer_confidence"],
                any_photo_key=bucket["any_photo_key"],
            ))
        return posts

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
    if mode not in ("event_candidates", "venue_ids"):
        raise InvalidEventExtractionConfig(f"unknown eligibility mode: {mode!r}")
    venue_ids_raw = str(eligibility.get("venue_ids") or "").strip()
    venue_ids = [v.strip() for v in venue_ids_raw.split(",") if v.strip()]

    return {
        "eligibility_mode": mode,
        "eligibility_venue_ids": venue_ids,
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
        now_provider=None,
    ):
        self.venue_dao = venue_dao
        self.post_source = post_source
        self.openai_client = openai_client
        self.min_confidence = min_confidence
        self.flyer_confidence_floor = flyer_confidence_floor
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
        venue_ids = self._resolve_venue_ids(cfg)

        outcome_counts: dict[str, int] = {}

        def _bump(outcome: str) -> None:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            EVENT_EXTRACTION_POSTS_TOTAL.labels(outcome=outcome).inc()

        qualifying_posts = 0
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

                outcome = await self._extract_one(venue_id, handle, post, cfg)
                _bump(outcome)

        if not cfg["dry_run"]:
            self._update_events_gauge()

        return {
            "qualifying_posts": qualifying_posts,
            "outcomes": outcome_counts,
            "dry_run": cfg["dry_run"],
            # Never asserted as fact: measure a real run before trusting this.
            "estimated_cost_usd": None,
        }

    async def _extract_one(self, venue_id: str, handle: str, post: ArchivedPost, cfg: dict) -> str:
        existing = self.venue_dao.get_event_by_source(handle, post.shortcode)
        image_key = post.flyer_photo_key or post.any_photo_key
        image_data_uri = await self.post_source.image_data_uri(image_key)

        raw_text: Optional[str] = None
        try:
            raw_text = await self.openai_client.extract(
                caption=post.caption, image_data_uri=image_data_uri,
            )
            parsed = parse_extraction_response(raw_text)
        except Exception as e:
            logger.warning(
                f"[EventExtraction] extraction failed for {handle}/{post.shortcode}: {e}"
            )
            self._record_failure(venue_id, handle, post, raw_text, str(e), existing)
            return OUTCOME_EXTRACTION_FAILED

        post_timestamp = post.timestamp or self._now()
        resolved = resolve_event_datetime(
            date_text=parsed["date_text"], time_text=parsed["time_text"],
            post_timestamp=post_timestamp,
        )

        reasons: list[str] = []
        if resolved.needs_review:
            reasons.append(resolved.review_reason)
        low_confidence = parsed["confidence"] < cfg["min_confidence"]
        if low_confidence:
            reasons.append(REVIEW_REASON_LOW_CONFIDENCE)
        review_reason = "; ".join(reasons) if reasons else None

        now = self._now()
        fields = {
            "venue_id": venue_id,
            "source_kind": SOURCE_KIND_VENUE_POST,
            "source_handle": handle,
            "source_shortcode": post.shortcode,
            "source_permalink": post.permalink,
            "starts_at": resolved.starts_at,
            "ends_at": resolved.ends_at,
            "is_recurring": resolved.is_recurring or bool(parsed["is_recurring"]),
            "recurrence_text": resolved.recurrence_text or parsed["recurrence_text"],
            "title": parsed["title"],
            "description": parsed["description"],
            "lineup": parsed["lineup"],
            "ticket_url": parsed["ticket_url"],
            "price_text": parsed["price_text"],
            "location_text": parsed["location_text"],
            "cover_photo_key": image_key,
            "confidence": parsed["confidence"],
            "status": STATUS_PENDING_REVIEW,
            "review_reason": review_reason,
            "raw_extraction": parsed,
            "last_seen_at": now,
        }

        if existing is None:
            fields["event_id"] = new_event_id()
            fields["first_seen_at"] = now
            self.venue_dao.insert_event(fields)
        elif existing.get("status") == STATUS_CONFIRMED:
            self._preserve_confirmed(existing, fields, parsed)
        else:
            self.venue_dao.update_event(existing["event_id"], fields)

        if resolved.needs_review:
            return OUTCOME_NO_DATE
        if low_confidence:
            return OUTCOME_LOW_CONFIDENCE
        return OUTCOME_EXTRACTED

    def _preserve_confirmed(self, existing: dict, fields: dict, parsed: dict) -> None:
        """The operator outranks the model (plan requirement): a `confirmed`
        event is never reverted by re-extraction. Only raw_extraction and
        last_seen_at move; every field the operator could have corrected is
        left untouched. A divergence is flagged via review_reason WITHOUT
        moving status away from confirmed.
        """
        update_fields = {
            "raw_extraction": parsed,
            "last_seen_at": fields["last_seen_at"],
        }
        title_diverges = (parsed.get("title") or None) != (existing.get("title") or None)
        date_diverges = fields["starts_at"] != existing.get("starts_at")
        if title_diverges or date_diverges:
            update_fields["review_reason"] = REVIEW_REASON_DIVERGES_FROM_CONFIRMED
        self.venue_dao.update_event(existing["event_id"], update_fields)

    def _record_failure(
        self, venue_id: str, handle: str, post: ArchivedPost,
        raw_text: Optional[str], error_text: str, existing: Optional[dict],
    ) -> None:
        now = self._now()
        raw_extraction = (
            {"raw_response": raw_text} if raw_text is not None else {"error": error_text}
        )
        if existing is not None and existing.get("status") == STATUS_CONFIRMED:
            # Not even a failed re-extraction reverts a confirmed record.
            self.venue_dao.update_event(existing["event_id"], {
                "raw_extraction": raw_extraction, "last_seen_at": now,
            })
            return

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
        }
        if existing is None:
            fields["event_id"] = new_event_id()
            fields["first_seen_at"] = now
            self.venue_dao.insert_event(fields)
        else:
            self.venue_dao.update_event(existing["event_id"], fields)

    def _update_events_gauge(self) -> None:
        counts = {status: 0 for status in ALL_STATUSES}
        for row in self.venue_dao.list_events():
            status = row.get("status")
            counts[status] = counts.get(status, 0) + 1
        for status, count in counts.items():
            EVENTS_TOTAL.labels(status=status).set(count)


__all__ = [
    "ArchivedPost", "EventPostSource", "EventExtractionService",
    "InvalidEventExtractionConfig", "parse_event_extraction_config",
    "post_qualifies", "new_event_id",
    "OUTCOME_EXTRACTED", "OUTCOME_NOT_EVENT_LIKE", "OUTCOME_NO_DATE",
    "OUTCOME_LOW_CONFIDENCE", "OUTCOME_EXTRACTION_FAILED", "OUTCOME_SKIPPED_SEEN",
]
