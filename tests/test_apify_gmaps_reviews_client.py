"""Unit tests for ApifyGMapsReviewsClient and its `parse_review`/
`parse_publish_time` helpers. Mirrors tests/test_apify_poll_timeout.py's
transport-fake pattern for the sibling compass/google-maps-extractor client.
See plans/260813_deep-review-corpus.md.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import app.api.apify_gmaps_reviews_client as mod
from app.api.apify_gmaps_reviews_client import (
    ApifyGMapsReviewsClient,
    parse_publish_time,
    parse_review,
)
from app.api.apify_gmaps_extractor_client import ApifyPollTimeoutError, POLL_BUDGET_EXHAUSTED
from app.api.apify_instagram_client import ApifyCreditExhaustedError

INTERVAL = 0.001
BASE = 4
ENDPOINT = "gmaps_reviews"


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 402:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _Http:
    def __init__(self, statuses, items=None, credit_at=None, start_input_sink=None):
        self.statuses = list(statuses)
        self.items = items if items is not None else []
        self.credit_at = credit_at
        self.starts = 0
        self.polls = 0
        self._start_input_sink = start_input_sink

    async def post(self, url, params=None, json=None, **kw):
        self.starts += 1
        if self._start_input_sink is not None:
            self._start_input_sink.append(json)
        return _Resp({"data": {"id": "run_x", "defaultDatasetId": "ds_x"}})

    async def get(self, url, params=None, **kw):
        if "/actor-runs/" in url:
            self.polls += 1
            if self.credit_at is not None and self.polls >= self.credit_at:
                return _Resp({"error": "credits"}, status_code=402)
            idx = min(self.polls - 1, len(self.statuses) - 1)
            return _Resp({"data": {"status": self.statuses[idx]}})
        return _Resp(self.items)


def _client(http):
    client = ApifyGMapsReviewsClient(api_token="t")
    client.client = http  # type: ignore[assignment]
    return client


def _run(coro):
    return asyncio.run(coro)


# ── parse_publish_time ───────────────────────────────────────────────────────
class TestParsePublishTime:
    def test_parses_iso_with_z_suffix(self):
        dt = parse_publish_time("2026-01-01T00:00:00.000Z")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 1 and dt.day == 1

    def test_returns_none_for_garbage(self):
        assert parse_publish_time("not a date") is None

    def test_returns_none_for_none_or_non_string(self):
        assert parse_publish_time(None) is None
        assert parse_publish_time(12345) is None

    def test_naive_datetime_is_treated_as_utc(self):
        dt = parse_publish_time("2026-01-01T00:00:00")
        assert dt.tzinfo is not None


# ── parse_review: malformed items are skipped, never raise ─────────────────
class TestParseReview:
    def test_parses_a_well_formed_item(self):
        review = parse_review({
            "reviewId": "r1", "name": "Ana", "text": "Great place",
            "publishedAtDate": "2026-01-01T00:00:00Z", "stars": 5,
            "reviewerLanguage": "pt",
        })
        assert review is not None
        assert review.author_name == "Ana"
        assert review.rating == 5
        assert review.review_id == "r1"
        assert review.source == "apify_gmaps"
        assert review.language == "pt"

    @pytest.mark.parametrize("item", [
        {},
        {"name": "Ana", "text": "no date"},
        {"text": "no author", "publishedAtDate": "2026-01-01T00:00:00Z"},
        {"name": "Ana", "publishedAtDate": "2026-01-01T00:00:00Z"},  # no text
        {"name": "Ana", "text": "bad date", "publishedAtDate": "not-a-date"},
        "not-a-dict",
        None,
    ])
    def test_malformed_items_are_skipped(self, item):
        assert parse_review(item) is None

    def test_missing_or_non_numeric_rating_defaults_to_zero(self):
        review = parse_review({
            "name": "Ana", "text": "t", "publishedAtDate": "2026-01-01T00:00:00Z",
            "stars": "not-a-number",
        })
        assert review is not None
        assert review.rating == 0

    def test_falls_back_to_alternate_field_names(self):
        review = parse_review({
            "authorName": "Bea", "text": "t", "publishAt": "2026-01-01T00:00:00Z",
            "rating": 4,
        })
        assert review is not None
        assert review.author_name == "Bea"
        assert review.rating == 4


# ── run input construction ───────────────────────────────────────────────────
class TestFetchReviewsInputConstruction:
    def test_builds_expected_run_input_without_since(self):
        sink = []
        http = _Http(["SUCCEEDED"], items=[], start_input_sink=sink)
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            _run(client.fetch_reviews(["place_1"], 300, language="pt-BR"))
        assert sink[0] == {
            "placeIds": ["place_1"], "maxReviews": 300, "sort": "newest", "language": "pt-BR",
        }

    def test_includes_reviews_start_date_when_since_given(self):
        sink = []
        http = _Http(["SUCCEEDED"], items=[], start_input_sink=sink)
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            _run(client.fetch_reviews(["place_1"], 50, since="2026-01-01T00:00:00+00:00"))
        assert sink[0]["reviewsStartDate"] == "2026-01-01T00:00:00+00:00"


# ── terminal status handling ─────────────────────────────────────────────────
class TestFetchReviewsTerminalStatus:
    def test_succeeded_returns_dataset_items(self):
        items = [{"reviewId": "r1", "name": "A", "text": "t", "publishedAtDate": "2026-01-01T00:00:00Z"}]
        http = _Http(["SUCCEEDED"], items=items)
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            result = _run(client.fetch_reviews(["place_1"], 10))
        assert result == items

    def test_failed_status_returns_none(self):
        http = _Http(["FAILED"])
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            result = _run(client.fetch_reviews(["place_1"], 10))
        assert result is None

    def test_poll_budget_exhaustion_raises_apify_poll_timeout_error(self):
        """The RUNNING/READY case: the actor is still alive, but we stopped
        waiting — distinct from the actor's own TIMED-OUT terminal status."""
        http = _Http(["RUNNING"] * (BASE + 2))
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            with pytest.raises(ApifyPollTimeoutError) as exc_info:
                _run(client.fetch_reviews(["place_1"], 10))
        assert exc_info.value.last_status == "RUNNING"

    def test_credit_exhausted_on_start_propagates(self):
        class _CreditHttp(_Http):
            async def post(self, url, params=None, json=None, **kw):
                return _Resp({"error": "credits"}, status_code=402)

        client = _client(_CreditHttp(["SUCCEEDED"]))
        with pytest.raises(ApifyCreditExhaustedError):
            _run(client.fetch_reviews(["place_1"], 10))

    def test_credit_exhausted_mid_poll_propagates(self):
        http = _Http(["RUNNING", "RUNNING", "SUCCEEDED"], credit_at=2)
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            with pytest.raises(ApifyCreditExhaustedError):
                _run(client.fetch_reviews(["place_1"], 10))
