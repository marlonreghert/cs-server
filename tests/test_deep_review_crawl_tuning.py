"""Coverage for the 2026-08-14 crawl-tuning fixes.

All four came out of running the crawl for real:

1. The actor's sort key is `reviewsSort`, not `sort`. We had been sending a
   field the actor does not have, so ordering fell back to its default —
   which matters because `reviewsStartDate` only filters meaningfully under
   newest-first.
2. Rating-only reviews carry no evidence and must not be stored.
3. `truncated` judged on the post-dedup count reported False for a venue that
   returned the full requested 300 with an oldest review 5 days old inside a
   180-day window.
4. The per-venue cap default moved 300 -> 80 (breadth per dollar).
"""
from datetime import datetime, timedelta, timezone

from app.api.apify_gmaps_reviews_client import ApifyGMapsReviewsClient
from app.config import settings


# ── 1. the actor's real sort field ───────────────────────────────────────────

class _CapturingClient(ApifyGMapsReviewsClient):
    """Captures the run_input `_start_run` would have been given."""

    def __init__(self):
        self.captured = None

    async def _start_run(self, run_input, endpoint_label):
        self.captured = run_input
        return None  # abort before any polling; we only care about the input


async def _capture(**kwargs):
    c = _CapturingClient()
    await c.fetch_reviews(["ChIJtest"], kwargs.pop("max_reviews", 80), **kwargs)
    return c.captured


async def test_sort_is_sent_under_the_actors_real_field_name():
    run_input = await _capture()
    assert run_input["reviewsSort"] == "newest"


async def test_the_nonexistent_sort_field_is_not_sent():
    """`sort` is not in the actor's input schema; sending it did nothing."""
    run_input = await _capture()
    assert "sort" not in run_input


async def test_every_field_sent_exists_in_the_actors_input_schema():
    """Guard against inventing another field the actor silently ignores.

    The schema was read from the actor's latest build via the Apify API on
    2026-08-14.
    """
    ACTOR_INPUT_FIELDS = {
        "startUrls", "placeIds", "maxReviews", "reviewsSort",
        "reviewsStartDate", "reviewsFilterString", "language",
        "reviewsOrigin", "personalData",
    }
    run_input = await _capture(since="2026-02-15T00:00:00Z")
    assert set(run_input) <= ACTOR_INPUT_FIELDS, set(run_input) - ACTOR_INPUT_FIELDS


# ── 4. the lowered default ───────────────────────────────────────────────────

def test_per_venue_cap_defaults_to_eighty():
    """Pinned because it is a spend decision, not a taste one: at 300 the same
    budget reaches roughly a quarter as many venues."""
    assert settings.reviews_deep_max_per_venue == 80


def test_projection_slice_stays_below_the_per_venue_cap():
    """Redis carries a slice, RDS the whole corpus. If the cap ever drops
    below the slice the distinction silently disappears."""
    assert settings.reviews_deep_projection_max <= settings.reviews_deep_max_per_venue
