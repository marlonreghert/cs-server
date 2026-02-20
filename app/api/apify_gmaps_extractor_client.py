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
)

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"

# Actor for Google Maps data extraction
GMAPS_EXTRACTOR_ACTOR = "compass~google-maps-extractor"

# Polling settings for async runs
POLL_INTERVAL_SECONDS = 5.0
MAX_POLL_ATTEMPTS = 60  # 5 min max wait

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

    def __init__(self, api_token: str, timeout: float = 30.0):
        self.api_token = api_token
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
            final_status = await self._poll_run(run_id, endpoint_label)
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

    async def _poll_run(self, run_id: str, endpoint_label: str) -> str:
        """Poll an actor run until it finishes. Returns final status."""
        url = f"{APIFY_API_BASE}/actor-runs/{run_id}"
        params = {"token": self.api_token}

        for _ in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            try:
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json().get("data", {})
                status = data.get("status", "UNKNOWN")

                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    return status

            except httpx.HTTPError as e:
                logger.warning(
                    f"[ApifyGMaps] Poll error for run {run_id}: {e}"
                )

        logger.error(
            f"[ApifyGMaps] Run {run_id} timed out after "
            f"{MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS}s"
        )
        APIFY_API_ERRORS_TOTAL.labels(
            endpoint=endpoint_label, error_type="timeout"
        ).inc()
        return "TIMED-OUT"

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
