"""Crawl active promoter accounts, extract their events with the existing
extractor, and resolve each event's venue through the resolution ladder. See
plans/260804_instagram-promoter-events.md §C-D and
plans/260806_multi-event-posts.md (one post can announce SEVERAL events).

Reuses, unchanged, every piece `260804_instagram-event-extraction.md` already
built: `resolve_event_datetime` (dates are never trusted to the model),
`matches_event_marker` (the free cost-gate — a post is worth a model call
only when its caption smells like an event; no photo classifier runs for
promoter posts, so a flyer-photo qualification never applies here). This
module adds the promoter POST SOURCE and the venue RESOLUTION, nothing else.
The resolution ladder itself does not change for multi-event posts either —
each event just runs it independently, with its OWN location_text.

A promoter account is crawled only when `status == 'active'` — the one-way
gate that makes `PromoterRegistryService.run_discovery` safe to run
unattended: a discovered candidate sits in the registry costing storage,
never crawled, until an operator flips it.

A manual link (`location_resolution == 'manual'`) is never overwritten by a
later crawl of the same post: the operator outranks the model, the same
principle `EventExtractionService` applies to a `confirmed` event. With
several events per post, both protections now apply PER EVENT (matched by
its content-derived `source_event_key`, app.services.event_identity), not
per post — a confirmed or manually-linked sibling must never be affected by
what happens to the other events the same post announces.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.api.openai_event_extraction_client import (
    EventExtractionParseError,
    parse_multi_event_extraction_response,
)
from app.metrics import (
    EVENT_EXTRACTION_EVENTS_PER_POST,
    EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL,
    EVENT_REVIEW_QUEUE_DEPTH,
    EVENT_VENUE_LINK_TOTAL,
    PROMOTER_CRAWL_POSTS_TOTAL,
)
from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
from app.services.event_caption_matcher import matches_event_marker
from app.services.event_date_resolver import REASON_MISSING_DATE, resolve_event_datetime
from app.services.event_extraction_service import new_event_id
from app.services.event_identity import compute_source_event_key
from app.services.event_venue_resolution import (
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_MARGIN,
    RESOLUTION_AUTO,
    RESOLUTION_MANUAL,
    RESOLUTION_QUEUED,
    RESOLUTION_UNRESOLVED,
    build_handle_index,
    build_venue_catalog,
    resolve_event_venue,
)
from app.services.event_venue_targeting import _parse_timestamp
from app.services.instagram_handle_sources import normalize_handle
from app.services.promoter_registry_service import STATUS_ACTIVE
from app.services.venue_photo_archive_service import new_run_id, run_prefix

logger = logging.getLogger(__name__)

SOURCE_KIND_PROMOTER_POST = "promoter_post"

STATUS_PENDING_REVIEW = "pending_review"
STATUS_EXTRACTION_FAILED = "extraction_failed"
STATUS_CONFIRMED = "confirmed"
# Already defined (and unused) by 0023_event_table's status vocabulary —
# plans/260806_multi-event-posts.md is the first thing that writes it: an
# event a later extraction no longer finds is superseded, never
# hard-deleted, and never touched at all once confirmed or manually linked.
STATUS_SUPERSEDED = "superseded"
REVIEW_REASON_LOW_CONFIDENCE = "low_confidence"
REVIEW_REASON_EXTRACTION_FAILED = "extraction_failed"

OUTCOME_ARCHIVED = "archived"
OUTCOME_NOT_EVENT_LIKE = "not_event_like"
OUTCOME_EXTRACTED = "extracted"
OUTCOME_EXTRACTION_FAILED = "extraction_failed"
OUTCOME_ACCOUNT_UNAVAILABLE = "account_unavailable"
# A truncated response is a DIFFERENT fix from a model error (the budget is
# too small, not a bad answer) — kept as its own outcome, never folded into
# extraction_failed. Nothing is persisted for a truncated post.
OUTCOME_TRUNCATED = "truncated"

DEFAULT_MAX_ACCOUNTS = 10
# NOT optional (plan): a promoter posts far more than a venue does. Small on
# purpose — see app.config.settings.promoter_max_posts_per_account.
DEFAULT_MAX_POSTS_PER_ACCOUNT = 15
DEFAULT_LOOKBACK_DAYS = 60
# Sanity bound on events per post — see
# app.config.settings.event_extraction_max_events_per_post, which the output
# token budget also scales from.
DEFAULT_MAX_EVENTS_PER_POST = 20

_RESULT_LABEL = {
    RESOLUTION_AUTO: "auto",
    RESOLUTION_QUEUED: "queued",
    RESOLUTION_UNRESOLVED: "unresolved",
}


class InvalidPromoterCrawlConfig(ValueError):
    pass


class PromoterAccountUnavailable(Exception):
    """The account no longer exists or is private. Raised by the posts
    client so the crawl can mark it and move on — one dead handle must not
    cost the other accounts their crawl."""


class ApifyPromoterPostsClient:
    """Thin adapter over `ApifyInstagramClient.fetch_recent_posts` for
    promoter accounts.

    Detecting a private/missing account from Apify's own response is
    UNVERIFIED against live data (see the plan's Open Questions):
    `fetch_recent_posts` already drops any per-item `error` entry before
    this adapter ever sees a result, and an account with zero recent posts
    is a legitimate empty crawl, not evidence of unavailability. So this
    adapter never raises `PromoterAccountUnavailable` today — the
    skip-and-continue path it exists to trigger is proven at the
    orchestration level (BDD, with a fake posts client) and must be
    re-armed here once a live crawl shows what an unavailable account's
    payload actually looks like.
    """

    def __init__(self, apify_client):
        self.apify_client = apify_client

    async def fetch_recent_posts(self, handle: str, *, results_limit: int) -> list[dict]:
        return await self.apify_client.fetch_recent_posts(handle, results_limit=results_limit)


def parse_promoter_crawl_config(
    config: Optional[dict], *, default_max_posts_per_account: int,
) -> dict:
    cfg = dict(config or {})

    def _non_negative_int(value, field: str, default: int) -> int:
        if value in (None, ""):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise InvalidPromoterCrawlConfig(f"{field} must be an integer, got {value!r}")
        if parsed < 0:
            raise InvalidPromoterCrawlConfig(f"{field} must be >= 0, got {parsed}")
        return parsed

    handles_raw = cfg.get("handles")
    if isinstance(handles_raw, str):
        handles = [h.strip() for h in handles_raw.split(",") if h.strip()]
    else:
        handles = [str(h).strip() for h in (handles_raw or []) if str(h).strip()]

    # The per-account bound is NOT optional: 0 (or omitted) falls back to the
    # configured default rather than meaning "no cap" — a promoter posts far
    # more than a venue, and this is the guarantee that must never disappear
    # behind a permissive config value.
    max_posts = _non_negative_int(
        cfg.get("max_posts_per_account"), "max_posts_per_account", default_max_posts_per_account,
    ) or default_max_posts_per_account

    return {
        "handles": handles,
        "max_accounts": _non_negative_int(
            cfg.get("max_accounts"), "max_accounts", DEFAULT_MAX_ACCOUNTS,
        ),
        "max_posts_per_account": max_posts,
        "lookback_days": _non_negative_int(
            cfg.get("lookback_days"), "lookback_days", DEFAULT_LOOKBACK_DAYS,
        ),
        "dry_run": bool(cfg.get("dry_run")),
    }


class PromoterCrawlService:
    def __init__(
        self,
        *,
        venue_dao,
        posts_client,
        media_store=None,
        downloader=None,
        openai_client=None,
        archive_source: str = SOURCE_INSTAGRAM_POSTS,
        max_posts_per_account_default: int = DEFAULT_MAX_POSTS_PER_ACCOUNT,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
        margin: float = DEFAULT_MARGIN,
        min_confidence: float = 0.5,
        max_events_per_post: int = DEFAULT_MAX_EVENTS_PER_POST,
        now_provider=None,
    ):
        self.venue_dao = venue_dao
        self.posts_client = posts_client
        self.media_store = media_store
        self.downloader = downloader
        self.openai_client = openai_client
        self.archive_source = archive_source
        self.max_posts_per_account_default = max_posts_per_account_default
        self.confidence_floor = confidence_floor
        self.margin = margin
        self.min_confidence = min_confidence
        self.max_events_per_post = max_events_per_post
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        # In-memory diagnostic record of truncated responses this instance
        # has seen — nothing is persisted to events.event for a truncated
        # post (plan requirement: no partial event), but the raw text must
        # still be "kept" for an operator to see why nothing was written.
        self.last_truncated: list[dict] = []

    # ── account selection ────────────────────────────────────────────────────
    def _select_accounts(self, cfg: dict) -> list[dict]:
        """Empty `handles` means every `active` account. An explicit handle
        list is filtered to `active` too — a candidate or paused account
        named directly must not be crawlable by working around the empty-
        list default."""
        if cfg["handles"]:
            out = []
            for raw in cfg["handles"]:
                handle = normalize_handle(raw)
                account = self.venue_dao.get_promoter_account(handle) if handle else None
                if account is not None and account.get("status") == STATUS_ACTIVE:
                    out.append(account)
            return out
        return [
            a for a in (self.venue_dao.list_promoter_accounts() or [])
            if a.get("status") == STATUS_ACTIVE
        ]

    def _within_lookback(self, post: dict, since: datetime) -> bool:
        ts = _parse_timestamp(post.get("timestamp"))
        # A post with no parseable timestamp is kept — the caption is cheap
        # evidence either way and must not be silently dropped for a data
        # gap, mirroring event_venue_targeting.caption_evidence.
        return ts is None or ts >= since

    # ── the run ──────────────────────────────────────────────────────────────
    async def run(self, config: Optional[dict] = None) -> dict:
        cfg = parse_promoter_crawl_config(
            config, default_max_posts_per_account=self.max_posts_per_account_default,
        )
        now = self._now()
        since = now - timedelta(days=cfg["lookback_days"])

        accounts = self._select_accounts(cfg)
        if cfg["max_accounts"]:
            accounts = accounts[: cfg["max_accounts"]]

        venues = build_venue_catalog(self.venue_dao)
        handle_index = build_handle_index(self.venue_dao)

        prefix = None
        if not cfg["dry_run"] and self.media_store is not None:
            prefix = run_prefix(self.archive_source, now, new_run_id(now))

        report: dict = {"dry_run": cfg["dry_run"], "accounts": []}

        for account in accounts:
            handle = account["handle"]
            try:
                posts = await self.posts_client.fetch_recent_posts(
                    handle, results_limit=cfg["max_posts_per_account"],
                )
            except PromoterAccountUnavailable as e:
                PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_ACCOUNT_UNAVAILABLE).inc()
                self.venue_dao.upsert_promoter_account(handle, {
                    "notes": f"unavailable as of {now.isoformat()}: {e}",
                    "last_crawled_at": now,
                })
                report["accounts"].append({
                    "handle": handle, "outcome": "unavailable", "posts_crawled": 0,
                })
                logger.warning(f"[PromoterCrawl] account unavailable, skipped: {handle}: {e}")
                continue

            posts = [
                p for p in (posts or []) if self._within_lookback(p, since)
            ][: cfg["max_posts_per_account"]]
            report["accounts"].append({
                "handle": handle, "outcome": "crawled", "posts_crawled": len(posts),
            })

            if cfg["dry_run"]:
                continue

            manifest_entries: list[dict] = []
            extracted = 0
            for post in posts:
                PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_ARCHIVED).inc()
                if prefix is not None:
                    manifest_entries.extend(
                        await self._archive_post_images(prefix, handle, post)
                    )
                extracted += await self._process_post(
                    handle=handle, post=post, venues=venues, handle_index=handle_index,
                    now=now,
                )

            if prefix is not None and manifest_entries:
                try:
                    await self.media_store.put_promoter_manifest(
                        prefix=prefix, handle=handle,
                        manifest={"handle": handle, "photos": manifest_entries},
                    )
                except Exception as e:
                    logger.error(f"[PromoterCrawl] manifest write failed for {handle}: {e}")

            self.venue_dao.upsert_promoter_account(handle, {
                "last_crawled_at": now,
                "posts_crawled": int(account.get("posts_crawled") or 0) + len(posts),
                "events_extracted": int(account.get("events_extracted") or 0) + extracted,
            })

        if not cfg["dry_run"]:
            self._update_review_queue_gauge()
        return report

    # ── archiving (promoter=<handle>, never venue_id=) ──────────────────────
    async def _archive_post_images(self, prefix: str, handle: str, post: dict) -> list[dict]:
        if self.downloader is None or self.media_store is None:
            return []
        shortcode = post.get("shortcode")
        urls = post.get("image_urls") or []
        entries: list[dict] = []
        for idx, url in enumerate(urls, start=1):
            try:
                data, content_type = await self.downloader.download(url)
                photo_id = f"{shortcode}_{idx}" if len(urls) > 1 else (shortcode or f"post_{idx}")
                key = await self.media_store.put_promoter_image(
                    prefix=prefix, handle=handle, photo_id=photo_id,
                    data=data, content_type=content_type,
                )
            except Exception as e:
                logger.warning(
                    f"[PromoterCrawl] image archive failed for {handle}/{shortcode}: {e}"
                )
                continue
            entries.append({
                "key": key, "shortcode": shortcode, "caption": post.get("caption"),
                "permalink": post.get("permalink"), "uploaded_at": post.get("timestamp"),
                "location_tag": post.get("location_tag"),
            })
        return entries

    # ── extraction + resolution for one post (may yield several events) ─────
    async def _process_post(
        self, *, handle: str, post: dict, venues: list, handle_index: dict, now: datetime,
    ) -> int:
        """Returns the number of events persisted (0 on any early exit) —
        the caller sums this into `events_extracted`, which is now an
        accurate per-event tally rather than a per-post flag."""
        shortcode = post.get("shortcode")
        caption = post.get("caption")
        if not shortcode:
            return 0

        # ALL rows already extracted from this post, not just one — a post
        # can hold several events (plans/260806_multi-event-posts.md), so
        # get_event_by_source (at most one row) is unsafe here.
        existing_events = self.venue_dao.list_events_by_source(handle, shortcode)

        if not matches_event_marker(caption):
            PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_NOT_EVENT_LIKE).inc()
            return 0

        if self.openai_client is None:
            return 0

        image_data_uri = await self._first_image_data_uri(post)
        raw_text: Optional[str] = None
        try:
            raw_text, truncated = await self.openai_client.extract_events(
                caption=caption, image_data_uri=image_data_uri,
                max_events=self.max_events_per_post,
            )
        except Exception as e:
            logger.warning(f"[PromoterCrawl] extraction failed for {handle}/{shortcode}: {e}")
            self._record_failure(handle, shortcode, post, raw_text, str(e), existing_events, now)
            PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_EXTRACTION_FAILED).inc()
            return 0

        if truncated:
            # A truncated response means the budget is too small — a
            # different fix from a model error (extraction_failed) — and
            # persisting a half-parsed list would be worse than persisting
            # nothing: a batch already lost to a flat token budget once
            # (docs/venue-retrieval-storage.md §4) reported SUCCESS. Existing
            # rows for this post are left completely untouched: a truncated
            # run is not evidence any of them disappeared.
            logger.warning(
                f"[PromoterCrawl] extraction truncated for {handle}/{shortcode}: "
                f"budget too small for up to {self.max_events_per_post} events; "
                f"persisting nothing"
            )
            self.last_truncated.append({
                "handle": handle, "shortcode": shortcode, "raw_response": raw_text,
            })
            PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_TRUNCATED).inc()
            return 0

        try:
            events_data, malformed_count = parse_multi_event_extraction_response(
                raw_text, max_events=self.max_events_per_post,
            )
        except EventExtractionParseError as e:
            logger.warning(f"[PromoterCrawl] extraction failed for {handle}/{shortcode}: {e}")
            self._record_failure(handle, shortcode, post, raw_text, str(e), existing_events, now)
            PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_EXTRACTION_FAILED).inc()
            return 0

        if malformed_count:
            EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL.inc(malformed_count)

        post_ts = _parse_timestamp(post.get("timestamp")) or now
        existing_by_key = {
            row["source_event_key"]: row for row in existing_events
            if row.get("source_event_key") is not None
        }
        seen_keys: set[str] = set()
        persisted = 0

        for index, parsed in enumerate(events_data, start=1):
            # Each event resolves its OWN date, independently, against the
            # post's timestamp — never a sibling's.
            resolved_date = resolve_event_datetime(
                date_text=parsed["date_text"], time_text=parsed["time_text"],
                post_timestamp=post_ts,
            )
            source_event_key = compute_source_event_key(parsed["title"], resolved_date.starts_at)
            seen_keys.add(source_event_key)
            existing = existing_by_key.get(source_event_key)

            # A `confirmed` event is the operator's word: only raw_extraction
            # and last_seen_at move, mirroring
            # EventExtractionService._preserve_confirmed. This now applies
            # PER EVENT — a confirmed sibling in the same post must not gate
            # or alter what happens to the others.
            if existing is not None and existing.get("status") == STATUS_CONFIRMED:
                self.venue_dao.update_event(existing["event_id"], {
                    "raw_extraction": parsed, "last_seen_at": now,
                })
                persisted += 1
                continue

            # A manual link outranks the model: a later crawl of the same
            # post must never move venue_id/location_resolution/confidence/
            # linked_* once an operator has set them for THIS event.
            preserve_manual_link = (
                existing is not None and existing.get("location_resolution") == RESOLUTION_MANUAL
            )
            # An event with no location_text resolves as unresolved and must
            # NEVER inherit a sibling's venue — "the other event in this
            # post was at the Cinema" is not evidence about this one. Passing
            # each event's OWN location_text (never a sibling's, never a
            # merged one) into the SAME unchanged ladder is what guarantees
            # this: resolve_event_venue already returns RESOLUTION_UNRESOLVED
            # when location_text is absent (no name-match candidates), and
            # rungs 1-2 read the shared caption/tag, which no per-event data
            # can leak between siblings.
            resolution = None
            if not preserve_manual_link:
                resolution = resolve_event_venue(
                    caption=caption, location_text=parsed["location_text"],
                    location_tag=post.get("location_tag"), promoter_handle=handle,
                    venues=venues, handle_index=handle_index,
                    confidence_floor=self.confidence_floor, margin=self.margin,
                )

            review_reason = None
            if resolved_date.needs_review:
                review_reason = REASON_MISSING_DATE
            elif parsed["confidence"] < self.min_confidence:
                review_reason = REVIEW_REASON_LOW_CONFIDENCE

            fields = {
                "source_kind": SOURCE_KIND_PROMOTER_POST,
                "source_handle": handle, "source_shortcode": shortcode,
                "source_permalink": post.get("permalink"),
                "source_event_key": source_event_key, "source_event_index": index,
                "starts_at": resolved_date.starts_at, "ends_at": resolved_date.ends_at,
                "is_recurring": resolved_date.is_recurring or bool(parsed["is_recurring"]),
                "recurrence_text": resolved_date.recurrence_text or parsed["recurrence_text"],
                "title": parsed["title"], "description": parsed["description"],
                "lineup": parsed["lineup"], "ticket_url": parsed["ticket_url"],
                "price_text": parsed["price_text"], "location_text": parsed["location_text"],
                "confidence": parsed["confidence"],
                "status": STATUS_PENDING_REVIEW,
                "review_reason": review_reason,
                "raw_extraction": parsed,
                "last_seen_at": now,
            }

            if resolution is not None:
                if resolution.resolution == RESOLUTION_AUTO:
                    fields.update({
                        "venue_id": resolution.venue_id,
                        "location_resolution": RESOLUTION_AUTO,
                        "location_confidence": resolution.confidence,
                        "linked_by": resolution.method,
                        "linked_at": now,
                    })
                elif resolution.resolution == RESOLUTION_UNRESOLVED:
                    fields.update({
                        "venue_id": None,
                        "location_resolution": RESOLUTION_UNRESOLVED,
                        "location_confidence": None,
                        "linked_by": None,
                        "linked_at": None,
                    })
                # RESOLUTION_QUEUED: the four link columns are left out of
                # `fields` entirely — update_event's partial-update contract
                # means they stay NULL, the "awaiting review" state.

            if existing is None:
                fields["event_id"] = new_event_id()
                fields["first_seen_at"] = now
                # A fresh row has no prior value for the four link columns to
                # fall back on — unlike an update, where omitting them from
                # `fields` correctly leaves whatever was already stored (NULL,
                # from that same row's own first insert) untouched. Without this,
                # a brand-new RESOLUTION_QUEUED event would insert with venue_id
                # simply ABSENT rather than explicitly NULL — the real store's
                # column default covers that, but the in-memory fake does not.
                fields.setdefault("venue_id", None)
                fields.setdefault("location_resolution", None)
                fields.setdefault("location_confidence", None)
                fields.setdefault("linked_by", None)
                fields.setdefault("linked_at", None)
                self.venue_dao.insert_event(fields)
                event_id = fields["event_id"]
            else:
                self.venue_dao.update_event(existing["event_id"], fields)
                event_id = existing["event_id"]

            if resolution is not None and resolution.candidates:
                self.venue_dao.replace_event_venue_link_candidates(event_id, [
                    {
                        "venue_id": c.venue_id, "rank": rank, "score": c.score,
                        "method": c.method, "evidence": c.evidence,
                    }
                    for rank, c in enumerate(resolution.candidates, start=1)
                ])

            if resolution is not None:
                EVENT_VENUE_LINK_TOTAL.labels(
                    method=resolution.method or "none",
                    result=_RESULT_LABEL[resolution.resolution],
                ).inc()

            persisted += 1

        # An event previously extracted from this post and absent from THIS
        # run is superseded — never hard-deleted, and never touched at all
        # once confirmed or manually linked (the operator outranks the
        # model, exactly as the confirmed branch above already behaves).
        for row in existing_events:
            key = row.get("source_event_key")
            if key is None or key in seen_keys:
                continue
            if row.get("status") == STATUS_CONFIRMED:
                continue
            if row.get("location_resolution") == RESOLUTION_MANUAL:
                continue
            self.venue_dao.update_event(row["event_id"], {"status": STATUS_SUPERSEDED})

        # A listings account collapsing back to one event per post is
        # exactly the regression this feature exists to prevent — visible
        # only in this distribution, never in a single scalar.
        EVENT_EXTRACTION_EVENTS_PER_POST.observe(len(events_data))
        PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_EXTRACTED).inc()
        return persisted

    async def _first_image_data_uri(self, post: dict) -> Optional[str]:
        if self.downloader is None:
            return None
        image_url = next(iter(post.get("image_urls") or []), None)
        if not image_url:
            return None
        try:
            data, content_type = await self.downloader.download(image_url)
        except Exception as e:
            logger.warning(f"[PromoterCrawl] extraction image download failed: {e}")
            return None
        return f"data:{content_type};base64,{base64.b64encode(data).decode()}"

    def _record_failure(
        self, handle: str, shortcode: str, post: dict,
        raw_text: Optional[str], error_text: str, existing_events: list[dict], now: datetime,
    ) -> None:
        """A total extraction failure (API error, or a response so malformed
        even the top-level 'events' list cannot be read) — never even a
        partial persist. Mirrors the single-event contract per existing row:
        a confirmed row only ever gets raw_extraction/last_seen_at touched
        (not even a failed re-extraction reverts it); every other existing
        row for this post is marked extraction_failed so the failure is
        visible without losing the post. A post with NO prior rows gets one
        placeholder row recording the failure."""
        raw_extraction = (
            {"raw_response": raw_text} if raw_text is not None else {"error": error_text}
        )
        if not existing_events:
            fields = {
                "source_kind": SOURCE_KIND_PROMOTER_POST, "source_handle": handle,
                "source_shortcode": shortcode, "source_permalink": post.get("permalink"),
                "status": STATUS_EXTRACTION_FAILED, "review_reason": REVIEW_REASON_EXTRACTION_FAILED,
                "raw_extraction": raw_extraction, "last_seen_at": now,
                "event_id": new_event_id(), "first_seen_at": now,
            }
            self.venue_dao.insert_event(fields)
            return

        for row in existing_events:
            if row.get("status") == STATUS_CONFIRMED:
                self.venue_dao.update_event(row["event_id"], {
                    "raw_extraction": raw_extraction, "last_seen_at": now,
                })
            else:
                self.venue_dao.update_event(row["event_id"], {
                    "status": STATUS_EXTRACTION_FAILED,
                    "review_reason": REVIEW_REASON_EXTRACTION_FAILED,
                    "raw_extraction": raw_extraction, "last_seen_at": now,
                })

    def _update_review_queue_gauge(self) -> None:
        EVENT_REVIEW_QUEUE_DEPTH.set(len(self.venue_dao.list_events_pending_location() or []))


__all__ = [
    "PromoterCrawlService", "ApifyPromoterPostsClient", "PromoterAccountUnavailable",
    "InvalidPromoterCrawlConfig", "parse_promoter_crawl_config",
    "SOURCE_KIND_PROMOTER_POST", "STATUS_SUPERSEDED",
    "OUTCOME_ARCHIVED", "OUTCOME_NOT_EVENT_LIKE", "OUTCOME_EXTRACTED",
    "OUTCOME_EXTRACTION_FAILED", "OUTCOME_ACCOUNT_UNAVAILABLE", "OUTCOME_TRUNCATED",
    "DEFAULT_MAX_EVENTS_PER_POST",
]
