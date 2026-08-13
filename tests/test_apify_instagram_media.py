"""Unit tests for the media fields `fetch_recent_posts` gained for the media
archive pipeline: `shortcode`, `permalink`, and `image_urls`.

The exact Apify key for a carousel's child images (`childPosts[].displayUrl`,
per the actor's own docs) is UNVERIFIED against a live response — there is no
APIFY_API_TOKEN in this environment. These tests pin the tolerant parsing
instead: every documented shape is tried, the same tolerance `_external_url`
already applies to `externalUrls`, and a shape the parser cannot read yields
an empty list rather than raising.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.api.apify_instagram_client import ApifyInstagramClient


def _client_with_items(items):
    client = ApifyInstagramClient(api_token="t")

    async def _fake_run_actor_sync(actor_id, run_input, endpoint_label):
        return items

    client._run_actor_sync = _fake_run_actor_sync
    return client


def _fetch(items, results_limit=10):
    client = _client_with_items(items)
    result = asyncio.run(client.fetch_recent_posts("somehandle", results_limit=results_limit))
    return result.posts


class TestSingleImagePost:
    def test_captures_shortcode_permalink_and_the_display_url(self):
        posts = _fetch([{
            "caption": "hi", "likesCount": 3, "commentsCount": 1,
            "timestamp": "2026-08-01T00:00:00.000Z", "type": "image",
            "shortCode": "abc123", "url": "https://instagram.com/p/abc123/",
            "displayUrl": "https://instagram.cdn.example/abc123.jpg",
        }])
        assert len(posts) == 1
        post = posts[0]
        assert post["shortcode"] == "abc123"
        assert post["permalink"] == "https://instagram.com/p/abc123/"
        assert post["image_urls"] == ["https://instagram.cdn.example/abc123.jpg"]
        # The caption-era keys must still be there — InstagramPostsEnrichmentService
        # reads only these and must keep working untouched.
        assert post["caption"] == "hi"
        assert post["likes_count"] == 3
        assert post["comments_count"] == 1
        assert post["post_type"] == "image"

    def test_missing_shortcode_and_url_degrade_to_none_not_a_crash(self):
        posts = _fetch([{"caption": "", "displayUrl": "https://x/y.jpg"}])
        assert posts[0]["shortcode"] is None
        assert posts[0]["permalink"] is None
        assert posts[0]["image_urls"] == ["https://x/y.jpg"]


class TestCarouselPost:
    def test_child_posts_become_the_full_image_list(self):
        posts = _fetch([{
            "shortCode": "car1", "type": "carousel",
            "displayUrl": "https://instagram.cdn.example/car1_cover.jpg",
            "childPosts": [
                {"displayUrl": "https://instagram.cdn.example/car1_0.jpg"},
                {"displayUrl": "https://instagram.cdn.example/car1_1.jpg"},
                {"displayUrl": "https://instagram.cdn.example/car1_2.jpg"},
            ],
        }])
        # The top-level displayUrl mirrors the first child on a real carousel
        # post; using both would archive the cover twice under two ids, so
        # children alone are the whole list when they are present.
        assert posts[0]["image_urls"] == [
            "https://instagram.cdn.example/car1_0.jpg",
            "https://instagram.cdn.example/car1_1.jpg",
            "https://instagram.cdn.example/car1_2.jpg",
        ]

    def test_an_alternate_documented_children_key_is_tolerated(self):
        # Unverified against a live response, so more than one documented
        # shape is tried — the same tolerance `_external_url` applies.
        posts = _fetch([{
            "shortCode": "car2",
            "sidecarChildren": [
                {"displayUrl": "https://x/a.jpg"},
                {"display_url": "https://x/b.jpg"},
            ],
        }])
        assert posts[0]["image_urls"] == ["https://x/a.jpg", "https://x/b.jpg"]

    def test_a_child_missing_display_url_is_dropped_not_a_blank_entry(self):
        posts = _fetch([{
            "shortCode": "car3",
            "childPosts": [
                {"displayUrl": "https://x/a.jpg"},
                {"caption": "no display url here"},
                {"displayUrl": ""},
            ],
        }])
        assert posts[0]["image_urls"] == ["https://x/a.jpg"]


class TestVideoOrUnreadablePost:
    def test_a_video_post_with_no_display_url_yields_no_images(self):
        posts = _fetch([{
            "shortCode": "vid1", "type": "video", "caption": "watch this",
        }])
        assert posts[0]["image_urls"] == []
        assert posts[0]["post_type"] == "video"

    def test_an_item_missing_display_url_entirely_does_not_raise(self):
        posts = _fetch([{"caption": "no media fields at all"}])
        assert len(posts) == 1
        assert posts[0]["image_urls"] == []

    def test_an_error_item_is_skipped(self):
        posts = _fetch([
            {"error": "no_items"},
            {"shortCode": "ok1", "displayUrl": "https://x/ok1.jpg"},
        ])
        assert len(posts) == 1
        assert posts[0]["shortcode"] == "ok1"


class TestResultsLimit:
    def test_results_limit_is_forwarded_to_the_actor_request(self):
        # The actor itself bounds how many posts come back; the client's job
        # is to ask for the right number, not to re-slice the response.
        seen = {}

        client = ApifyInstagramClient(api_token="t")

        async def _fake_run_actor_sync(actor_id, run_input, endpoint_label):
            seen["resultsLimit"] = run_input.get("resultsLimit")
            return []

        client._run_actor_sync = _fake_run_actor_sync
        asyncio.run(client.fetch_recent_posts("somehandle", results_limit=3))
        assert seen["resultsLimit"] == 3


class TestScheduledCrawlParameters:
    """plans/260809_scheduled-incremental-instagram-crawl.md: `only_posts_
    newer_than` and `results_type` default to the pre-existing, unbounded
    behaviour so the three existing callers stay unaffected until they opt
    in — pinned here at the wire-format level."""

    def test_defaults_omit_the_date_filter_and_request_posts(self):
        seen = {}

        client = ApifyInstagramClient(api_token="t")

        async def _fake_run_actor_sync(actor_id, run_input, endpoint_label):
            seen["run_input"] = run_input
            return []

        client._run_actor_sync = _fake_run_actor_sync
        asyncio.run(client.fetch_recent_posts("somehandle", results_limit=5))
        assert "onlyPostsNewerThan" not in seen["run_input"]
        assert seen["run_input"]["resultsType"] == "posts"

    def test_only_posts_newer_than_is_forwarded_verbatim(self):
        seen = {}

        client = ApifyInstagramClient(api_token="t")

        async def _fake_run_actor_sync(actor_id, run_input, endpoint_label):
            seen["run_input"] = run_input
            return []

        client._run_actor_sync = _fake_run_actor_sync
        asyncio.run(client.fetch_recent_posts(
            "somehandle", results_limit=5, only_posts_newer_than="2026-08-01T00:00:00Z",
        ))
        assert seen["run_input"]["onlyPostsNewerThan"] == "2026-08-01T00:00:00Z"

    def test_results_type_reels_is_forwarded_and_is_a_separate_run_from_posts(self):
        seen = {}

        client = ApifyInstagramClient(api_token="t")

        async def _fake_run_actor_sync(actor_id, run_input, endpoint_label):
            seen["run_input"] = run_input
            return []

        client._run_actor_sync = _fake_run_actor_sync
        asyncio.run(client.fetch_recent_posts("somehandle", results_limit=1, results_type="reels"))
        assert seen["run_input"]["resultsType"] == "reels"

    def test_is_pinned_is_read_from_the_raw_item(self):
        posts = _fetch([
            {"shortCode": "pinned1", "isPinned": True, "timestamp": "2023-01-01T00:00:00.000Z"},
            {"shortCode": "notpinned1", "timestamp": "2026-08-01T00:00:00.000Z"},
        ])
        assert posts[0]["is_pinned"] is True
        assert posts[1]["is_pinned"] is False


class TestErrorItemClassification:
    """plans/260812_crawl-error-visibility.md §A: the client no longer just
    drops an error item — it surfaces the fields a caller needs to tell a
    permanently-wrong handle, a transient block, and a genuinely empty
    stream apart. Classification itself (which of those three a given
    code/count combination MEANS) is `instagram_crawl_service._run_stream`'s
    job, not this client's — these tests pin only what the client reports."""

    def _fetch_result(self, items, results_limit=10):
        client = _client_with_items(items)
        return asyncio.run(client.fetch_recent_posts("somehandle", results_limit=results_limit))

    def test_not_found_error_item_is_reported(self):
        result = self._fetch_result([
            {"error": "not_found", "errorDescription": "Post does not exist"},
        ])
        assert result.posts == []
        assert result.error_code == "not_found"
        assert result.error_description == "Post does not exist"
        assert result.request_error_count == 0

    def test_no_items_with_request_errors_reports_the_count(self):
        result = self._fetch_result([{
            "error": "no_items",
            "errorDescription": "Empty or private data for provided input",
            "requestErrorMessages": [
                "Request blocked, retrying it again with different session",
            ] * 11,
        }])
        assert result.posts == []
        assert result.error_code == "no_items"
        assert result.request_error_count == 11

    def test_no_items_with_no_request_errors_reports_zero(self):
        result = self._fetch_result([{
            "error": "no_items",
            "errorDescription": "Empty or private data for provided input",
        }])
        assert result.posts == []
        assert result.error_code == "no_items"
        assert result.request_error_count == 0

    def test_an_unrecognised_error_code_is_still_reported_verbatim(self):
        """A future Apify error this client has never seen must not be
        silently swallowed — the caller decides what an unknown code means
        (transient failure, never success); this client's only job is to
        not lose it."""
        result = self._fetch_result([{"error": "some_new_code"}])
        assert result.posts == []
        assert result.error_code == "some_new_code"

    def test_a_dataset_with_no_error_item_reports_none(self):
        result = self._fetch_result([
            {"shortCode": "ok1", "displayUrl": "https://x/ok1.jpg"},
        ])
        assert result.error_code is None
        assert result.request_error_count == 0

    def test_a_mixed_dataset_keeps_both_the_posts_and_the_error(self):
        """The evidence this plan is built on: a dataset can carry real
        posts AND an error item together in the same response."""
        result = self._fetch_result([
            {"shortCode": "ok1", "displayUrl": "https://x/ok1.jpg"},
            {"shortCode": "ok2", "displayUrl": "https://x/ok2.jpg"},
            {"shortCode": "ok3", "displayUrl": "https://x/ok3.jpg"},
            {"error": "no_items"},
        ])
        assert len(result.posts) == 3
        assert {p["shortcode"] for p in result.posts} == {"ok1", "ok2", "ok3"}


class TestTransportFailureClassification:
    """plans/260813_crawl-transport-failure-visibility.md §A: the production
    defect this plan fixes lives entirely inside `_run_actor_sync`/`fetch_
    recent_posts` — before this plan, a timeout/HTTP error/connection error
    collapsed into `FetchPostsResult(posts=[], error_code=None)`,
    indistinguishable from a genuinely empty account (verified in production
    2026-08-13: downtownbeergarden_, 121 posts, Apify succeeded server-side
    with 16 results, our client timed out and the target read as healthy and
    empty).

    Unlike `TestErrorItemClassification` above (which monkeypatches `_run_
    actor_sync` itself and so can never exercise this bug), these tests
    mock only `self.client.post` — the TRUE external boundary — so the REAL
    `_run_actor_sync` exception handling actually runs. This is the layer
    that proves the fix; the BDD feature (crawl-transport-failure-
    visibility.feature) exercises `instagram_crawl_service`'s classification
    of whatever `.error_code` this client reports, one layer up, through a
    fake that can't see this bug either (see that feature's own step-module
    docstring)."""

    def _client(self) -> ApifyInstagramClient:
        return ApifyInstagramClient(api_token="t")

    def _fetch_with_post_raising(self, exc: Exception):
        client = self._client()

        async def _raising_post(*args, **kwargs):
            raise exc

        client.client.post = _raising_post
        return asyncio.run(client.fetch_recent_posts("somehandle", results_limit=10))

    def test_a_timeout_is_reported_as_a_transport_failure_not_an_empty_dataset(self):
        result = self._fetch_with_post_raising(httpx.TimeoutException("timed out"))
        assert result.posts == []
        assert result.error_code == "timeout"
        assert result.error_description is None
        assert result.request_error_count == 0

    def test_a_connection_error_is_reported_as_a_transport_failure(self):
        result = self._fetch_with_post_raising(httpx.ConnectError("connection refused"))
        assert result.posts == []
        assert result.error_code == "request_error"

    def test_an_http_error_is_reported_as_a_transport_failure(self):
        client = self._client()
        request = httpx.Request("POST", "https://api.apify.com/v2/acts/x/run-sync-get-dataset-items")

        async def _post_500(*args, **kwargs):
            return httpx.Response(500, text="internal error", request=request)

        client.client.post = _post_500
        result = asyncio.run(client.fetch_recent_posts("somehandle", results_limit=10))
        assert result.posts == []
        assert result.error_code == "http_error"

    def test_a_credit_exhausted_response_still_raises_and_is_not_reclassified(self):
        """402 must keep propagating as ApifyCreditExhaustedError -- §A only
        touches the three transport-exception branches, never the existing
        credit-exhaustion contract `InstagramPostsEnrichmentService` and
        others already depend on."""
        from app.api.apify_instagram_client import ApifyCreditExhaustedError

        client = self._client()
        request = httpx.Request("POST", "https://api.apify.com/v2/acts/x/run-sync-get-dataset-items")

        async def _post_402(*args, **kwargs):
            return httpx.Response(402, text="payment required", request=request)

        client.client.post = _post_402
        with pytest.raises(ApifyCreditExhaustedError):
            asyncio.run(client.fetch_recent_posts("somehandle", results_limit=10))

    def test_a_transport_failure_on_search_users_still_degrades_to_no_results(self):
        """search_users has no per-caller use for the failure TYPE (it never
        surfaced one before this plan either) -- only its degrade-to-empty
        behavior on a transport failure must survive §A's contract change
        from `_run_actor_sync` returning `None` to raising."""
        client = self._client()

        async def _raising_post(*args, **kwargs):
            raise httpx.TimeoutException("timed out")

        client.client.post = _raising_post
        results = asyncio.run(client.search_users("some query"))
        assert results == []
