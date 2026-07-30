"""Apify Google Maps Data Extractor client for venue photo discovery.

Uses the compass/google-maps-extractor actor via the Apify REST API
to fetch venue data including categorized photos from Google Maps.

Replaces both:
- thescrappa/google-maps-photos-scraper (broken as of Feb 2026)
- SearchApi.io google_maps_photos engine (disabled)

Uses async run pattern: start run → poll status → fetch dataset items.

Photo prioritization strategy:
  The compass extractor returns photos without category labels (imageCategories
  is always empty). To maximize the chance of finding menu photos, we:
  1. Request a larger photo pool (FETCH_PHOTOS_POOL) from the API
  2. Prioritize owner-uploaded photos (authorName matches venue title) since
     businesses typically upload their own menu photos
  3. Return only max_photos results for downstream GPT classification
"""
import asyncio
import logging
import time
import unicodedata
from typing import Optional

import httpx

from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.metrics import (
    APIFY_API_CALLS_TOTAL,
    APIFY_API_CALL_DURATION_SECONDS,
    APIFY_API_ERRORS_TOTAL,
    APIFY_POLL_TIMEOUTS_TOTAL,
)

logger = logging.getLogger(__name__)


class ApifyPollTimeoutError(Exception):
    """The actor run was still non-terminal when we stopped waiting.

    Raised rather than returned so the caller cannot mistake it for "no result".
    A bare `None` reported a mid-scrape venue as one that does not exist on
    Google Maps, and the caller had no way to tell the two apart.

    `last_status` carries the last non-terminal status observed (`READY` or
    `RUNNING`) — the field that decides whether the remedy is more time or less
    concurrency.
    """

    def __init__(self, message: str, last_status: str = "UNKNOWN"):
        super().__init__(message)
        self.last_status = last_status

APIFY_API_BASE = "https://api.apify.com/v2"

# Actor for Google Maps data extraction
GMAPS_EXTRACTOR_ACTOR = "compass~google-maps-extractor"

# Polling settings for async runs
POLL_INTERVAL_SECONDS = 5.0
MAX_POLL_ATTEMPTS = 60  # 5 min max wait

# Local sentinel for "we stopped waiting", kept distinct from Apify's own
# TIMED-OUT terminal status so the caller can tell "the actor gave up" from
# "we gave up on the actor".
POLL_BUDGET_EXHAUSTED = "POLL_BUDGET_EXHAUSTED"

# Request more photos from the API than max_photos to get a better pool
# for owner-photo prioritization. Owner photos are sorted first.
FETCH_PHOTOS_POOL = 50

# Default menu category keywords
DEFAULT_MENU_CATEGORIES = ["menu", "cardápio", "cardapio", "preços", "precos", "valores"]


def _normalize(text: str) -> str:
    """Strip accents and lowercase for comparison."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


class ApifyGMapsExtractorClient:
    """Async HTTP client for compass/google-maps-extractor actor.

    Fetches venue data including categorized photos from Google Maps.
    Filters photos by menu-related categories.
    """

    def __init__(
        self,
        api_token: str,
        timeout: float = 30.0,
        poll_continuation_seconds: float = 0.0,
    ):
        self.api_token = api_token
        # How much longer to keep polling a run that is still alive when the base
        # budget runs out. Zero disables it, which is the shipped default: the
        # right size depends on whether stalled runs are READY or RUNNING, and
        # that is measured by APIFY_POLL_TIMEOUTS_TOTAL before it is guessed.
        self.poll_continuation_seconds = poll_continuation_seconds
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def fetch_venue_menu_photos(
        self,
        search_query: str,
        menu_keywords: Optional[list[str]] = None,
        max_photos: int = 20,
    ) -> Optional[list[dict]]:
        """Fetch menu-category photos for a venue from Google Maps.

        Uses compass/google-maps-extractor which returns venue data including
        photos organized by categories. We filter to menu-related categories.

        Args:
            search_query: Venue name + address for search
            menu_keywords: Category titles to match (case/accent insensitive)
            max_photos: Maximum photos to return

        Returns:
            List of dicts: [{"image_url": "...", "category": "..."}]
            None on error or no results.
        """
        keywords = menu_keywords or DEFAULT_MENU_CATEGORIES
        normalized_keywords = [_normalize(kw) for kw in keywords]

        # Request a larger pool than max_photos so we can prioritize owner photos
        fetch_count = max(max_photos, FETCH_PHOTOS_POOL)

        run_input = {
            "searchStringsArray": [search_query],
            "maxImages": fetch_count,
            "language": "pt-BR",
            "includeImages": True,
            "scrapeImageAuthors": True,
        }

        start_time = time.perf_counter()
        endpoint_label = "gmaps_menu_photos"

        try:
            # 1. Start the run
            run_data = await self._start_run(run_input, endpoint_label)
            if not run_data:
                return None

            run_id = run_data["id"]
            dataset_id = run_data.get("defaultDatasetId")

            # 2. Poll until finished
            final_status, _ = await self._poll_run(run_id, endpoint_label)
            if final_status != "SUCCEEDED":
                logger.error(
                    f"[ApifyGMaps] Run {run_id} ended with status: {final_status}"
                )
                APIFY_API_CALLS_TOTAL.labels(
                    endpoint=endpoint_label, status="error"
                ).inc()
                return None

            # 3. Fetch dataset items
            items = await self._fetch_dataset(dataset_id, endpoint_label)

            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                duration
            )
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="success"
            ).inc()

            if not items:
                logger.info(
                    f"[ApifyGMaps] No results for query: {search_query}"
                )
                return None

            # 4. Extract menu photos from the first result
            return self._extract_menu_photos(items, normalized_keywords, max_photos)

        except ApifyCreditExhaustedError:
            raise

        except Exception as e:
            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                duration
            )
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="error"
            ).inc()
            logger.error(
                f"[ApifyGMaps] Error fetching photos for '{search_query}': {e}"
            )
            return None

    async def fetch_venue_photos(
        self,
        search_query: str,
        max_photos: int = 20,
        language: str = "pt-BR",
        scrape_image_authors: bool = True,
    ) -> Optional[list[dict]]:
        """Fetch ALL available photos for a venue — the archive's entry point.

        Sibling of `fetch_venue_menu_photos` rather than a flag on it: that one
        exists to find menu shots and filters by category, while the archive
        wants everything the actor returns. Same run shape, same billing (per
        place, not per photo), no category filter.

        Returns `{"photos": [...], "info": {...}}` — the photos in the
        archive's common dict shape (`url`, `author_name`, `photo_name`) plus
        everything else the actor returned, which the same charge already
        covered.
        """
        run_input = {
            "searchStringsArray": [search_query],
            "maxImages": max_photos,
            "language": language,
            "includeImages": True,
            # The single biggest cost driver on a photo-heavy venue: the actor
            # looks up an author PER IMAGE. Measured on Bar do Cuscuz (1,941
            # images): 1,729s with authors, 135s without — 12.8x, and the same
            # images either way. Venues that large cannot finish inside the poll
            # budget with this on, so it is a per-run choice: attribution, or
            # those venues at all.
            "scrapeImageAuthors": scrape_image_authors,
        }
        endpoint_label = "gmaps_archive_photos"
        start_time = time.perf_counter()

        try:
            run_data = await self._start_run(run_input, endpoint_label)
            if not run_data:
                return None
            status, last_status = await self._poll_run(
                run_data["id"], endpoint_label
            )
            if status == POLL_BUDGET_EXHAUSTED:
                # Observed here as well as on the success path: without it the
                # histogram counted only the calls that finished, which is why 89
                # real calls showed up as 54 observations and the slow tail was
                # invisible in the latency data.
                APIFY_API_CALL_DURATION_SECONDS.labels(
                    endpoint=endpoint_label
                ).observe(time.perf_counter() - start_time)
                APIFY_API_CALLS_TOTAL.labels(
                    endpoint=endpoint_label, status="error"
                ).inc()
                raise ApifyPollTimeoutError(
                    f"Apify run for {search_query!r} still {last_status} when the "
                    f"poll budget was exhausted",
                    last_status=last_status,
                )
            if status != "SUCCEEDED":
                logger.error(f"[ApifyGMaps] archive run ended as {status}")
                APIFY_API_CALLS_TOTAL.labels(
                    endpoint=endpoint_label, status="error"
                ).inc()
                return None
            items = await self._fetch_dataset(
                run_data.get("defaultDatasetId"), endpoint_label
            )
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                time.perf_counter() - start_time
            )
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="success"
            ).inc()
            if not items:
                logger.info(f"[ApifyGMaps] no result for query: {search_query}")
                return None
            return {
                "photos": self._archive_photos(items, max_photos),
                # Everything the actor returned that is not an image. Already
                # paid for by the same place-scraped event, so discarding it
                # would be throwing away data we were billed for.
                "info": self._place_info(items[0] or {}),
            }
        except ApifyCreditExhaustedError:
            # Propagated, never swallowed: the caller must stop the whole run
            # rather than keep paying into an exhausted balance.
            raise
        except ApifyPollTimeoutError:
            # Also propagated. Swallowing it here would collapse back into the
            # bare `None` this exception exists to replace, and the duration and
            # error counters were already recorded at the raise site.
            raise
        except Exception as e:  # noqa: BLE001 — one venue must not end a run
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                time.perf_counter() - start_time
            )
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="error"
            ).inc()
            logger.error(f"[ApifyGMaps] archive fetch failed for {search_query!r}: {e}")
            return None

    # Image payloads are stored as files, not repeated inside the info JSON.
    _IMAGE_KEYS = ("images", "imageUrls", "imageCategories")

    def _place_info(self, place: dict) -> dict:
        """The non-media half of the actor's result, kept verbatim.

        Deliberately a subtraction rather than an allow-list: the actor adds
        fields over time, and an allow-list would silently drop the new ones
        while still paying for them.
        """
        return {k: v for k, v in place.items() if k not in self._IMAGE_KEYS}

    def _archive_photos(self, items: list[dict], max_photos: int) -> list[dict]:
        """Normalise the actor's images into the archive's photo dict.

        The actor exposes images as `imageUrls` (plain strings) and/or
        `images` (objects with an author). Both are read, de-duplicated by URL,
        so a change in which one the actor populates cannot silently yield zero
        photos.
        """
        place = items[0] or {}
        out: list[dict] = []
        seen: set[str] = set()

        venue_title = _normalize(place.get("title", ""))
        for image in place.get("images") or []:
            url = (image or {}).get("imageUrl") or (image or {}).get("url")
            if url and url not in seen:
                seen.add(url)
                author = (image or {}).get("authorName")
                # Authorship is a FACT about the photo; `category` is where we
                # decide to file it. They were the same field, so classifying a
                # photo would have destroyed the owner/visitor signal — which
                # cannot be recovered without re-fetching.
                authorship = (
                    "by_owner"
                    if venue_title and author and _normalize(author) == venue_title
                    else "by_visitor"
                )
                out.append({
                    "url": url,
                    "author_name": author,
                    "author_uri": (image or {}).get("authorUrl"),
                    "authorship": authorship,
                    "uploaded_at": (image or {}).get("uploadedAt"),
                    # Verbatim provider payload, so a field we ignore today is
                    # still here when we want it.
                    "raw": image,
                    # Google exposes photo tabs (Menu, Food & drink, Vibe...) but
                    # tags no INDIVIDUAL image with one — `imageCategories` is a
                    # place-level list of which tabs exist. Authorship is the one
                    # signal derivable here, so it doubles as the filing category
                    # until a classifier assigns a real one; `authorship` above
                    # keeps the fact either way.
                    "category": authorship,
                    # No Google photo resource name from a scrape; the URL is
                    # what photo_id_for() hashes, which keeps ids stable.
                    "photo_name": None,
                })

        for url in place.get("imageUrls") or []:
            if url and url not in seen:
                seen.add(url)
                # A bare url carries nothing but itself: no author, so no
                # authorship claim can honestly be made.
                out.append({
                    "url": url, "author_name": None, "author_uri": None,
                    "photo_name": None, "uploaded_at": None,
                    "authorship": "unknown", "category": "by_visitor",
                    "raw": {"imageUrl": url},
                })

        return out[:max_photos]

    def _extract_menu_photos(
        self,
        items: list[dict],
        normalized_keywords: list[str],
        max_photos: int,
    ) -> Optional[list[dict]]:
        """Extract menu photos from compass extractor output.

        The extractor returns one item per place. Each item may have:
        - imageCategories: [{"title": "Menu", "images": ["url1", ...]}, ...]
        - images: [{"imageUrl": "...", "authorName": "..."}, ...]
        - imageUrls: ["url1", "url2", ...] (flat string array, older format)

        Strategy:
        1. If imageCategories has menu-matching categories, use those directly
        2. Otherwise fall back to images array, prioritizing owner-uploaded
           photos (authorName matches venue title) since owners typically
           upload their own menu photos
        """
        if not items:
            return None

        # Take first place result
        place = items[0]
        venue_title = _normalize(place.get("title", ""))

        # Try categorized photos first
        image_categories = place.get("imageCategories") or []
        menu_photos = []

        for category in image_categories:
            title = category.get("title", "")
            normalized_title = _normalize(title)

            if any(kw in normalized_title for kw in normalized_keywords):
                images = category.get("images") or []
                for img_url in images:
                    if isinstance(img_url, str) and img_url:
                        menu_photos.append({
                            "image_url": img_url,
                            "category": title,
                        })

        if menu_photos:
            logger.info(
                f"[ApifyGMaps] Found {len(menu_photos)} menu photos from "
                f"categorized images"
            )
            return menu_photos[:max_photos]

        # No categorized menu photos — fall back to `images` array with
        # owner-photo prioritization. Owner-uploaded photos are much more
        # likely to contain menus than customer/review photos.
        images_list = place.get("images") or []
        if images_list:
            owner_photos = []
            other_photos = []
            for img in images_list:
                if isinstance(img, dict):
                    url = img.get("imageUrl", "")
                    author = _normalize(img.get("authorName", ""))
                elif isinstance(img, str):
                    url = img
                    author = ""
                else:
                    continue
                if not url:
                    continue
                entry = {"image_url": url, "category": ""}
                # Check if this photo was uploaded by the venue owner
                if venue_title and author and (
                    venue_title in author or author in venue_title
                ):
                    owner_photos.append(entry)
                else:
                    other_photos.append(entry)

            # Owner photos first, then others
            prioritized = owner_photos + other_photos
            if prioritized:
                result = prioritized[:max_photos]
                owner_count = min(len(owner_photos), max_photos)
                logger.info(
                    f"[ApifyGMaps] No image categories available. "
                    f"Returning {len(result)} photos "
                    f"({owner_count} owner-uploaded, "
                    f"{len(result) - owner_count} other) "
                    f"for GPT classification"
                )
                return result

        # Also try imageUrls (flat string array, older format)
        image_urls = place.get("imageUrls") or []
        if image_urls:
            fallback_photos = [
                {"image_url": url, "category": ""}
                for url in image_urls
                if isinstance(url, str) and url
            ]
            if fallback_photos:
                logger.info(
                    f"[ApifyGMaps] Using imageUrls fallback. "
                    f"Returning {len(fallback_photos[:max_photos])} photos"
                )
                return fallback_photos[:max_photos]

        # No photos at all
        logger.info(
            f"[ApifyGMaps] No photos found for venue. "
            f"Available categories: {[c.get('title', '') for c in image_categories]}"
        )
        return None

    async def _start_run(
        self, run_input: dict, endpoint_label: str
    ) -> Optional[dict]:
        """Start an async actor run."""
        url = f"{APIFY_API_BASE}/acts/{GMAPS_EXTRACTOR_ACTOR}/runs"
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
        """Poll an actor run until it finishes.

        Returns `(final_status, last_non_terminal_status)`.

        The final status is one of Apify's four terminal states, or the local
        sentinel `POLL_BUDGET_EXHAUSTED` when we stopped waiting on a run that was
        still alive. That sentinel is not Apify's `TIMED-OUT`: Apify's means the
        actor itself hit its own limit and is a real terminal answer, ours means
        only that we gave up watching. Returning `"TIMED-OUT"` for both made them
        indistinguishable to the caller.

        When the budget is exhausted the run is, by definition, still `READY` or
        `RUNNING` — it has not failed. If a continuation window is configured we
        keep polling THE SAME run rather than abandoning it, because the scrape is
        already paid for and starting a replacement run would bill a second time
        for the same venue.
        """
        url = f"{APIFY_API_BASE}/actor-runs/{run_id}"
        params = {"token": self.api_token}

        continuation_attempts = 0
        if self.poll_continuation_seconds > 0 and POLL_INTERVAL_SECONDS > 0:
            continuation_attempts = int(
                self.poll_continuation_seconds / POLL_INTERVAL_SECONDS
            )
        base_budget = MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS
        total_attempts = MAX_POLL_ATTEMPTS + continuation_attempts
        last_non_terminal = "UNKNOWN"

        for attempt in range(1, total_attempts + 1):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            if attempt == MAX_POLL_ATTEMPTS + 1:
                logger.warning(
                    f"[ApifyGMaps] Run {run_id} still {last_non_terminal} after "
                    f"{base_budget:.1f}s; continuing to poll the same run for up "
                    f"to {self.poll_continuation_seconds:.1f}s more"
                )

            try:
                response = await self.client.get(url, params=params)
                if getattr(response, "status_code", 200) == 402:
                    # The balance can run out mid-poll, not just at start-run.
                    # Propagated so the run stops rather than keeps polling and
                    # starting more runs against an exhausted account.
                    APIFY_API_ERRORS_TOTAL.labels(
                        endpoint=endpoint_label, error_type="credit_exhausted"
                    ).inc()
                    raise ApifyCreditExhaustedError("Apify credits exhausted (402)")
                response.raise_for_status()
                data = response.json().get("data", {})
                status = data.get("status", "UNKNOWN")

                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    if status == "SUCCEEDED" and attempt > MAX_POLL_ATTEMPTS:
                        elapsed = attempt * POLL_INTERVAL_SECONDS
                        logger.info(
                            f"[ApifyGMaps] Run {run_id} recovered after "
                            f"{elapsed:.1f}s — {elapsed - base_budget:.1f}s past "
                            f"the base budget; no second run was started"
                        )
                    return status, last_non_terminal

                last_non_terminal = status

            except httpx.HTTPError as e:
                logger.warning(
                    f"[ApifyGMaps] Poll error for run {run_id}: {e}"
                )

        logger.error(
            f"[ApifyGMaps] Run {run_id} exhausted its "
            f"{total_attempts * POLL_INTERVAL_SECONDS:.1f}s poll budget while "
            f"still {last_non_terminal}"
        )
        APIFY_API_ERRORS_TOTAL.labels(
            endpoint=endpoint_label, error_type="timeout"
        ).inc()
        APIFY_POLL_TIMEOUTS_TOTAL.labels(
            endpoint=endpoint_label, last_status=last_non_terminal
        ).inc()
        return POLL_BUDGET_EXHAUSTED, last_non_terminal

    async def _fetch_dataset(
        self, dataset_id: str, endpoint_label: str
    ) -> Optional[list[dict]]:
        """Fetch items from the run's default dataset."""
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
            logger.error(
                f"[ApifyGMaps] Failed to fetch dataset {dataset_id}: {e}"
            )
            return None
