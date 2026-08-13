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
from dataclasses import dataclass, field
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


class ApifyTransportFailure(Exception):
    """Raised INTERNALLY by `_run_actor_sync` when the actor call fails at
    the HTTP/transport layer (a timeout, a non-2xx response, or a connection
    error) -- never for an Apify-issued dataset error item, which
    `_run_actor_sync` never inspects (it only returns the raw decoded JSON
    body). `code` is one of `"timeout"`/`"http_error"`/`"request_error"` --
    the values plans/260813_crawl-transport-failure-visibility.md §A defines
    on `FetchPostsResult.error_code`, chosen so they can never collide with
    an Apify-issued code (`no_items`, `not_found`, ...) -- see
    `FetchPostsResult`'s own docstring.

    Caught immediately by BOTH of `_run_actor_sync`'s callers (`search_
    users`, `fetch_recent_posts`), just below where each already calls it --
    this exception never escapes this module. Raising here (rather than the
    OLD contract of returning `None`) is what lets the failure TYPE survive
    at all: a bare `None` return could not carry which of the three
    transport failures happened, and every caller's own `if not items:`
    check could not tell that `None` apart from a genuinely empty dataset
    (`[]`) either -- both are falsy. `search_users` degrades exactly as it
    did before (a transport failure yields no search results, same as
    today); `fetch_recent_posts` is the one caller that now reads `.code`."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FetchPostsResult:
    """`fetch_recent_posts`'s return shape (plans/260812_crawl-error-
    visibility.md §A). `posts` never includes an Apify error item — that
    item's own fields surface separately here instead of being discarded by
    a bare `continue`, so a caller that needs to (today, only
    `instagram_crawl_service._run_stream`) can tell a blocked scrape or a
    non-existent handle apart from a genuinely empty result. Every other
    existing caller (`archive_sources._fetch_instagram`,
    `InstagramPostsEnrichmentService.enrich_all_venues`,
    `ApifyPromoterPostsClient.fetch_recent_posts`) reads only `.posts` and
    is otherwise unaffected.

    `error_code` is EITHER Apify's own `error` value verbatim (e.g.
    `"not_found"`, `"no_items"` — Apify owns these), OR one of
    `"timeout"`/`"http_error"`/`"request_error"` when `_run_actor_sync`
    itself failed at the transport layer instead of Apify ever running (this
    client owns these — plans/260813_crawl-transport-failure-visibility.md
    §A). The two families can never collide (Apify has never issued, and is
    not expected to issue, any of the three transport strings). `None` means
    the dataset carried no error item AND the call itself succeeded — the
    common case, and the ONLY value that means "no error at all". Before
    260813, a transport failure collapsed into `posts=[], error_code=None`,
    indistinguishable from a genuinely empty account.
    `error_description` is Apify's own `errorDescription` prose (always
    `None` for a transport failure, which has no Apify dataset item to read
    one from), carried through for logging ONLY: `not_found` and a
    genuinely-blocked `no_items` can share the exact same description text,
    so control flow must never branch on it (see the module's `fetch_
    recent_posts` docstring). `request_error_count` is `len(requestError
    Messages)` on the error item (always 0 for a transport failure) — the
    one signal, alongside `error_code`, that tells a real Instagram block (a
    non-empty count) apart from a genuinely empty/private stream (zero) when
    `error_code == "no_items"`.
    """

    posts: list = field(default_factory=list)
    error_code: Optional[str] = None
    error_description: Optional[str] = None
    request_error_count: int = 0


class ApifyInstagramClient:
    """Async HTTP client for Apify Instagram scraper actors."""

    # plans/260813_crawl-transport-failure-visibility.md §D: 120s abandoned a
    # run that had ALREADY billed results server-side and succeeded --
    # 2026-08-13, downtownbeergarden_ (121 posts, a 20-post seed): Apify's
    # run succeeded with 16 items, our client gave up two minutes in and
    # threw the answer away. 300s is the starting proposal, comfortably past
    # the slowest OBSERVED success on 2026-08-12 (a few seconds to just over
    # a minute) -- container.py wires the real, admin-tunable value from
    # `settings.apify_instagram_client_timeout_seconds`; this default only
    # matters for a caller that constructs the client directly (tests, or a
    # future script), and must not silently regress back to the value that
    # caused the incident.
    def __init__(self, api_token: str, timeout: float = 300.0):
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

        try:
            items = await self._run_actor_sync(
                SEARCH_ACTOR, run_input, endpoint_label="search_users"
            )
        except ApifyTransportFailure:
            # Degrades exactly as a transport failure always has for this
            # caller (it never surfaced a distinction to begin with) — only
            # `fetch_recent_posts` below reads the failure's TYPE (plans/
            # 260813_crawl-transport-failure-visibility.md §A).
            return []

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
        self,
        username: str,
        results_limit: int = 10,
        *,
        only_posts_newer_than: Optional[str] = None,
        results_type: str = "posts",
    ) -> FetchPostsResult:
        """Fetch recent posts (or reels) for an Instagram profile.

        Uses apify/instagram-scraper. `resultsType` defaults to "posts" (the
        original, unchanged behaviour); passing "reels" runs the SAME actor
        against the account's reels tab instead — a separate billed run, not
        a flag on the posts one (plans/260809_scheduled-incremental-
        instagram-crawl.md §Evidence). Verified against a live run (that
        plan's Step 0 probe, @entreamigos.praia, resultsLimit=1): reel items
        carry the identical shape posts do (including `timestamp`), so the
        item-to-dict mapping below needs no reels-specific branch.

        `only_posts_newer_than` is Apify's own `onlyPostsNewerThan` filter —
        `YYYY-MM-DD`, ISO-8601, or a relative string ("1 day", "2 months").
        Its docs say times are interpreted in UTC, not local time; verified
        for `resultsType="reels"` specifically by the same Step 0 probe (the
        field is named for posts and documented generically, so this was not
        assumed). None (the default) omits the filter entirely, which is the
        pre-existing, unbounded behaviour every caller of this method got
        before this parameter existed — so the three existing callers
        (`archive_sources._fetch_instagram`, `ApifyPromoterPostsClient.
        fetch_recent_posts`, `InstagramPostsEnrichmentService.
        enrich_all_venues`) are UNAFFECTED until they explicitly opt in.

        Pinned posts can appear even when `only_posts_newer_than` is set
        (Apify's own documented caveat, reconfirmed by the Step 0 probe) — a
        pinned item arrives billed and must be filtered by the CALLER after
        the fact, never assumed absent here.

        The image urls are signed with a short-lived Instagram CDN signature:
        they are only good within the run that fetched them, and a caller that
        stores this dict for later must download them in the SAME run, not
        read them back from a cache.

        Args:
            username: Instagram username (without @)
            results_limit: Max posts to return (default 10)
            only_posts_newer_than: Apify's `onlyPostsNewerThan` filter value,
                or None to omit it (fetch without a lower bound).
            results_type: "posts" (default) or "reels".

        Returns:
            FetchPostsResult — `.posts` is a list of post dicts with keys:
            caption, likes_count, comments_count, timestamp, post_type,
            shortcode, permalink, image_urls, is_pinned; `.error_code`/
            `.error_description`/`.request_error_count` surface an Apify
            error item instead of silently discarding it (plans/260812_
            crawl-error-visibility.md §A) — see FetchPostsResult's own
            docstring for what each means and why control flow must never
            read `.error_description`'s text. `.posts` is ALSO `[]` on a
            transport-level failure, but as of plans/260813_crawl-transport-
            failure-visibility.md §A that case is no longer indistinguishable
            from a genuinely empty dataset: `.error_code` is `"timeout"`/
            `"http_error"`/`"request_error"` (never `None`) when
            `_run_actor_sync` raised `ApifyTransportFailure`, caught here.
        """
        run_input = {
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": results_type,
            "resultsLimit": results_limit,
        }
        if only_posts_newer_than:
            run_input["onlyPostsNewerThan"] = only_posts_newer_than

        try:
            items = await self._run_actor_sync(
                "apify~instagram-scraper", run_input, endpoint_label="instagram_posts",
                username=username,
            )
        except ApifyTransportFailure as e:
            # The defect plans/260813_crawl-transport-failure-visibility.md
            # §A exists to close: before this, `_run_actor_sync` returned
            # `None` here and fell straight into the SAME `if not items:`
            # branch below as a genuinely empty dataset, so `.error_code`
            # stayed `None` and a caller could never tell the two apart.
            return FetchPostsResult(posts=[], error_code=e.code)

        if not items:
            return FetchPostsResult(posts=[])

        posts = []
        # Apify's own error item — `{"error": "no_items", "errorDescription":
        # "...", "requestErrorMessages": [...]}` — carries no post fields, so
        # it is never appended to `posts`; its fields are captured here
        # instead of being discarded by a bare `continue` (the defect
        # plans/260812_crawl-error-visibility.md exists to fix). At most one
        # error item has ever been observed per dataset (see the plan's own
        # evidence table); if more than one arrives, the FIRST is kept —
        # never overwritten by a second, so a caller's classification is
        # deterministic regardless of dataset order.
        error_code: Optional[str] = None
        error_description: Optional[str] = None
        request_error_count = 0
        for item in items:
            if "error" in item:
                if error_code is None:
                    error_code = item.get("error")
                    error_description = item.get("errorDescription")
                    request_error_count = len(item.get("requestErrorMessages") or [])
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
                # Needed by the scheduled crawl (§G) to drop a pinned item
                # that bypassed `only_posts_newer_than` without ever letting
                # it move a cursor; every other existing caller ignores it.
                "is_pinned": bool(item.get("isPinned", False)),
            })

        logger.info(
            f"[ApifyInstagram] Fetched {len(posts)} {results_type} for @{username}"
        )
        if error_code is not None:
            # Logged HERE too (in addition to whatever the caller logs) so
            # the raw Apify fields are on record even for a caller that
            # never inspects them (archive_sources._fetch_instagram,
            # InstagramPostsEnrichmentService.enrich_all_venues,
            # ApifyPromoterPostsClient.fetch_recent_posts) — never a decision
            # point, only a trace. `errorDescription` is logged but never
            # matched on: Apify reuses the same prose for a genuinely-empty
            # stream and a blocked one (see FetchPostsResult's docstring).
            logger.warning(
                f"[ApifyInstagram] {username} {results_type}: Apify returned "
                f"error={error_code!r} request_error_count={request_error_count} "
                f"description={error_description!r}"
            )
        return FetchPostsResult(
            posts=posts, error_code=error_code, error_description=error_description,
            request_error_count=request_error_count,
        )

    async def _run_actor_sync(
        self, actor_id: str, run_input: dict, endpoint_label: str,
        *, username: Optional[str] = None,
    ) -> list[dict]:
        """Run an Apify actor synchronously and return dataset items.

        Uses the run-sync-get-dataset-items endpoint for simplicity.

        Returns the raw dataset items on success (an empty list IS a valid,
        genuinely-empty success — never confused with failure). Raises
        `ApifyCreditExhaustedError` on a 402, or `ApifyTransportFailure` for
        a timeout/non-2xx response/connection error (plans/260813_crawl-
        transport-failure-visibility.md §A) — before this plan, those three
        cases returned `None` instead, which every caller's own `if not
        items:` check could not distinguish from a genuinely empty dataset
        (`[]`, also falsy). Raising instead of returning a sentinel is what
        lets the failure's TYPE reach `fetch_recent_posts`.

        `username` (plan §C) is included in the timeout/HTTP-error/request-
        error log lines below when the caller has one to give — ONLY `fetch_
        recent_posts` does; `search_users` has no single handle (a search
        query can match many) and passes nothing, so those log lines stay
        exactly as they were. Without this, `endpoint_label` alone
        (`instagram_posts`, identical for every target) cannot be attributed
        to a handle from logs — the exact gap that cost a full diagnostic
        round trip on 2026-08-13 (downtownbeergarden_'s timeout was sitting
        in the logs, unattributable, while grepping by handle returned
        nothing).
        """
        url = f"{APIFY_API_BASE}/acts/{actor_id}/run-sync-get-dataset-items"
        params = {"token": self.api_token}
        # Appended verbatim to each transport-error log line below; empty
        # when the caller has no single handle to name (`search_users`).
        who = f" (@{username})" if username else ""

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
                f"[ApifyInstagram] HTTP error for {endpoint_label}{who}: "
                f"{e.response.status_code} {e.response.text[:200]}"
            )
            raise ApifyTransportFailure("http_error") from e

        except httpx.TimeoutException as e:
            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(duration)
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="timeout"
            ).inc()
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="timeout"
            ).inc()
            logger.error(f"[ApifyInstagram] Timeout for {endpoint_label}{who}")
            raise ApifyTransportFailure("timeout") from e

        except httpx.RequestError as e:
            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(duration)
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="error"
            ).inc()
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="connection_error"
            ).inc()
            logger.error(f"[ApifyInstagram] Request error for {endpoint_label}{who}: {e}")
            raise ApifyTransportFailure("request_error") from e
