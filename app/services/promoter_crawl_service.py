"""Crawl active promoter accounts, extract their events with the existing
extractor, and resolve each event's venue through the resolution ladder. See
plans/260804_instagram-promoter-events.md §C-D,
plans/260806_multi-event-posts.md (one post can announce SEVERAL events), and
plans/260806_venue-post-multi-event.md (the per-post reconciliation itself is
now SHARED with EventExtractionService — see app/services/
event_reconciliation.py — rather than duplicated here).

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
principle applies to a `confirmed` event — now enforced by the shared
`reconcile_post_events`, not a local copy. With several events per post, both
protections apply PER EVENT (matched by its content-derived
`source_event_key`, app.services.event_identity), not per post — a confirmed
or manually-linked sibling must never be affected by what happens to the
other events the same post announces. A `confirmed` promoter event now also
gets its divergence FLAGGED when the model's fresh title/date no longer
matches — a behaviour this path gained by moving onto the shared module,
mirroring what the venue path already did.
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
    EVENT_EXTRACTION_MALFORMED_ATTRACTIONS_TOTAL,
    EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL,
    EVENT_EXTRACTION_POSTS_TOTAL,
    EVENT_REVIEW_QUEUE_DEPTH,
    PROMOTER_CRAWL_POSTS_TOTAL,
)
from app.models.event_kind import NON_EVENT_KINDS
from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
from app.services.event_caption_matcher import matches_event_marker
from app.services.event_date_resolver import (
    REASON_DATE_RANGE,
    REASON_YEAR_INFERRED,
    resolve_event_datetime,
    vote_on_sibling_years,
)
from app.services.event_extraction_service import (
    KIND_LABEL_MIXED,
    KIND_LABEL_NOT_APPLICABLE,
    KIND_LABEL_UNKNOWN,
    OUTCOME_NOT_AN_EVENT as EES_OUTCOME_NOT_AN_EVENT,
)
from app.services.event_merge import merge_touched_events
from app.services.event_reconciliation import (
    STATUS_CONFIRMED,
    STATUS_SUPERSEDED,
    new_event_id,
    reconcile_post_events,
)
from app.services.event_venue_resolution import (
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_MARGIN,
    build_handle_index,
    build_location_text_attribute_fn,
    build_venue_catalog,
)
from app.services.event_venue_targeting import _parse_timestamp
from app.services.instagram_handle_sources import normalize_handle
from app.services.promoter_registry_service import STATUS_ACTIVE
from app.services.venue_photo_archive_service import new_run_id, run_prefix

logger = logging.getLogger(__name__)

SOURCE_KIND_PROMOTER_POST = "promoter_post"

STATUS_EXTRACTION_FAILED = "extraction_failed"
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
                archived_images: list[dict] = []
                if prefix is not None:
                    archived_images = await self._archive_post_images(prefix, handle, post)
                    manifest_entries.extend(archived_images)
                extracted += await self._process_post(
                    handle=handle, post=post, venues=venues, handle_index=handle_index,
                    now=now, archived_images=archived_images,
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
        archived_images: list[dict] = (),
        location_text_fallback_to_caption: bool = False,
        source_kind: str = SOURCE_KIND_PROMOTER_POST,
        attribution_outcomes: Optional[list] = None,
        report_kind_metric: bool = False,
    ) -> int:
        """Returns the number of events persisted (0 on any early exit) —
        the caller sums this into `events_extracted`, which is now an
        accurate per-event tally rather than a per-post flag.

        `source_kind` — plans/260810_date-correctness-review-reasons-and-
        path-parity.md §D: defaults to `SOURCE_KIND_PROMOTER_POST` so a REAL
        promoter account (`chain_promoter`/`run()`) is byte-for-byte
        unchanged. `InstagramCrawlChainer._chain_shared_handle` passes
        `SOURCE_KIND_VENUE_POST` — the posts this method handles there come
        from a VENUE's own account, and stamping them `promoter_post` was
        the provenance bug this plan fixes (it is also how those events used
        to reach the review queue at all, via the queue predicate's now-
        widened unresolved branch — see `list_events_awaiting_decision`).

        `attribution_outcomes`, when given a list, gets this post's own
        venue-resolution outcome (`RESOLUTION_AUTO`/`RESOLUTION_QUEUED`/
        `RESOLUTION_UNRESOLVED`) appended for every event this post's
        resolution ladder actually ran for — `_chain_shared_handle` reads it
        back to bump `CRAWL_VENUE_ATTRIBUTION_TOTAL{outcome="resolved"|
        "ambiguous"}` once per post. `None` (the default) costs nothing extra
        for a real promoter account, which already reports through
        `EVENT_VENUE_LINK_TOTAL` instead.

        `report_kind_metric` — plans/260810_date-correctness-review-reasons-
        and-path-parity.md §D: also bump `EVENT_EXTRACTION_POSTS_TOTAL`
        (kind-labelled, the SAME counter `EventExtractionService.run()`
        already emits for the single-venue path) so the event/non-event
        split is visible on the shared-handle path too. Default False: a
        real promoter account is a genuinely different content source (many
        venues, not one), and folding it into the same counter would mix two
        populations a dashboard has no way to tell apart again.

        `archived_images` is this SAME post's already-archived objects (see
        `_archive_post_images`, called just before this in `run()`). One
        post can announce several events (plans/260806_multi-event-posts.md)
        and slide-to-event alignment is out of scope
        (plans/260806_multi-event-posts.md), so every event this post
        produces gets the post's FIRST archived image as its cover — better
        than a perishable permalink alone, and honest about what it is. A
        post that stored no image (or whose only image failed to archive)
        leaves every one of its events with `cover_photo_key = None`.

        `location_text_fallback_to_caption` — plans/260810_post-kind-and-
        post-extraction-attribution.md §C: default False, so a REAL
        promoter account's own resolution (run against its full servable
        catalog, `venues=build_venue_catalog(...)`) is byte-for-byte
        unchanged — a caption is much longer and noisier free text than a
        promoter's own `location_text`, and name-matching it against the
        WHOLE catalog risks a false match the bounded, few-candidate
        shared-handle case does not. `InstagramCrawlChainer._chain_shared_
        handle` passes True: there, `venues` is already bounded to one
        handle's own two or three venues (`candidate_venues_for_ids`), so
        a caption naming a branch in passing is exactly the signal §C asks
        this path to use when the model itself reported no location_text.
        """
        cover_photo_key = archived_images[0]["key"] if archived_images else None
        shortcode = post.get("shortcode")
        caption = post.get("caption")
        if not shortcode:
            return 0

        # ALL rows already extracted from this post, not just one — a post
        # can hold several events (plans/260806_multi-event-posts.md), so
        # get_event_by_source (at most one row) is unsafe here.
        existing_events = self.venue_dao.list_events_by_source(handle, shortcode)

        # plans/260810_date-correctness-review-reasons-and-path-parity.md §D:
        # also bump the venue-path's own kind-labelled counter when opted in
        # (`_chain_shared_handle` only) — see this method's own docstring.
        def _bump_kind_metric(outcome: str, kind: str = KIND_LABEL_NOT_APPLICABLE) -> None:
            if report_kind_metric:
                EVENT_EXTRACTION_POSTS_TOTAL.labels(outcome=outcome, kind=kind).inc()

        if not matches_event_marker(caption):
            PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_NOT_EVENT_LIKE).inc()
            _bump_kind_metric(OUTCOME_NOT_EVENT_LIKE)
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
            self._record_failure(
                handle, shortcode, post, raw_text, str(e), existing_events, now,
                source_kind=source_kind,
            )
            PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_EXTRACTION_FAILED).inc()
            _bump_kind_metric(OUTCOME_EXTRACTION_FAILED)
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
            _bump_kind_metric(OUTCOME_TRUNCATED)
            return 0

        try:
            events_data, malformed_count, malformed_attractions_count = (
                parse_multi_event_extraction_response(
                    raw_text, max_events=self.max_events_per_post,
                )
            )
        except EventExtractionParseError as e:
            logger.warning(f"[PromoterCrawl] extraction failed for {handle}/{shortcode}: {e}")
            self._record_failure(
                handle, shortcode, post, raw_text, str(e), existing_events, now,
                source_kind=source_kind,
            )
            PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_EXTRACTION_FAILED).inc()
            _bump_kind_metric(OUTCOME_EXTRACTION_FAILED)
            return 0

        if malformed_count:
            EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL.inc(malformed_count)
        if malformed_attractions_count:
            EVENT_EXTRACTION_MALFORMED_ATTRACTIONS_TOTAL.inc(malformed_attractions_count)

        post_ts = _parse_timestamp(post.get("timestamp")) or now

        kept_events: list[dict] = []
        for parsed in events_data:
            # plans/260810_post-kind-and-post-extraction-attribution.md §B,
            # re-scoped mid-execution: events, promotions and menus are
            # separate entities — this feature builds no promotion/menu
            # entity, so a post classified as anything other than `event`
            # produces NO events.event row. A missing/unrecognised kind is
            # never in NON_EVENT_KINDS (the fail-toward-visible rule), so it
            # falls through to the normal event flow below.
            kind = parsed.get("kind")
            if kind in NON_EVENT_KINDS:
                logger.info(
                    f"[PromoterCrawl] non-event post skipped for "
                    f"{handle}/{shortcode}: kind={kind!r} "
                    f"title={parsed.get('title')!r} -- no event row created"
                )
                continue
            kept_events.append(parsed)

        # Each event resolves its OWN date independently, against the post's
        # timestamp — never a sibling's raw text — then
        # `vote_on_sibling_years` (plans/260810_date-correctness-review-
        # reasons-and-path-parity.md §A) looks across THIS post's own events
        # only (one flyer, one programme) and pulls a lone rolled-forward
        # outlier back onto the year the rest of the post agrees on. Mirrors
        # `EventExtractionService._extract_one` exactly — the two paths must
        # not drift on how a post's dates are resolved.
        resolved_dates = [
            resolve_event_datetime(
                date_text=parsed["date_text"], time_text=parsed["time_text"],
                post_timestamp=post_ts,
                is_recurring=parsed["is_recurring"], recurrence_text=parsed["recurrence_text"],
            )
            for parsed in kept_events
        ]
        resolved_dates = vote_on_sibling_years(resolved_dates)

        prepared_events: list[dict] = []
        for parsed, resolved_date in zip(kept_events, resolved_dates):
            reasons: list[str] = []
            if resolved_date.review_reason:
                # Use the resolver's OWN reason rather than assuming
                # REASON_MISSING_DATE: plans/260807_date-resolution-
                # correctness.md's weekday_mismatch also sets needs_review
                # True while still resolving a real starts_at — hardcoding
                # "missing_date" here would mislabel a resolved-but-flagged
                # date as a blank one.
                reasons.append(resolved_date.review_reason)
            if resolved_date.year_inferred:
                reasons.append(REASON_YEAR_INFERRED)
            if resolved_date.date_range:
                reasons.append(REASON_DATE_RANGE)
            if parsed["confidence"] < self.min_confidence:
                reasons.append(REVIEW_REASON_LOW_CONFIDENCE)
            review_reason = "; ".join(reasons) if reasons else None

            prepared_events.append({
                "starts_at": resolved_date.starts_at, "ends_at": resolved_date.ends_at,
                "is_recurring": resolved_date.is_recurring or bool(parsed["is_recurring"]),
                "recurrence_text": resolved_date.recurrence_text or parsed["recurrence_text"],
                "title": parsed["title"], "description": parsed["description"],
                "lineup": parsed["lineup"], "attractions": parsed["attractions"],
                "ticket_url": parsed["ticket_url"], "ticket_info": parsed["ticket_info"],
                "price_text": parsed["price_text"], "location_text": parsed["location_text"],
                "cover_photo_key": cover_photo_key,
                "confidence": parsed["confidence"],
                "review_reason": review_reason,
                "raw_extraction": parsed,
            })

        # plans/260810_date-correctness-review-reasons-and-path-parity.md
        # §D: the SAME per-post kind label EventExtractionService computes —
        # one event's own kind, "mixed" for a roundup, "not_applicable" for
        # none — never duplicated ad hoc. Computed from `events_data` (every
        # parsed event, BEFORE the non-event-kind filter above), exactly
        # like EventExtractionService._extract_one: a post whose one event
        # is a menu item must still be counted "menu", not "not_applicable"
        # just because no events.event row was created for it.
        if len(events_data) == 1:
            kind_label = events_data[0].get("kind") or KIND_LABEL_UNKNOWN
        elif len(events_data) > 1:
            kind_label = KIND_LABEL_MIXED
        else:
            kind_label = KIND_LABEL_NOT_APPLICABLE

        # The ONE thing parameterised (plans/260806_venue-post-multi-event.md
        # §B): each event runs the resolution ladder on its OWN
        # location_text — never a sibling's, never merged. An event with no
        # location_text resolves as unresolved and must NEVER inherit a
        # sibling's venue: resolve_event_venue already guarantees this (it
        # returns RESOLUTION_UNRESOLVED when location_text is absent, and
        # rungs 1-2 read only the shared caption/tag, which carries no
        # per-event data to leak between siblings). Never invoked by the
        # shared reconciler for a confirmed or manually-linked existing row.
        #
        # Returns (fields, on_persisted): `replace_event_venue_link_
        # candidates` writes a row with a real, non-deferrable FK to
        # events.event (migration 0024_promoter_accounts) — it MUST NOT run
        # until the event row itself is committed, so it is deferred into
        # `on_persisted` rather than called here directly. The reconciler
        # calls it after insert_event/update_event.
        #
        # plans/260811_extract-by-handle.md: this closure USED to be built
        # inline here. Extracted to `event_venue_resolution.
        # build_location_text_attribute_fn` so `EventExtractionService._run_
        # handles` (re-extracting a MULTI-VENUE handle's already-archived
        # posts, no re-crawl) resolves venues through the exact SAME ladder
        # call, not a second one. This call is behaviour-preserving —
        # `tests/test_promoter_crawl_service.py`'s
        # `TestAttributionBehaviourPinnedAcrossExtraction` pins the real
        # promoter path's outcomes/metrics across this refactor.
        _attribute = build_location_text_attribute_fn(
            caption=caption, location_tag=post.get("location_tag"),
            promoter_handle=handle, venues=venues, handle_index=handle_index,
            venue_dao=self.venue_dao, now=now,
            confidence_floor=self.confidence_floor, margin=self.margin,
            location_text_fallback_to_caption=location_text_fallback_to_caption,
            attribution_outcomes=attribution_outcomes,
        )

        touched_event_ids: list[str] = []
        persisted = reconcile_post_events(
            venue_dao=self.venue_dao,
            source_kind=source_kind,
            source_handle=handle,
            source_shortcode=shortcode,
            source_permalink=post.get("permalink"),
            prepared_events=prepared_events,
            now=now,
            attribute=_attribute,
            touched_event_ids=touched_event_ids,
            min_confidence=self.min_confidence,
        )
        # A promoter post can resolve to the SAME venue+date+title a venue's
        # own post (or another promoter post) already announced — recognise
        # that the moment this post's events are persisted, rather than
        # leaving it to a later migration. See
        # plans/260807_one-event-many-posts.md.
        if touched_event_ids:
            merge_touched_events(self.venue_dao, touched_event_ids, now)

        PROMOTER_CRAWL_POSTS_TOTAL.labels(outcome=OUTCOME_EXTRACTED).inc()
        if prepared_events:
            kind_outcome = OUTCOME_EXTRACTED
        elif events_data:
            # Every parsed event was a recognised non-event kind — nothing
            # persisted, by design. Mirrors
            # EventExtractionService._extract_one's OUTCOME_NOT_AN_EVENT.
            kind_outcome = EES_OUTCOME_NOT_AN_EVENT
        else:
            # events_data itself was empty ({"events": []}) — same edge case
            # EventExtractionService._extract_one leaves as OUTCOME_EXTRACTED.
            kind_outcome = OUTCOME_EXTRACTED
        _bump_kind_metric(kind_outcome, kind_label)
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
        *, source_kind: str = SOURCE_KIND_PROMOTER_POST,
    ) -> None:
        """A total extraction failure (API error, or a response so malformed
        even the top-level 'events' list cannot be read) — never even a
        partial persist. Mirrors the single-event contract per existing row:
        a confirmed row only ever gets raw_extraction/last_seen_at touched
        (not even a failed re-extraction reverts it); every other existing
        row for this post is marked extraction_failed so the failure is
        visible without losing the post. A post with NO prior rows gets one
        placeholder row recording the failure.

        `source_kind` — plans/260810_date-correctness-review-reasons-and-
        path-parity.md §D: `_process_post`'s own `source_kind` param,
        threaded through so a shared-handle venue post that fails extraction
        is ALSO stamped `venue_post`, not `promoter_post`."""
        raw_extraction = (
            {"raw_response": raw_text} if raw_text is not None else {"error": error_text}
        )
        if not existing_events:
            fields = {
                "source_kind": source_kind, "source_handle": handle,
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
                    # Identifies which of this (possibly multi-source,
                    # post-merge) event's sources owns this refresh — see
                    # plans/260807_one-event-many-posts.md.
                    "source_handle": handle, "source_shortcode": shortcode,
                })
            else:
                self.venue_dao.update_event(row["event_id"], {
                    "status": STATUS_EXTRACTION_FAILED,
                    "review_reason": REVIEW_REASON_EXTRACTION_FAILED,
                    "raw_extraction": raw_extraction, "last_seen_at": now,
                    "source_handle": handle, "source_shortcode": shortcode,
                })

    def _update_review_queue_gauge(self) -> None:
        # Widened alongside list_events_awaiting_decision (plans/260807_
        # review-queue-completeness-and-venue-names.md) — see that DAO
        # method's docstring, and the metric's own definition in
        # app/metrics.py, for what "review queue depth" means now.
        EVENT_REVIEW_QUEUE_DEPTH.set(len(self.venue_dao.list_events_awaiting_decision() or []))


__all__ = [
    "PromoterCrawlService", "ApifyPromoterPostsClient", "PromoterAccountUnavailable",
    "InvalidPromoterCrawlConfig", "parse_promoter_crawl_config",
    "SOURCE_KIND_PROMOTER_POST", "STATUS_SUPERSEDED",
    "OUTCOME_ARCHIVED", "OUTCOME_NOT_EVENT_LIKE", "OUTCOME_EXTRACTED",
    "OUTCOME_EXTRACTION_FAILED", "OUTCOME_ACCOUNT_UNAVAILABLE", "OUTCOME_TRUNCATED",
    "DEFAULT_MAX_EVENTS_PER_POST",
]
