"""Apify Google Maps Reviews client — the deep-review-corpus paid boundary.
See plans/260813_deep-review-corpus.md.

Mirrors app/api/apify_gmaps_extractor_client.py's start/poll/fetch shape
VERBATIM (the plan's own words): `_start_run` / `_poll_run` / `_fetch_dataset`,
the same `POLL_BUDGET_EXHAUSTED` local sentinel kept distinct from Apify's own
terminal statuses, and the same `ApifyCreditExhaustedError` propagation on a
402. Reuses `ApifyPollTimeoutError` from that module rather than redefining
it — "same error type" per the plan.

Uses `compass/google-maps-reviews-scraper` (from $0.30 / 1,000 reviews), NOT
`compass/google-maps-extractor` (the existing client above) — that actor's
own docs say it "does not extract ... Images, Reviews".

One place id per run, not a batch of several: an approved BDD scenario
("One venue's failure does not abort the run") requires per-venue failure
isolation, and a single Apify run returns one terminal status and one
dataset for everything it covers — there is no way for a multi-place-id run
to fail for only one of its place ids. DeepReviewCrawlService therefore calls
`fetch_reviews` once per venue even though this client's `place_ids`
parameter accepts a list (matching the actor's real input shape, and left
open for a future batched mode that does not need per-venue isolation). This
is a deliberate deviation from the plan's literal "batch several place ids
per run" cost framing — see DeepReviewCrawlService's module docstring and the
execution report for the reasoning.

ASSUMED dataset item shape (compass/google-maps-reviews-scraper), UNVERIFIED
against a live run: `reviewId`, `name` (author), `text`, `publishedAtDate`
(ISO 8601), `stars` (int rating), `reviewerLanguage`. The plan's own Manual
Checks section names confirming these against a real one-venue trial (Phase
2b) as a precondition before any paid batch — `parse_review` below is the
one function that trial must re-validate.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.api.apify_gmaps_extractor_client import (
    APIFY_API_BASE,
    MAX_POLL_ATTEMPTS,
    POLL_BUDGET_EXHAUSTED,
    POLL_INTERVAL_SECONDS,
    ApifyPollTimeoutError,
)
from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.metrics import (
    APIFY_API_CALLS_TOTAL,
    APIFY_API_CALL_DURATION_SECONDS,
    APIFY_API_ERRORS_TOTAL,
    APIFY_POLL_TIMEOUTS_TOTAL,
)
from app.models.venue_review import VenueReview

logger = logging.getLogger(__name__)

GMAPS_REVIEWS_ACTOR = "compass~google-maps-reviews-scraper"


def parse_publish_time(value) -> Optional[datetime]:
    """Parse an ISO 8601 publish timestamp (a raw actor field, or a
    `VenueReview.publish_time` this module already normalized), tolerating a
    trailing `Z`. Returns None — never raises — on anything unparseable, so a
    malformed date degrades to "unusable" for both item parsing (skip) and
    window/cursor comparison, rather than crashing a whole batch."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_publish_time_for_actor(dt: datetime) -> str:
    """Render a datetime in the ONE shape the actor's `reviewsStartDate`
    input validation accepts for an absolute timestamp:
    `YYYY-MM-DDTHH:MM:SSZ` — UTC, a trailing `Z`, second precision, no
    `+00:00` offset and no microseconds.

    The actor's own validation regex (read verbatim off a live 400 body):
    ``^(\\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\\d|3[01])(T[0-2]\\d:[0-5]\\d(:[0-5]\\d)?(\\.\\d+)?Z?)?$``
    ``|^(\\d+)\\s*(minute|hour|day|week|month|year)s?$``

    Python's `datetime.isoformat()` produces `+00:00`, which that pattern
    REJECTS — this is the exact defect that made a real 150-venue crawl bill
    $0 while reporting `outcome: "ok"`: every `reviewsStartDate` was invalid,
    every actor call 400'd before a run ever started, and the client's own
    error handling (fixed separately) swallowed the failure into an empty
    result. Every value handed to the actor MUST be pushed through this
    function first — never a bare `.isoformat()`.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_review(item: dict) -> Optional[VenueReview]:
    """Parse one dataset item into a `VenueReview`, or None when the item is
    unusable. An item without an author, review text, or a parseable publish
    date cannot be windowed, deduped, or attributed — storing it as a hollow
    record would be worse than skipping it, so it is dropped and counted as
    malformed by the caller rather than raising and failing the whole batch.
    """
    if not isinstance(item, dict):
        return None
    author = item.get("name") or item.get("authorName")
    text = item.get("text")
    publish_raw = item.get("publishedAtDate") or item.get("publishAt")
    if not author or not text or not publish_raw:
        return None
    published = parse_publish_time(publish_raw)
    if published is None:
        return None
    rating_raw = item.get("stars", item.get("rating"))
    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        rating = 0
    return VenueReview(
        author_name=str(author),
        rating=rating,
        text=str(text),
        # The actor's human phrase ("2 months ago"), when it supplies one;
        # empty rather than None because the field is required and there is
        # nothing honest to default it to otherwise.
        relative_time=item.get("publishedAtDateText") or "",
        language=item.get("reviewerLanguage") or item.get("language"),
        publish_time=published.isoformat(),
        review_id=item.get("reviewId") or item.get("id"),
        source="apify_gmaps",
    )


class ApifyGMapsReviewsClient:
    """Async HTTP client for compass/google-maps-reviews-scraper."""

    def __init__(self, api_token: str, timeout: float = 30.0):
        self.api_token = api_token
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def close(self):
        await self.client.aclose()

    async def fetch_reviews(
        self,
        place_ids: list[str],
        max_reviews: int,
        language: str = "pt-BR",
        since: Optional[str] = None,
    ) -> Optional[list[dict]]:
        """Start a run over `place_ids`, sorted newest-first, and return its
        raw dataset items.

        Returns `None` on a hard failure (a non-402 HTTP error starting the
        run, a non-SUCCEEDED terminal status, or a failed dataset fetch) and
        `[]` only for a GENUINE empty result — a run that actually succeeded
        and legitimately found nothing. The caller (DeepReviewCrawlService)
        depends on this distinction to tell "this venue has no new reviews"
        apart from "the call failed": collapsing both into `[]` is exactly
        what let a real outage (every call 400ing on an invalid
        `reviewsStartDate`) get reported as a clean, zero-review success for
        every one of 150 venues. NEVER let this method's happy path fall
        back to `[]` on any error branch.

        `since` (an ISO date, or the actor's relative-window shorthand) is
        its own date filter under "Newest" sort (plan Evidence: "date
        filtering works only under 'Newest' sort") — passed when a venue
        already has a stored deep-review record, so a re-run's actual bill
        reflects only what is genuinely new rather than re-fetching (and
        re-billing for) the whole window. Field name `reviewsStartDate` is
        the actor's DOCUMENTED input as of plan-time; unverified against a
        live run (see the module docstring). The caller MUST have already
        normalized `since` via `format_publish_time_for_actor` — this client
        forwards it verbatim and does not itself validate the shape.
        """
        # Field names come from the actor's OWN published input schema (read
        # from its latest build via the Apify API on 2026-08-14), not from the
        # store page prose. The sort key is `reviewsSort` — we previously sent
        # `sort`, which is not a field this actor has, so it was ignored and we
        # were silently relying on the actor's default ordering. That matters:
        # `reviewsStartDate` filtering is only meaningful under newest-first.
        # The full field set is: startUrls, placeIds, maxReviews, reviewsSort,
        # reviewsStartDate, reviewsFilterString, language, reviewsOrigin,
        # personalData. There is NO server-side "only reviews with text"
        # option — that filter has to run locally, after paying.
        run_input = {
            "placeIds": place_ids,
            "maxReviews": max_reviews,
            "reviewsSort": "newest",
            "language": language,
        }
        if since:
            run_input["reviewsStartDate"] = since

        endpoint_label = "gmaps_reviews"
        start_time = time.perf_counter()
        try:
            run_data = await self._start_run(run_input, endpoint_label)
            if not run_data:
                return None
            status, last_status = await self._poll_run(run_data["id"], endpoint_label)
            if status == POLL_BUDGET_EXHAUSTED:
                APIFY_API_CALL_DURATION_SECONDS.labels(
                    endpoint=endpoint_label
                ).observe(time.perf_counter() - start_time)
                APIFY_API_CALLS_TOTAL.labels(endpoint=endpoint_label, status="error").inc()
                raise ApifyPollTimeoutError(
                    f"Apify reviews run for {place_ids!r} still {last_status} "
                    "when the poll budget was exhausted",
                    last_status=last_status,
                )
            if status != "SUCCEEDED":
                logger.error(f"[ApifyGMapsReviews] run ended as {status}")
                APIFY_API_CALLS_TOTAL.labels(endpoint=endpoint_label, status="error").inc()
                return None
            items = await self._fetch_dataset(run_data.get("defaultDatasetId"), endpoint_label)
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                time.perf_counter() - start_time
            )
            if items is None:
                # _fetch_dataset's own failure signal — do NOT fold this into
                # `[]`. A dataset fetch failing after the actor run itself
                # SUCCEEDED is still a hard failure from the caller's point
                # of view (no reviews were actually retrieved), and must
                # never be mislabeled "success" the way `items or []` did.
                APIFY_API_CALLS_TOTAL.labels(endpoint=endpoint_label, status="error").inc()
                return None
            APIFY_API_CALLS_TOTAL.labels(endpoint=endpoint_label, status="success").inc()
            return items
        except ApifyCreditExhaustedError:
            raise
        except ApifyPollTimeoutError:
            raise
        except Exception as e:  # noqa: BLE001 — the caller isolates per venue
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                time.perf_counter() - start_time
            )
            APIFY_API_CALLS_TOTAL.labels(endpoint=endpoint_label, status="error").inc()
            logger.error(f"[ApifyGMapsReviews] fetch failed for {place_ids!r}: {e}")
            return None

    async def _start_run(self, run_input: dict, endpoint_label: str) -> Optional[dict]:
        url = f"{APIFY_API_BASE}/acts/{GMAPS_REVIEWS_ACTOR}/runs"
        params = {"token": self.api_token}
        response = await self.client.post(url, params=params, json=run_input)
        if response.status_code == 402:
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="credit_exhausted"
            ).inc()
            raise ApifyCreditExhaustedError("Apify credits exhausted (402)")
        response.raise_for_status()
        return response.json().get("data")

    async def _poll_run(self, run_id: str, endpoint_label: str) -> tuple[str, str]:
        """Identical shape to ApifyGMapsExtractorClient._poll_run — see that
        method's docstring for the POLL_BUDGET_EXHAUSTED-vs-TIMED-OUT
        reasoning. No poll-continuation window here: unlike a photo/venue
        scrape, a stalled review run is cheap to just retry on the next
        scheduled operator run rather than special-cased."""
        url = f"{APIFY_API_BASE}/actor-runs/{run_id}"
        params = {"token": self.api_token}
        last_non_terminal = "UNKNOWN"
        for _ in range(1, MAX_POLL_ATTEMPTS + 1):
            await self._sleep(POLL_INTERVAL_SECONDS)
            try:
                response = await self.client.get(url, params=params)
                if getattr(response, "status_code", 200) == 402:
                    APIFY_API_ERRORS_TOTAL.labels(
                        endpoint=endpoint_label, error_type="credit_exhausted"
                    ).inc()
                    raise ApifyCreditExhaustedError("Apify credits exhausted (402)")
                response.raise_for_status()
                data = response.json().get("data", {})
                status = data.get("status", "UNKNOWN")
                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    return status, last_non_terminal
                last_non_terminal = status
            except httpx.HTTPError as e:
                logger.warning(f"[ApifyGMapsReviews] Poll error for run {run_id}: {e}")

        logger.error(
            f"[ApifyGMapsReviews] Run {run_id} exhausted its "
            f"{MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS:.1f}s poll budget while "
            f"still {last_non_terminal}"
        )
        APIFY_API_ERRORS_TOTAL.labels(endpoint=endpoint_label, error_type="timeout").inc()
        APIFY_POLL_TIMEOUTS_TOTAL.labels(
            endpoint=endpoint_label, last_status=last_non_terminal
        ).inc()
        return POLL_BUDGET_EXHAUSTED, last_non_terminal

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    async def _fetch_dataset(self, dataset_id: str, endpoint_label: str) -> Optional[list[dict]]:
        url = f"{APIFY_API_BASE}/datasets/{dataset_id}/items"
        params = {"token": self.api_token}
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="http_error"
            ).inc()
            logger.error(f"[ApifyGMapsReviews] Failed to fetch dataset {dataset_id}: {e}")
            return None
