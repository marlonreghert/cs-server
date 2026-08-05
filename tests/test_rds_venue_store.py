"""Unit tests for the venue_source='google_only' refresh-selection exclusion
(plans/260804_add-venue-google-only.md) — the feature's cost guarantee.

Exercised via tests.rds_fake.InMemoryRdsVenueStore, the proven behaviour
contract for the SQLAlchemy RdsVenueStore (see tests/test_priority_bounded_refresh.py
for the base ordering contract, which this file does not re-test — only the
google_only exclusion on top of it).
"""
from app.models import Venue
from tests.rds_fake import InMemoryRdsVenueStore


def _venue(vid, priority=5, reviews=None, rating=None, venue_source="besttime"):
    return Venue(
        forecast=True,
        processed=True,
        venue_id=vid,
        venue_name=f"Venue {vid}",
        venue_address=f"addr {vid}",
        venue_lat=-8.05,
        venue_lng=-34.88,
        priority=priority,
        reviews=reviews,
        rating=rating,
        venue_source=venue_source,
    )


class TestActivePriorityExcludesGoogleOnly:
    def test_google_only_venue_excluded_even_at_top_priority(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("besttime_venue", priority=1, reviews=1))
        store.upsert_venue(
            _venue(
                "google_only_venue",
                priority=0,
                reviews=9999,
                venue_source="google_only",
            )
        )
        assert store.list_active_venue_ids_by_priority(10) == ["besttime_venue"]

    def test_google_only_alone_selects_nothing(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("only_google", venue_source="google_only"))
        assert store.list_active_venue_ids_by_priority(10) == []

    def test_besttime_venues_still_selected_ordering_unaffected(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("a", priority=0, reviews=10))
        store.upsert_venue(_venue("b", priority=1, reviews=10))
        store.upsert_venue(_venue("g", priority=0, reviews=9999, venue_source="google_only"))
        assert store.list_active_venue_ids_by_priority(10) == ["a", "b"]


class TestServablePriorityExcludesGoogleOnly:
    def test_google_only_venue_excluded_from_bounded_refresh(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("besttime_venue", priority=1, reviews=1))
        store.upsert_venue(
            _venue(
                "google_only_venue",
                priority=0,
                reviews=9999,
                venue_source="google_only",
            )
        )
        assert store.list_servable_venue_ids_by_priority(10) == ["besttime_venue"]

    def test_google_only_venue_still_appears_in_the_unbounded_serving_view(self):
        """The exclusion is refresh-selection-only: a google_only venue stays
        servable (reaches the app) — only bounded BestTime refresh skips it,
        because it has no BestTime id to query."""
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("g", priority=0, venue_source="google_only"))
        assert "g" in store.list_servable_venue_ids()
        assert store.list_servable_venue_ids_by_priority(10) == []

    def test_google_only_alone_selects_nothing(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("only_google", venue_source="google_only"))
        assert store.list_servable_venue_ids_by_priority(10) == []

    def test_deprecated_google_only_venue_stays_excluded(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("a", priority=0, reviews=1))
        store.upsert_venue(_venue("g", priority=0, reviews=999, venue_source="google_only"))
        store.soft_delete_venue("g", "test", "test")
        assert store.list_servable_venue_ids_by_priority(10) == ["a"]
