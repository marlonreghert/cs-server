"""Unit tests for ApifyGMapsReviewsClient and its `parse_review`/
`parse_publish_time` helpers. Mirrors tests/test_apify_poll_timeout.py's
transport-fake pattern for the sibling compass/google-maps-extractor client.
See plans/260813_deep-review-corpus.md.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from datetime import datetime, timedelta, timezone

import app.api.apify_gmaps_reviews_client as mod
from app.api.apify_gmaps_reviews_client import (
    ApifyGMapsReviewsClient,
    format_publish_time_for_actor,
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


# ── format_publish_time_for_actor: the exact normalisation the outage needed ─
class TestFormatPublishTimeForActor:
    def test_utc_offset_is_rendered_as_a_bare_z_suffix(self):
        """`datetime.isoformat()` emits `+00:00`, which the actor's own input
        validation REJECTS. This is the root cause of a real outage: every
        one of a 150-venue crawl's `reviewsStartDate` values 400'd."""
        dt = datetime(2026, 2, 15, 10, 39, 0, tzinfo=timezone.utc)
        assert dt.isoformat() == "2026-02-15T10:39:00+00:00"  # what NOT to send
        assert format_publish_time_for_actor(dt) == "2026-02-15T10:39:00Z"

    def test_microseconds_are_stripped(self):
        """`newest_publish_time` comes back from a real Apify run looking
        like `2026-08-11T12:32:52.816000+00:00` — equally invalid, and for
        a second reason (microseconds) on top of the offset."""
        dt = datetime(2026, 8, 11, 12, 32, 52, 816000, tzinfo=timezone.utc)
        assert format_publish_time_for_actor(dt) == "2026-08-11T12:32:52Z"

    def test_a_non_utc_offset_is_converted_to_utc_first(self):
        dt = datetime(2026, 2, 15, 7, 39, 0, tzinfo=timezone(timedelta(hours=-3)))
        assert format_publish_time_for_actor(dt) == "2026-02-15T10:39:00Z"

    def test_a_naive_datetime_is_treated_as_already_utc(self):
        dt = datetime(2026, 2, 15, 10, 39, 0)
        assert format_publish_time_for_actor(dt) == "2026-02-15T10:39:00Z"


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


# ── None (failure) vs [] (genuine empty result) — the exact seam a real
# outage exploited: DeepReviewCrawlService cannot tell "no new reviews" apart
# from "the call failed" unless this distinction actually holds ────────────
class TestFailureVsGenuineEmptyResult:
    def test_succeeded_with_a_genuinely_empty_dataset_returns_an_empty_list(self):
        """A run that actually SUCCEEDED and found nothing must return `[]`,
        not `None` — collapsing this into a failure would be just as wrong
        as the reverse."""
        http = _Http(["SUCCEEDED"], items=[])
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            result = _run(client.fetch_reviews(["place_1"], 10))
        assert result == []
        assert result is not None

    def test_a_dataset_fetch_failure_after_a_succeeded_run_returns_none_not_empty_list(self):
        """The actor run itself SUCCEEDED, but fetching its dataset failed
        (e.g. a transient 5xx). The old code (`return items or []`) folded
        this straight into an empty success and even labeled the call
        `status="success"` in metrics — exactly the shape that let a real
        outage report `outcome: "ok"` while nothing was actually retrieved."""
        class _BadDatasetHttp(_Http):
            async def get(self, url, params=None, **kw):
                if "/actor-runs/" in url:
                    return await super().get(url, params=params, **kw)
                return _Resp({"error": "gone"}, status_code=500)

        http = _BadDatasetHttp(["SUCCEEDED"])
        client = _client(http)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            result = _run(client.fetch_reviews(["place_1"], 10))
        assert result is None

    def test_a_non_402_http_error_starting_the_run_returns_none_not_empty_list(self):
        """The exact defect this whole fix exists for: every one of a real
        150-venue crawl's actor calls 400'd on an invalid `reviewsStartDate`
        at run-creation time. That must surface as None, never `[]`."""
        class _BadStartHttp(_Http):
            async def post(self, url, params=None, json=None, **kw):
                self.starts += 1
                return _Resp({"error": "Invalid input"}, status_code=400)

        client = _client(_BadStartHttp(["SUCCEEDED"]))
        result = _run(client.fetch_reviews(["place_1"], 10))
        assert result is None
