"""Deep review corpus crawl — an operator-selected, two-gate-budgeted capture
of every Google review inside a window for a chosen venue subset.
See plans/260813_deep-review-corpus.md.

## Batching: a deliberate departure from the plan's literal design

The plan's Implementation Approach describes batching several place ids into
one Apify run ("Batching several place ids per run is what keeps Apify's
per-run overhead off the per-venue cost"). The approved Gherkin
(tests/bdd/enrichment/deep-review-corpus.feature) also requires per-venue
failure isolation ("One venue's failure does not abort the run" — the actor
fails for the second of three venues, the first and third still store). A
single Apify run returns ONE terminal status and ONE dataset for every place
id it covers; there is no way for a multi-place-id run to fail for only one
of them. The two requirements are mutually exclusive at the unit-of-failure
level, and the approved scenario wins (the execute-feature skill: "Do not
weaken the Gherkin scenario to make implementation easier").

So this service calls `ApifyGMapsReviewsClient.fetch_reviews` ONCE PER VENUE.
`settings.reviews_deep_batch_size` still means something, just not "place
ids per actor run": venues are grouped into batches of that size so the
(comparatively expensive, rate-limit-sensitive) Apify ACCOUNT headroom
lookup happens once per group rather than once per venue, while the LOCAL
monthly budget — a cheap Redis read — is still checked before literally
every actor call, per the plan's item 6 ("before every actor call").

## The two budget gates

1. LOCAL: `ReviewCrawlBudgetDao`, a monthly review counter, checked before
   EVERY actor call.
2. ACCOUNT: `ApifyAccountUsageClient`, the shared Apify plan's remaining
   headroom, checked once per batch. A batch is refused unless
   `headroom_usd - batch_worst_case_cost >= reviews_deep_reserved_headroom_usd`.
   A failed lookup (None) is ZERO headroom — refuse, never pass.

Either gate tripping stops the run at that point: everything already fetched
is persisted, and every venue not yet reached is named in the summary with
outcome "partial" — never a silent truncation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.api.apify_gmaps_reviews_client import parse_publish_time, parse_review
from app.config import settings
from app.dao.review_crawl_budget_dao import ReviewCrawlBudgetDao
from app.metrics import (
    DEEP_REVIEW_BUDGET_REMAINING,
    DEEP_REVIEW_CRAWL_COST_USD_TOTAL,
    DEEP_REVIEW_CRAWL_DURATION_SECONDS,
    DEEP_REVIEW_CRAWL_VENUES_TOTAL,
    DEEP_REVIEWS_FETCHED_TOTAL,
)
from app.models.venue_review import VenueReview, VenueReviewsDeep

logger = logging.getLogger(__name__)


def _dedup_key(review: VenueReview) -> tuple:
    """`review_id` when the actor supplies one (the preferred, stable key);
    otherwise `(author_name, publish_time, first 64 chars of text)` — the
    fallback exists because a scraped payload without a stable id must not
    duplicate on every re-run (plan's Implementation Approach)."""
    if review.review_id:
        return ("id", review.review_id)
    return ("fallback", review.author_name, review.publish_time, (review.text or "")[:64])


@dataclass
class SelectionResult:
    candidates: list[str] = field(default_factory=list)
    skipped_no_place_id: list[str] = field(default_factory=list)


class DeepReviewCrawlService:
    def __init__(
        self,
        repo,
        apify_client,
        budget_dao: ReviewCrawlBudgetDao,
        account_usage_client,
        now_fn=None,
    ):
        self.repo = repo  # VenueRepository (RDS system of record + Redis serving)
        self.apify_client = apify_client
        self.budget_dao = budget_dao
        self.account_usage_client = account_usage_client
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # ── candidate resolution ─────────────────────────────────────────────────
    def resolve_candidates(
        self, venue_ids: Optional[list[str]] = None, filter_spec: Optional[dict] = None
    ) -> SelectionResult:
        """The id list, when given, is AUTHORITATIVE: a filter may narrow it
        (drop ids the filter would not have matched) but can never add a venue
        the operator did not explicitly select. With no id list, the filter
        alone defines the candidate set."""
        if venue_ids:
            candidates = list(dict.fromkeys(venue_ids))  # de-dup, preserve order
            if filter_spec:
                allowed = self._apply_filter(filter_spec)
                candidates = [v for v in candidates if v in allowed]
        elif filter_spec:
            candidates = sorted(self._apply_filter(filter_spec))
        else:
            candidates = []

        resolved, skipped = [], []
        for vid in candidates:
            if self._place_id_for(vid):
                resolved.append(vid)
            else:
                skipped.append(vid)
        return SelectionResult(candidates=resolved, skipped_no_place_id=skipped)

    def _place_id_for(self, venue_id: str) -> Optional[str]:
        try:
            vibe = self.repo.get_vibe_attributes(venue_id)
        except Exception as e:
            logger.warning(f"[DeepReviewCrawl] vibe-attrs read failed for {venue_id}: {e}")
            return None
        return vibe.google_place_id if vibe else None

    def _apply_filter(self, filter_spec: dict) -> set[str]:
        ids = set(self.repo.list_servable_venue_ids())
        venue_type = filter_spec.get("venue_type")
        city = filter_spec.get("city")
        if venue_type or city:
            for vid in list(ids):
                venue = self.repo.get_venue(vid)
                if venue is None:
                    ids.discard(vid)
                    continue
                if venue_type and getattr(venue, "venue_type", None) != venue_type:
                    ids.discard(vid)
                    continue
                if city and city.lower() not in (getattr(venue, "venue_address", "") or "").lower():
                    ids.discard(vid)
        if filter_spec.get("has_no_deep_reviews"):
            has_deep = set(self.repo.rds_store.list_fresh_enrichment_venue_ids("venues.reviews_deep"))
            ids -= has_deep
        return ids

    def _get_deep(self, venue_id: str) -> Optional[VenueReviewsDeep]:
        try:
            return self.repo.get_venue_reviews_deep(venue_id)
        except Exception as e:
            logger.warning(f"[DeepReviewCrawl] deep-reviews read failed for {venue_id}: {e}")
            return None

    # ── estimate (spends nothing) ────────────────────────────────────────────
    def estimate(
        self, venue_ids: Optional[list[str]] = None, filter_spec: Optional[dict] = None
    ) -> dict:
        selection = self.resolve_candidates(venue_ids, filter_spec)
        max_per_venue = settings.reviews_deep_max_per_venue
        worst_case_reviews = len(selection.candidates) * max_per_venue
        worst_case_cost_usd = round(worst_case_reviews * settings.apify_review_cost_usd, 4)
        return {
            "selected_venue_count": len(selection.candidates),
            "skipped_no_place_id": list(selection.skipped_no_place_id),
            "worst_case_review_count": worst_case_reviews,
            "worst_case_cost_usd": worst_case_cost_usd,
            # The estimate is an upper bound, not a prediction (plan item 2):
            # how many reviews fall inside the window is unknowable without
            # actually crawling.
            "is_upper_bound": True,
            "window_days": settings.reviews_deep_window_days,
            "max_reviews_per_venue": max_per_venue,
        }

    # ── local budget (gate 1) ────────────────────────────────────────────────
    def _local_remaining(self, year_month: str) -> int:
        spent = self.budget_dao.get_month_count(year_month)
        return settings.reviews_deep_monthly_review_budget - spent

    # ── account headroom (gate 2) ────────────────────────────────────────────
    async def _account_headroom_usd(self) -> Optional[float]:
        try:
            return await self.account_usage_client.get_headroom_usd()
        except Exception as e:
            # A raising client is the same "unknown" as one that returns None —
            # both must refuse, never pass, per the plan's explicit rule.
            logger.error(f"[DeepReviewCrawl] account headroom lookup raised: {e}")
            return None

    # ── run (spends money, gated) ────────────────────────────────────────────
    async def run(
        self, venue_ids: Optional[list[str]] = None, filter_spec: Optional[dict] = None
    ) -> dict:
        start = time.perf_counter()
        selection = self.resolve_candidates(venue_ids, filter_spec)
        for _ in selection.skipped_no_place_id:
            DEEP_REVIEW_CRAWL_VENUES_TOTAL.labels(outcome="skipped_no_place_id").inc()

        summary = {
            "outcome": "ok",
            "stored_venues": [],
            "truncated_venues": [],
            "skipped_no_place_id": list(selection.skipped_no_place_id),
            "failed_venues": [],
            "not_reached_venues": [],
            "budget_stopped": False,
            "reviews_billed_total": 0,
        }

        candidates = list(selection.candidates)
        if not candidates:
            DEEP_REVIEW_CRAWL_DURATION_SECONDS.observe(time.perf_counter() - start)
            return summary

        year_month = ReviewCrawlBudgetDao.current_year_month_utc(self._now())

        # Gate 1, up front: a monthly budget already exhausted means no actor
        # run is ever started (plan scenario: "The budget is checked before
        # the paid call").
        if self._local_remaining(year_month) <= 0:
            summary["outcome"] = "budget_stopped"
            summary["budget_stopped"] = True
            summary["not_reached_venues"] = list(candidates)
            DEEP_REVIEW_BUDGET_REMAINING.set(max(self._local_remaining(year_month), 0))
            DEEP_REVIEW_CRAWL_DURATION_SECONDS.observe(time.perf_counter() - start)
            return summary

        batch_size = max(1, settings.reviews_deep_batch_size)
        stopped = False
        for group_start in range(0, len(candidates), batch_size):
            if stopped:
                break
            group = candidates[group_start: group_start + batch_size]

            # Gate 2, once per group: refuse the WHOLE group (and stop the
            # run) unless enough shared Apify headroom would remain after it.
            group_worst_cost = len(group) * settings.reviews_deep_max_per_venue * settings.apify_review_cost_usd
            headroom = await self._account_headroom_usd()
            if headroom is None or (headroom - group_worst_cost) < settings.reviews_deep_reserved_headroom_usd:
                logger.warning(
                    f"[DeepReviewCrawl] refusing batch of {len(group)} venues: "
                    f"insufficient Apify account headroom (headroom={headroom})"
                )
                summary["not_reached_venues"].extend(candidates[group_start:])
                summary["budget_stopped"] = True
                stopped = True
                break

            for offset, vid in enumerate(group):
                # Gate 1 again, before EVERY actor call (plan item 6, singular
                # "call") — a group can exhaust the local budget partway through.
                if self._local_remaining(year_month) <= 0:
                    summary["not_reached_venues"].extend(candidates[group_start + offset:])
                    summary["budget_stopped"] = True
                    stopped = True
                    break

                place_id = self._place_id_for(vid)
                existing = self._get_deep(vid)
                since = existing.newest_publish_time if existing else None

                try:
                    raw_items = await self.apify_client.fetch_reviews(
                        [place_id], settings.reviews_deep_max_per_venue, since=since
                    )
                except Exception as e:
                    logger.error(f"[DeepReviewCrawl] actor call failed for {vid}: {e}")
                    summary["failed_venues"].append(vid)
                    DEEP_REVIEW_CRAWL_VENUES_TOTAL.labels(outcome="error").inc()
                    continue

                billed = len(raw_items or [])
                if billed:
                    new_total = self.budget_dao.increment_month(year_month, billed)
                    DEEP_REVIEW_CRAWL_COST_USD_TOTAL.inc(billed * settings.apify_review_cost_usd)
                    DEEP_REVIEW_BUDGET_REMAINING.set(
                        max(settings.reviews_deep_monthly_review_budget - new_total, 0)
                    )
                    summary["reviews_billed_total"] += billed

                try:
                    outcome = self._store_venue_reviews(vid, existing, raw_items or [])
                except Exception as e:
                    logger.error(f"[DeepReviewCrawl] failed to persist {vid}: {e}")
                    summary["failed_venues"].append(vid)
                    DEEP_REVIEW_CRAWL_VENUES_TOTAL.labels(outcome="error").inc()
                    continue

                summary["stored_venues"].append(vid)
                if outcome == "truncated":
                    summary["truncated_venues"].append(vid)
                    DEEP_REVIEW_CRAWL_VENUES_TOTAL.labels(outcome="truncated").inc()
                else:
                    DEEP_REVIEW_CRAWL_VENUES_TOTAL.labels(outcome="ok").inc()

        # "ok" only when every candidate was actually reached and stored; a
        # budget stop mid-run or any per-venue failure makes the run
        # "partial" — never silently reported as a clean success (plan item 6:
        # "never silently truncate the selection").
        if summary["outcome"] != "budget_stopped" and (
            summary["not_reached_venues"] or summary["failed_venues"]
        ):
            summary["outcome"] = "partial"
        DEEP_REVIEW_CRAWL_DURATION_SECONDS.observe(time.perf_counter() - start)
        return summary

    # ── merge / dedup / window / cap ─────────────────────────────────────────
    def _store_venue_reviews(
        self, venue_id: str, existing: Optional[VenueReviewsDeep], raw_items: list[dict]
    ) -> str:
        """Merge freshly-fetched raw actor items into the venue's stored deep-
        review corpus. Returns "truncated" or "ok".

        Window and cap apply ONLY to newly fetched items — an existing stored
        review that has since aged out of the window is kept (plan item 5:
        aging out is not the same as expiring; a review already paid for is
        never worth re-fetching, and evidence a venue has, say, a covered
        play area does not expire the way busyness does)."""
        window_days = settings.reviews_deep_window_days
        cutoff = self._now() - timedelta(days=window_days)
        cap = settings.reviews_deep_max_per_venue

        existing_reviews = list(existing.reviews) if existing else []
        seen = {_dedup_key(r) for r in existing_reviews}

        parsed = [r for r in (parse_review(item) for item in raw_items) if r is not None]
        # Actor is asked to sort newest-first; sorted here too so the
        # dedup/window pass below is deterministic even if it does not.
        parsed.sort(key=lambda r: r.publish_time or "", reverse=True)

        added: list[VenueReview] = []
        for review in parsed:
            published = parse_publish_time(review.publish_time)
            # Window boundary is INCLUSIVE: a review published exactly
            # `window_days` ago is still "inside" the window (the plan reads
            # window_days as "the last N days", which includes day N).
            if published is not None and published < cutoff:
                DEEP_REVIEWS_FETCHED_TOTAL.labels(outcome="out_of_window").inc()
                continue
            key = _dedup_key(review)
            if key in seen:
                DEEP_REVIEWS_FETCHED_TOTAL.labels(outcome="duplicate").inc()
                continue
            seen.add(key)
            added.append(review)
            DEEP_REVIEWS_FETCHED_TOTAL.labels(outcome="stored").inc()

        merged = existing_reviews + added
        truncated = len(merged) > cap
        if truncated:
            # Cap binds on the NEWEST reviews — the newest-first fetch order
            # already means "whichever comes first" (window edge or cap) is
            # naturally the newest slice; keep that same newest-first rule
            # when trimming the merged set too.
            merged.sort(key=lambda r: r.publish_time or "", reverse=True)
            merged = merged[:cap]

        publish_times = [r.publish_time for r in merged if r.publish_time]
        deep = VenueReviewsDeep(
            venue_id=venue_id,
            reviews=merged,
            window_days=window_days,
            fetched_at=self._now().isoformat(),
            oldest_publish_time=min(publish_times) if publish_times else None,
            newest_publish_time=max(publish_times) if publish_times else None,
            truncated=truncated,
        )
        self.repo.set_venue_reviews_deep(deep)
        return "truncated" if truncated else "ok"
