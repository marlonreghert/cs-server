"""Unit tests for the per-venue type override:
- the enrichment guard (_apply_primary_type) preserving a locked type,
- the REPRESENTATIVE_GOOGLE_TYPE reverse map,
- primary_type_locked round-tripping through the RDS-backed repository.
"""
import fakeredis

from app.db.geo_redis_client import GeoRedisClient
from app.dao.venue_repository import VenueRepository
from app.models.vibe_attributes import VibeAttributes
from app.models.venue_category import (
    CATEGORIES,
    REPRESENTATIVE_GOOGLE_TYPE,
    _GOOGLE_TO_CATEGORY,
    representative_google_type,
    resolve_category,
)
from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService
from app.services.venue_eligibility import DEFAULT_BLOCKED_GOOGLE_TYPES
from tests.rds_fake import InMemoryRdsVenueStore


class _FakeDao:
    """Minimal venue_dao exposing get_vibe_attributes for the enrichment guard."""

    def __init__(self, existing=None):
        self._existing = existing

    def get_vibe_attributes(self, venue_id):
        return self._existing


def _svc(existing=None):
    return GooglePlacesEnrichmentService(
        google_places_client=None, venue_dao=_FakeDao(existing)
    )


class TestEnrichmentGuard:
    def test_locked_type_is_preserved_over_google(self):
        existing = VibeAttributes(
            venue_id="v1", google_primary_type="night_club", primary_type_locked=True
        )
        fresh = VibeAttributes(venue_id="v1")
        _svc(existing)._apply_primary_type("v1", fresh, "art_museum")
        assert fresh.google_primary_type == "night_club"
        assert fresh.primary_type_locked is True

    def test_unlocked_type_takes_google_value(self):
        existing = VibeAttributes(
            venue_id="v1", google_primary_type="night_club", primary_type_locked=False
        )
        fresh = VibeAttributes(venue_id="v1")
        _svc(existing)._apply_primary_type("v1", fresh, "art_museum")
        assert fresh.google_primary_type == "art_museum"
        assert fresh.primary_type_locked is False

    def test_no_existing_row_takes_google_value(self):
        fresh = VibeAttributes(venue_id="v1")
        _svc(None)._apply_primary_type("v1", fresh, "bar")
        assert fresh.google_primary_type == "bar"


class TestRepresentativeGoogleType:
    def test_every_non_other_category_has_a_valid_representative(self):
        for category in CATEGORIES:
            if category == "OTHER":
                assert representative_google_type(category) is None
                continue
            rep = representative_google_type(category)
            assert rep is not None, f"{category} has no representative google type"
            # The representative must itself resolve back to the same category,
            # be a real key in the forward map, and not be blocked by eligibility.
            assert _GOOGLE_TO_CATEGORY.get(rep) == category, (
                f"{rep} does not map back to {category}"
            )
            assert resolve_category(google_type=rep) == category
            assert rep not in DEFAULT_BLOCKED_GOOGLE_TYPES, (
                f"representative {rep} for {category} is eligibility-blocked"
            )

    def test_unknown_category_returns_none(self):
        assert representative_google_type("NOPE") is None
        assert representative_google_type("") is None
        assert representative_google_type(None) is None

    def test_map_has_no_other(self):
        assert "OTHER" not in REPRESENTATIVE_GOOGLE_TYPE


class TestPrimaryTypeLockedRoundTrip:
    def test_lock_round_trips_through_repository(self):
        repo = VenueRepository(
            GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)),
            rds_store=InMemoryRdsVenueStore(),
        )
        repo.set_vibe_attributes(
            VibeAttributes(
                venue_id="v1", google_primary_type="night_club", primary_type_locked=True
            )
        )
        got = repo.get_vibe_attributes("v1")
        assert got is not None
        assert got.google_primary_type == "night_club"
        assert got.primary_type_locked is True

    def test_default_is_unlocked(self):
        repo = VenueRepository(
            GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)),
            rds_store=InMemoryRdsVenueStore(),
        )
        repo.set_vibe_attributes(VibeAttributes(venue_id="v2", google_primary_type="bar"))
        got = repo.get_vibe_attributes("v2")
        assert got.primary_type_locked is False
