"""The 180-day window must be pushed to the actor, not applied after paying.

Written after the 2026-08-14 production trial: a first crawl sent no
`reviewsStartDate`, so the actor scraped to `maxReviews` (300/venue) and billed
all 600, of which only 67 were inside the window and kept. The window filter
worked — it just ran after the money was spent.

Written AGAIN after a second, more serious incident on the SAME day: pushing
a `since` down at all is not enough if its FORMAT is invalid. `_fetch_since`
was returning `datetime.isoformat()`, e.g. `2026-02-15T10:39:00+00:00` — a
`+00:00` offset the actor's own input validation REJECTS with an HTTP 400.
A 150-venue crawl built on that cursor 400'd on every single call, billed $0,
and (via a separate bug now also fixed) reported `outcome: "ok"`.

These tests pin `_fetch_since`, the cursor the client turns into
`reviewsStartDate` — including, now, that its OUTPUT actually validates
against the actor's real regex, not just that some string comes back.
"""
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import settings
from app.services.deep_review_crawl_service import DeepReviewCrawlService

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

# The actor's own `reviewsStartDate` validation pattern, read verbatim off a
# live 400 response body. Compiled here, independently of the production
# code, so this test actually catches a format regression rather than just
# re-asserting whatever the implementation currently does.
ACTOR_REVIEWS_START_DATE_PATTERN = re.compile(
    r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
    r"(T[0-2]\d:[0-5]\d(:[0-5]\d)?(\.\d+)?Z?)?$"
    r"|^(\d+)\s*(minute|hour|day|week|month|year)s?$"
)


def _service():
    svc = DeepReviewCrawlService.__new__(DeepReviewCrawlService)
    svc._now = lambda: NOW
    return svc


def _window_start():
    return NOW - timedelta(days=settings.reviews_deep_window_days)


def _window_start_actor_format():
    return _window_start().strftime("%Y-%m-%dT%H:%M:%SZ")


def test_first_crawl_pushes_the_window_edge_down_to_the_actor():
    """The regression: a first crawl must NOT ask for an unbounded history."""
    assert _service()._fetch_since(None) == _window_start_actor_format()


def test_first_crawl_never_returns_none():
    """None meant 'no reviewsStartDate', which is what caused the overspend."""
    assert _service()._fetch_since(None) is not None


def test_recrawl_uses_the_newest_stored_review_when_it_is_inside_the_window():
    newest = (NOW - timedelta(days=3)).isoformat()
    existing = SimpleNamespace(newest_publish_time=newest)
    expected = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _service()._fetch_since(existing) == expected


def test_recrawl_falls_back_to_the_window_edge_when_the_record_is_stale():
    """A record whose newest review predates the window must not re-open the
    history: the window edge is the later boundary and wins."""
    stale = (NOW - timedelta(days=400)).isoformat()
    existing = SimpleNamespace(newest_publish_time=stale)
    assert _service()._fetch_since(existing) == _window_start_actor_format()


def test_record_without_a_newest_timestamp_uses_the_window_edge():
    existing = SimpleNamespace(newest_publish_time=None)
    assert _service()._fetch_since(existing) == _window_start_actor_format()


def test_record_with_an_unparseable_newest_timestamp_falls_back_to_the_window_edge():
    """A stored value that cannot even be parsed must fall back to the window
    edge rather than forward something unvalidated to the actor."""
    existing = SimpleNamespace(newest_publish_time="not-a-real-timestamp")
    assert _service()._fetch_since(existing) == _window_start_actor_format()


# ── the test that would have caught the outage ──────────────────────────────
class TestSinceMatchesTheActorsRealValidationRegex:
    """`_fetch_since`'s output is asserted against the ACTUAL pattern the live
    Apify actor enforces on `reviewsStartDate`, compiled independently above.
    A `+00:00` offset or stray microseconds fail this exact assertion — which
    is precisely what a real 150-venue crawl got wrong."""

    def test_first_crawl_case(self):
        result = _service()._fetch_since(None)
        assert ACTOR_REVIEWS_START_DATE_PATTERN.match(result), result

    def test_in_window_stored_cursor_case(self):
        newest = (NOW - timedelta(days=3)).isoformat()
        existing = SimpleNamespace(newest_publish_time=newest)
        result = _service()._fetch_since(existing)
        assert ACTOR_REVIEWS_START_DATE_PATTERN.match(result), result

    def test_stored_timestamp_with_microseconds_and_utc_offset_case(self):
        """The exact shape `newest_publish_time` comes back as from a real
        Apify run: microseconds AND a `+00:00` offset, both individually
        rejected by the actor's validation."""
        existing = SimpleNamespace(newest_publish_time="2026-08-11T12:32:52.816000+00:00")
        result = _service()._fetch_since(existing)
        assert ACTOR_REVIEWS_START_DATE_PATTERN.match(result), result
