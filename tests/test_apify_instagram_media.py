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
    return asyncio.run(client.fetch_recent_posts("somehandle", results_limit=results_limit))


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
