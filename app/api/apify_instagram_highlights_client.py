"""Apify Instagram Scraper client for fetching venue highlights/stories.

Uses the apify/instagram-scraper actor to fetch highlight reels
for a given Instagram username. Filters highlights by title
(Menu, Cardapio, Precos, etc.) and returns story image URLs.

Uses the same httpx-based pattern as apify_instagram_client.py.
"""
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

# Actor for Instagram scraping (supports highlights/stories)
HIGHLIGHTS_ACTOR = "apify~instagram-scraper"

# Default keywords to match against highlight titles.
# Uses short stems so natural variations are caught via substring match
# after accent-stripping + lowercasing:
#   "drink" → drinks, drinques, drinqs
#   "bebid" → bebida, bebidas
#   "comid" → comida, comidas
#   "comes" → comes (Recife slang for food)
#   "entrada" → entradas
#   "aperitiv" → aperitivo, aperitivos
MENU_HIGHLIGHT_KEYWORDS = [
    # Menu / cardápio
    "menu", "cardapio",
    # Prices
    "preco", "valor",
    # Drinks
    "drink", "drinq", "bebid", "bebe",
    # Food
    "comid", "comes", "prato",
    # Starters / appetizers
    "entrada", "aperitiv", "petisco",
    # Portions / combos
    "porcao", "combo",
]


def _normalize(text: str) -> str:
    """Strip accents and lowercase for comparison."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


class ApifyInstagramHighlightsClient:
    """Async HTTP client for fetching Instagram highlights via apify/instagram-scraper."""

    def __init__(self, api_token: str, timeout: float = 180.0):
        self.api_token = api_token
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def fetch_menu_highlights(
        self,
        username: str,
        menu_keywords: Optional[list[str]] = None,
    ) -> list[dict]:
        """Fetch story images from menu-related highlight reels.

        Uses apify/instagram-scraper to get all highlights for a profile,
        then filters to those whose title matches menu-related keywords.

        Args:
            username: Instagram username (without @)
            menu_keywords: Highlight title keywords to match (case/accent insensitive).
                           Defaults to MENU_HIGHLIGHT_KEYWORDS.

        Returns:
            List of dicts: [{"image_url": "...", "highlight_title": "...", "timestamp": "..."}]
            Empty list if no matching highlights found.
        """
        keywords = menu_keywords or MENU_HIGHLIGHT_KEYWORDS
        normalized_keywords = [_normalize(kw) for kw in keywords]

        run_input = {
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": "stories",
            "resultsLimit": 200,
        }

        items = await self._run_actor_sync(
            HIGHLIGHTS_ACTOR, run_input, endpoint_label="instagram_highlights"
        )

        if not items:
            logger.info(
                f"[ApifyIGHighlights] No items returned for @{username}"
            )
            return []

        return self._filter_menu_highlights(items, normalized_keywords)

    def _filter_menu_highlights(
        self, items: list[dict], normalized_keywords: list[str]
    ) -> list[dict]:
        """Filter story items to only those from menu-related highlights.

        The apify/instagram-scraper returns items with fields like:
        - highlightTitle: title of the highlight reel
        - imageUrl: URL of the story image (None for video-only stories)
        - videoUrl: URL of the story video
        - timestamp: ISO timestamp
        - ownerUsername: profile username

        We only return items that:
        1. Have a highlightTitle matching our keywords
        2. Have an imageUrl (skip video-only stories)
        """
        results = []

        for item in items:
            highlight_title = item.get("highlightTitle") or ""
            if not highlight_title:
                continue

            normalized_title = _normalize(highlight_title)

            # Check if any keyword appears in the title
            matched = any(kw in normalized_title for kw in normalized_keywords)
            if not matched:
                continue

            # Only take items with an image URL (skip video-only)
            image_url = item.get("imageUrl")
            if not image_url:
                continue

            results.append({
                "image_url": image_url,
                "highlight_title": highlight_title,
                "timestamp": item.get("timestamp", ""),
            })

        if results:
            titles = set(r["highlight_title"] for r in results)
            logger.info(
                f"[ApifyIGHighlights] Found {len(results)} images from "
                f"highlights: {titles}"
            )
        else:
            logger.debug(
                f"[ApifyIGHighlights] No menu highlights found in "
                f"{len(items)} items"
            )

        return results

    async def _run_actor_sync(
        self, actor_id: str, run_input: dict, endpoint_label: str
    ) -> Optional[list[dict]]:
        """Run an Apify actor synchronously and return dataset items.

        Uses the run-sync-get-dataset-items endpoint for simplicity.
        """
        url = f"{APIFY_API_BASE}/acts/{actor_id}/run-sync-get-dataset-items"
        params = {"token": self.api_token}

        start_time = time.perf_counter()
        try:
            response = await self.client.post(
                url, params=params, json=run_input
            )

            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(duration)

            if response.status_code == 402:
                APIFY_API_ERRORS_TOTAL.labels(
                    endpoint=endpoint_label, error_type="credit_exhausted"
                ).inc()
                raise ApifyCreditExhaustedError("Apify credits exhausted (402)")

            response.raise_for_status()
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="success"
            ).inc()

            return response.json()

        except ApifyCreditExhaustedError:
            raise

        except httpx.HTTPStatusError as e:
            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(duration)
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="error"
            ).inc()
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="http_error"
            ).inc()
            logger.error(
                f"[ApifyIGHighlights] HTTP error for {endpoint_label}: "
                f"{e.response.status_code} {e.response.text[:200]}"
            )
            return None

        except httpx.TimeoutException:
            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(duration)
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="timeout"
            ).inc()
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="timeout"
            ).inc()
            logger.error(f"[ApifyIGHighlights] Timeout for {endpoint_label}")
            return None

        except httpx.RequestError as e:
            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(duration)
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="error"
            ).inc()
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="connection_error"
            ).inc()
            logger.error(f"[ApifyIGHighlights] Request error for {endpoint_label}: {e}")
            return None
