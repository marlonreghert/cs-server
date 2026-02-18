"""SearchApi.io client for Google Maps photo discovery with category filtering.

Uses the SearchApi.io google_maps_photos engine which supports:
- place_id parameter (no need to resolve data_id first)
- category_id filtering for menu-specific photos
- Language-aware category names (hl parameter)

This enables fetching menu-specific photos from Google Maps, which the
official Google Places API does not support (it only returns top 10 generic photos).
"""
import logging
import time
import unicodedata
from typing import Optional

import httpx

from app.metrics import (
    SERPAPI_API_CALLS_TOTAL,
    SERPAPI_API_CALL_DURATION_SECONDS,
    SERPAPI_API_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)

SEARCHAPI_BASE_URL = "https://www.searchapi.io/api/v1/search"


class SerpApiClient:
    """Async HTTP client for SearchApi.io Google Maps endpoints."""

    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def resolve_data_id(self, place_id: str) -> Optional[str]:
        """Convert a Google place_id to a data_id (hex CID format).

        Uses the google_maps_photos engine with place_id — extracts data_id
        from search_parameters if available. This is a fallback; prefer
        passing place_id directly to fetch_photos().

        Args:
            place_id: Google Place ID (e.g., "ChIJ5TEfXPgYqwcRlUZcJGThvAc")

        Returns:
            data_id string (e.g., "0x...:0x..."), or None on failure.
        """
        params = {
            "engine": "google_maps_photos",
            "place_id": place_id,
        }

        start_time = time.perf_counter()
        endpoint = "resolve_data_id"

        try:
            response = await self.client.get(SEARCHAPI_BASE_URL, params=params)

            if response.status_code == 429:
                SERPAPI_API_ERRORS_TOTAL.labels(
                    endpoint=endpoint, error_type="quota_exceeded"
                ).inc()
                logger.error("[SearchApi] Rate limit exceeded (429)")
                return None

            response.raise_for_status()

            duration = time.perf_counter() - start_time
            SERPAPI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            SERPAPI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="success").inc()

            data = response.json()

            # SearchApi may include data_id in search_parameters
            search_params = data.get("search_parameters", {})
            data_id = search_params.get("data_id")

            if data_id:
                logger.info(
                    f"[SearchApi] Resolved place_id {place_id} → data_id {data_id}"
                )
                return data_id

            logger.warning(
                f"[SearchApi] No data_id found for place_id {place_id}"
            )
            return None

        except httpx.HTTPStatusError as e:
            duration = time.perf_counter() - start_time
            SERPAPI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            SERPAPI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            SERPAPI_API_ERRORS_TOTAL.labels(
                endpoint=endpoint, error_type="http_error"
            ).inc()
            logger.error(
                f"[SearchApi] HTTP error resolving data_id for {place_id}: {e}"
            )
            return None

        except Exception as e:
            duration = time.perf_counter() - start_time
            SERPAPI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            SERPAPI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            logger.error(
                f"[SearchApi] Error resolving data_id for {place_id}: {e}"
            )
            return None

    async def fetch_photos(
        self,
        data_id: Optional[str] = None,
        place_id: Optional[str] = None,
        category_id: Optional[str] = None,
        hl: str = "pt-BR",
    ) -> Optional[dict]:
        """Fetch photos from SearchApi.io google_maps_photos engine.

        Pass either data_id or place_id (place_id is preferred as it
        skips the resolve step).

        Args:
            data_id: Hex CID format data_id (e.g., "0x...:0x...")
            place_id: Google Place ID (e.g., "ChIJ...")
            category_id: Optional category filter (e.g., "CgIYIQ" for Menu)
            hl: Language code for category names

        Returns:
            Dict with keys:
                - photos: list[dict] with {thumbnail, image}
                - categories: list[dict] with {id, title}
            or None on error.
        """
        if not data_id and not place_id:
            logger.error("[SearchApi] fetch_photos requires data_id or place_id")
            return None

        params = {
            "engine": "google_maps_photos",
            "hl": hl,
        }
        if place_id:
            params["place_id"] = place_id
        elif data_id:
            params["data_id"] = data_id
        if category_id:
            params["category_id"] = category_id

        start_time = time.perf_counter()
        endpoint = "fetch_photos"

        try:
            response = await self.client.get(SEARCHAPI_BASE_URL, params=params)

            if response.status_code == 429:
                SERPAPI_API_ERRORS_TOTAL.labels(
                    endpoint=endpoint, error_type="quota_exceeded"
                ).inc()
                logger.error("[SearchApi] Rate limit exceeded (429)")
                return None

            response.raise_for_status()

            duration = time.perf_counter() - start_time
            SERPAPI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            SERPAPI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="success").inc()

            data = response.json()

            photos = data.get("photos", [])
            categories = data.get("categories", [])

            id_label = place_id or data_id
            cat_label = f" (category={category_id})" if category_id else ""
            logger.info(
                f"[SearchApi] Fetched {len(photos)} photos for {id_label}{cat_label}, "
                f"{len(categories)} categories available"
            )

            return {
                "photos": photos,
                "categories": categories,
            }

        except httpx.HTTPStatusError as e:
            duration = time.perf_counter() - start_time
            SERPAPI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            SERPAPI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            SERPAPI_API_ERRORS_TOTAL.labels(
                endpoint=endpoint, error_type="http_error"
            ).inc()
            id_label = place_id or data_id
            logger.error(
                f"[SearchApi] HTTP error fetching photos for {id_label}: {e}"
            )
            return None

        except Exception as e:
            duration = time.perf_counter() - start_time
            SERPAPI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            SERPAPI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            id_label = place_id or data_id
            logger.error(
                f"[SearchApi] Error fetching photos for {id_label}: {e}"
            )
            return None

    @staticmethod
    def find_menu_category(
        categories: list[dict], menu_keywords: list[str]
    ) -> Optional[str]:
        """Find a menu/cardápio category ID from the available categories.

        Performs accent-insensitive, case-insensitive matching.

        Args:
            categories: [{id: "CgIYIQ", title: "Menu"}, ...]
            menu_keywords: ["menu", "cardápio", "cardapio", "preços", "valores"]

        Returns:
            Category ID string if a menu category is found, None otherwise.
        """

        def strip_accents(s: str) -> str:
            return "".join(
                c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn"
            )

        normalized_keywords = [strip_accents(k.lower()) for k in menu_keywords]

        for cat in categories:
            title = cat.get("title", "")
            normalized_title = strip_accents(title.lower())
            if normalized_title in normalized_keywords:
                logger.info(
                    f"[SearchApi] Found menu category: '{title}' (id={cat.get('id')})"
                )
                return cat.get("id")

        return None
