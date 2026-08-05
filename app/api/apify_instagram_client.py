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
    INSTAGRAM_SEARCH_CANDIDATES_DROPPED_TOTAL,
)

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"

# Actor IDs (use ~ separator for Apify REST API path)
SEARCH_ACTOR = "apify~instagram-search-scraper"

# Keys Apify has used for the URL inside an externalUrls entry, most specific
# first. `lynx_url` is the outbound wrapper Instagram itself serves.
_URL_KEYS = ("lynx_url", "url", "external_url", "href")


def _count_dropped(reason: str) -> None:
    """Discarding a search result must be COUNTED, not only logged.

    A silent drop is what let a total outage read as "no results found": when
    the payload shape changed, every linked profile failed validation and the
    only trace was a per-profile WARNING nobody was watching.
    """
    try:
        INSTAGRAM_SEARCH_CANDIDATES_DROPPED_TOTAL.labels(reason=reason).inc()
    except Exception:  # pragma: no cover - instrumentation must never raise
        pass


# Keys Apify's instagram-scraper has documented for a carousel post's child
# images, most specific first. Unverified against a live response — no
# APIFY_API_TOKEN in this environment — so every documented shape is tried
# rather than betting on one, the same tolerance `_external_url` applies to
# `externalUrls`.
_CHILD_KEYS = ("childPosts", "sidecarChildren", "children")


def _child_display_urls(item: dict) -> list[str]:
    """Every child image url of a carousel post, or [] for a single image."""
    for key in _CHILD_KEYS:
        children = item.get(key)
        if isinstance(children, list) and children:
            urls = [
                url for child in children
                if isinstance(child, dict)
                for url in (child.get("displayUrl") or child.get("display_url"),)
                if isinstance(url, str) and url
            ]
            if urls:
                return urls
    return []


def _post_image_urls(item: dict) -> list[str]:
    """Every archivable image for one post, main image first.

    A carousel's top-level `displayUrl` mirrors its first child, so when
    children are present they are the WHOLE list — using both would archive
    the cover twice under two different photo ids. A single-image post has no
    children, so its `displayUrl` is the only entry.
    """
    children = _child_display_urls(item)
    if children:
        return children
    main = item.get("displayUrl")
    return [main] if isinstance(main, str) and main else []


def _external_url(entry) -> Optional[str]:
    """The URL out of one `externalUrls` entry, whatever shape it arrives in.

    Apify changed this from a bare string to an object
    (`{title, lynx_url, link_type}`) with no notice. The model keeps its simple
    `Optional[str]` contract; the tolerance lives here, at the edge where
    foreign data lands. An entry we cannot read yields None — a link we can't
    parse is not a reason to throw the whole profile away.
    """
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        for key in _URL_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    return None


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
                _count_dropped("error_item")
                continue

            username = item.get("username", "")
            if not username:
                logger.debug("[ApifyInstagram] Skipping item with empty username")
                _count_dropped("no_username")
                continue

            try:
                # externalUrls is an array in Apify response
                external_urls = item.get("externalUrls") or []
                external_url = _external_url(external_urls[0]) if external_urls else None

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
                _count_dropped("parse_error")
                continue

        return results

    async def fetch_recent_posts(
        self, username: str, results_limit: int = 10
    ) -> list[dict]:
        """Fetch recent posts for an Instagram profile.

        Uses apify/instagram-scraper with resultsType="posts". Returns caption
        text, engagement metrics, AND the post's image urls — the media archive
        pipeline is what needed these; `InstagramPostsEnrichmentService` reads
        only the caption-era keys and keeps working unchanged.

        The image urls are signed with a short-lived Instagram CDN signature:
        they are only good within the run that fetched them, and a caller that
        stores this dict for later must download them in the SAME run, not
        read them back from a cache.

        Args:
            username: Instagram username (without @)
            results_limit: Max posts to return (default 10)

        Returns:
            List of post dicts with keys: caption, likes_count, comments_count,
            timestamp, post_type, shortcode, permalink, image_urls. Empty list
            on error.
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
                "shortcode": item.get("shortCode") or None,
                "permalink": item.get("url") or None,
                "image_urls": _post_image_urls(item),
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
