"""Apify Instagram scraper client for venue Instagram discovery.

Uses the Apify REST API directly (not the apify-client SDK) to stay
consistent with the existing httpx-based API client pattern.

Key insight: the search scraper already returns full profile data
(username, biography, followersCount, isBusinessAccount, etc.)
so we validate directly from search results without a separate
profile scraper call — halving API costs.
"""
import logging
import time
from typing import Optional
import httpx

from app.models.instagram import InstagramProfile
from app.metrics import (
    APIFY_API_CALLS_TOTAL,
    APIFY_API_CALL_DURATION_SECONDS,
    APIFY_API_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"

# Actor IDs (use ~ separator for Apify REST API path)
SEARCH_ACTOR = "apify~instagram-search-scraper"
PROFILE_ACTOR = "apify~instagram-profile-scraper"


class ApifyCreditExhaustedError(Exception):
    """Raised when Apify returns 402 (payment required)."""
    pass


class ApifyInstagramClient:
    """Async HTTP client for Apify Instagram scraper actors."""

    def __init__(self, api_token: str, timeout: float = 120.0):
        self.api_token = api_token
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def search_users(
        self, query: str, results_limit: int = 5
    ) -> list[InstagramProfile]:
        """Search Instagram for user profiles matching a query.

        Uses apify/instagram-search-scraper actor. The search results already
        contain full profile data, so we return InstagramProfile objects
        directly — no need for a separate profile scraper call.

        Args:
            query: Search string (e.g., "Bar Conchittas Recife")
            results_limit: Max results to return

        Returns:
            List of InstagramProfile (with full profile data from search)
        """
        run_input = {
            "search": query,
            "searchType": "user",
            "resultsLimit": results_limit,
        }

        items = await self._run_actor_sync(
            SEARCH_ACTOR, run_input, endpoint_label="search_users"
        )

        if not items:
            return []

        results = []
        for item in items:
            # Skip error items (e.g., {"error": "no_items", ...})
            if "error" in item:
                logger.debug(
                    f"[ApifyInstagram] Skipping error item: {item.get('error')}"
                )
                continue

            username = item.get("username", "")
            if not username:
                logger.debug("[ApifyInstagram] Skipping item with empty username")
                continue

            try:
                # externalUrls is an array in Apify response
                external_urls = item.get("externalUrls") or []
                external_url = external_urls[0] if external_urls else None

                results.append(InstagramProfile(
                    username=username,
                    full_name=item.get("fullName"),
                    biography=item.get("biography"),
                    external_url=external_url,
                    followers_count=item.get("followersCount"),
                    following_count=item.get("followsCount"),
                    is_business_account=item.get("isBusinessAccount"),
                    business_category_name=item.get("businessCategoryName"),
                    is_verified=item.get("verified"),
                ))
            except Exception as e:
                logger.warning(f"[ApifyInstagram] Failed to parse search result: {e}")
                continue

        return results

    async def get_profile(self, username: str) -> Optional[InstagramProfile]:
        """Get full profile data for a single Instagram user.

        Uses apify/instagram-profile-scraper actor.
        Only needed as a fallback — search_users() already returns full profile data.

        Args:
            username: Instagram username (without @)

        Returns:
            InstagramProfile or None on error
        """
        run_input = {
            "usernames": [username],
        }

        items = await self._run_actor_sync(
            PROFILE_ACTOR, run_input, endpoint_label="get_profile"
        )

        if not items or len(items) == 0:
            return None

        item = items[0]

        # Skip error items
        if "error" in item:
            logger.debug(
                f"[ApifyInstagram] Profile error for @{username}: {item.get('error')}"
            )
            return None

        try:
            # externalUrls is an array in Apify response
            external_urls = item.get("externalUrls") or []
            external_url = external_urls[0] if external_urls else None

            return InstagramProfile(
                username=item.get("username", username),
                full_name=item.get("fullName"),
                biography=item.get("biography"),
                external_url=external_url,
                followers_count=item.get("followersCount"),
                following_count=item.get("followsCount"),
                is_business_account=item.get("isBusinessAccount"),
                business_category_name=item.get("businessCategoryName"),
                is_verified=item.get("verified"),
            )
        except Exception as e:
            logger.error(f"[ApifyInstagram] Failed to parse profile for @{username}: {e}")
            return None

    async def fetch_recent_posts(
        self, username: str, results_limit: int = 10
    ) -> list[dict]:
        """Fetch recent posts for an Instagram profile.

        Uses apify/instagram-scraper with resultsType="posts".
        Only returns caption text + engagement metrics (no image URLs — they expire).

        Args:
            username: Instagram username (without @)
            results_limit: Max posts to return (default 10)

        Returns:
            List of post dicts with keys: caption, likes_count, comments_count,
            timestamp, post_type. Empty list on error.
        """
        run_input = {
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": "posts",
            "resultsLimit": results_limit,
        }

        items = await self._run_actor_sync(
            "apify~instagram-scraper", run_input, endpoint_label="instagram_posts"
        )

        if not items:
            return []

        posts = []
        for item in items:
            if "error" in item:
                continue
            posts.append({
                "caption": item.get("caption", ""),
                "likes_count": item.get("likesCount", 0),
                "comments_count": item.get("commentsCount", 0),
                "timestamp": item.get("timestamp", ""),
                "post_type": item.get("type", "image"),
            })

        logger.info(
            f"[ApifyInstagram] Fetched {len(posts)} posts for @{username}"
        )
        return posts

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
            raise  # Re-raise so enrichment service can handle it

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
                f"[ApifyInstagram] HTTP error for {endpoint_label}: "
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
            logger.error(f"[ApifyInstagram] Timeout for {endpoint_label}")
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
            logger.error(f"[ApifyInstagram] Request error for {endpoint_label}: {e}")
            return None
