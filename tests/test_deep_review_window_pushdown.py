"""The 180-day window must be pushed to the actor, not applied after paying.

Written after the 2026-08-14 production trial: a first crawl sent no
`reviewsStartDate`, so the actor scraped to `maxReviews` (300/venue) and billed
all 600, of which only 67 were inside the window and kept. The window filter
worked — it just ran after the money was spent.

These tests pin `_fetch_since`, the cursor the client turns into
`reviewsStartDate`.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import settings
from app.services.deep_review_crawl_service import DeepReviewCrawlService

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def _service():
    svc = DeepReviewCrawlService.__new__(DeepReviewCrawlService)
    svc._now = lambda: NOW
    return svc


def _window_start_iso():
    return (NOW - timedelta(days=settings.reviews_deep_window_days)).isoformat()


def test_first_crawl_pushes_the_window_edge_down_to_the_actor():
    """The regression: a first crawl must NOT ask for an unbounded history."""
    assert _service()._fetch_since(None) == _window_start_iso()


def test_first_crawl_never_returns_none():
    """None meant 'no reviewsStartDate', which is what caused the overspend."""
    assert _service()._fetch_since(None) is not None


def test_recrawl_uses_the_newest_stored_review_when_it_is_inside_the_window():
    newest = (NOW - timedelta(days=3)).isoformat()
    existing = SimpleNamespace(newest_publish_time=newest)
    assert _service()._fetch_since(existing) == newest


def test_recrawl_falls_back_to_the_window_edge_when_the_record_is_stale():
    """A record whose newest review predates the window must not re-open the
    history: the window edge is the later boundary and wins."""
    stale = (NOW - timedelta(days=400)).isoformat()
    existing = SimpleNamespace(newest_publish_time=stale)
    assert _service()._fetch_since(existing) == _window_start_iso()


def test_record_without_a_newest_timestamp_uses_the_window_edge():
    existing = SimpleNamespace(newest_publish_time=None)
    assert _service()._fetch_since(existing) == _window_start_iso()
