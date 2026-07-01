"""Tests for the Venue rating/reviews/price_level backfill path.

Context: the BestTime venue_filter discovery path populates Venue.rating /
Venue.reviews / Venue.price_level at ingestion time. The inventory-sync
path (added in #18) does not. Without this backfill, ~720 inventory-synced
venues (Praça Laura Nigro, Jockey Club, Barchef, …) stay null forever
even though Google has the data — and the mobile card has no stars.

The fix:
1. Ask Google for rating + userRatingCount + priceLevel (added to
   VIBE_FIELDS_MASK).
2. After enrichment, write those values back onto the Venue model and
   upsert (this file's subject).
"""
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.google_places_client import VIBE_FIELDS_MASK
from app.models.vibe_attributes import GooglePlacesDetailsResponse, VibeAttributes
from app.models import Venue
from app.services.google_places_enrichment_service import (
    GooglePlacesEnrichmentService,
    _price_level_to_int,
)


class _FakeGoogleClient:
    def __init__(self, details: GooglePlacesDetailsResponse):
        self.details = details
        self.get_place_details = AsyncMock(return_value=details)

    def details_to_vibe_attributes(self, venue_id, details):
        return VibeAttributes(
            venue_id=venue_id,
            google_place_id=details.place_id,
            google_primary_type=details.primary_type,
        )


def make_venue(**overrides):
    """Inventory-sync-shaped venue: id/name/coords only, review signal null."""
    base = dict(
        venue_id="ven_lauranigro",
        venue_name="Praça Laura Nigro",
        venue_address="R. de São Bento, S/N - Carmo Olinda - PE",
        venue_lat=-8.0163046,
        venue_lng=-34.852692,
        rating=None,
        reviews=None,
        price_level=None,
    )
    base.update(overrides)
    return Venue(**base)


class TestFieldMaskIncludesReviewSignal:
    """Sanity guard: if someone trims the field mask in the future, the API
    silently stops returning these and the backfill goes back to no-op."""

    def test_field_mask_requests_rating(self):
        assert "rating" in VIBE_FIELDS_MASK.split(",")

    def test_field_mask_requests_user_rating_count(self):
        assert "userRatingCount" in VIBE_FIELDS_MASK.split(",")

    def test_field_mask_requests_price_level(self):
        assert "priceLevel" in VIBE_FIELDS_MASK.split(",")


class TestPriceLevelMapping:
    @pytest.mark.parametrize(
        "google_enum,expected_int",
        [
            ("PRICE_LEVEL_INEXPENSIVE", 1),
            ("PRICE_LEVEL_MODERATE", 2),
            ("PRICE_LEVEL_EXPENSIVE", 3),
            ("PRICE_LEVEL_VERY_EXPENSIVE", 4),
        ],
    )
    def test_known_enums_map_to_1_4_scale(self, google_enum, expected_int):
        assert _price_level_to_int(google_enum) == expected_int

    @pytest.mark.parametrize(
        "edge_case", [None, "", "PRICE_LEVEL_FREE", "PRICE_LEVEL_UNSPECIFIED", "garbage"]
    )
    def test_unmapped_or_missing_returns_none(self, edge_case):
        # PRICE_LEVEL_FREE / _UNSPECIFIED don't fit the 1-4 scale the
        # mobile PriceIndicator expects, and lying with a 1 would be worse
        # than leaving the field null.
        assert _price_level_to_int(edge_case) is None


@pytest.mark.asyncio
class TestBackfillEndToEnd:
    async def _service_with_venue(self, venue, details):
        """Wire a service with a minimum-viable DAO mock + happy-path
        details. Returns (service, dao) so tests can assert call args."""
        dao = Mock()
        dao.get_vibe_attributes.return_value = None
        dao.get_venue.return_value = venue
        dao.set_google_business_status.return_value = True
        service = GooglePlacesEnrichmentService(_FakeGoogleClient(details), dao)
        return service, dao

    async def test_writes_rating_reviews_price_level_to_venue(self):
        """The Laura Nigro scenario: inventory-synced venue, Google has
        4.5★ / 586 / Moderate → after enrichment, Venue has them all."""
        venue = make_venue()
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake",
            business_status="OPERATIONAL",
            rating=4.5,
            user_rating_count=586,
            price_level="PRICE_LEVEL_MODERATE",
        )
        service, dao = await self._service_with_venue(venue, details)

        await service.enrich_venue("ven_lauranigro", "ChIJfake", force_refresh=True)

        dao.upsert_venue.assert_called_once()
        upserted = dao.upsert_venue.call_args[0][0]
        assert upserted.rating == 4.5
        assert upserted.reviews == 586
        assert upserted.price_level == 2

    async def test_no_upsert_when_google_returns_no_review_signal(self):
        """If Google has nothing to backfill (rating/count/price all None),
        don't waste an upsert rewriting the venue with identical data."""
        venue = make_venue()
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake",
            business_status="OPERATIONAL",
            rating=None,
            user_rating_count=None,
            price_level=None,
        )
        service, dao = await self._service_with_venue(venue, details)

        await service.enrich_venue("ven_lauranigro", "ChIJfake", force_refresh=True)

        dao.upsert_venue.assert_not_called()

    async def test_preserves_existing_venue_values_when_google_returns_none(self):
        """A venue that already has rating from BestTime must NOT have it
        wiped if Google happens to return null for that field this run."""
        venue = make_venue(rating=4.7, reviews=2327, price_level=3)
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake",
            business_status="OPERATIONAL",
            rating=None,
            user_rating_count=None,
            price_level=None,
        )
        service, dao = await self._service_with_venue(venue, details)

        await service.enrich_venue("ven_x", "ChIJfake", force_refresh=True)

        dao.upsert_venue.assert_not_called()

    async def test_partial_backfill_only_writes_changed_fields(self):
        """Google returns rating + count but no priceLevel — Venue should
        get rating + reviews updated, price_level untouched."""
        venue = make_venue(price_level=2)  # existing
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake",
            business_status="OPERATIONAL",
            rating=4.2,
            user_rating_count=100,
            price_level=None,
        )
        service, dao = await self._service_with_venue(venue, details)

        await service.enrich_venue("ven_y", "ChIJfake", force_refresh=True)

        dao.upsert_venue.assert_called_once()
        upserted = dao.upsert_venue.call_args[0][0]
        assert upserted.rating == 4.2
        assert upserted.reviews == 100
        assert upserted.price_level == 2  # unchanged

    async def test_skips_upsert_when_values_match_existing(self):
        """Google returns the same numbers already on the Venue — no-op. The
        derived tier now also persists its source + raw enum, so a true no-op
        requires those to already match too."""
        venue = make_venue(
            rating=4.5,
            reviews=586,
            price_level=2,
            price_level_source="google_enum",
            google_price_level="PRICE_LEVEL_MODERATE",
        )
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake",
            business_status="OPERATIONAL",
            rating=4.5,
            user_rating_count=586,
            price_level="PRICE_LEVEL_MODERATE",
        )
        service, dao = await self._service_with_venue(venue, details)

        await service.enrich_venue("ven_z", "ChIJfake", force_refresh=True)

        dao.upsert_venue.assert_not_called()

    async def test_venue_not_in_redis_logs_warning_and_skips(self):
        """The venue lookup races with delete/soft-delete — if it's gone
        by the time we try to backfill, swallow it. Don't crash and don't
        upsert a None."""
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake",
            business_status="OPERATIONAL",
            rating=4.5,
            user_rating_count=586,
            price_level="PRICE_LEVEL_MODERATE",
        )
        dao = Mock()
        dao.get_vibe_attributes.return_value = None
        dao.get_venue.return_value = None  # venue vanished
        service = GooglePlacesEnrichmentService(_FakeGoogleClient(details), dao)

        # Must not raise.
        await service.enrich_venue("ven_gone", "ChIJfake", force_refresh=True)

        dao.upsert_venue.assert_not_called()


@pytest.mark.asyncio
class TestGoogleOnlyPrice:
    """The `google_only_price` flag (backfill path): suppress the stored BestTime
    fallback so a venue with no Google price ends NULL, not a BestTime tier."""

    async def _service(self, venue, details):
        dao = Mock()
        dao.get_vibe_attributes.return_value = None
        dao.get_venue.return_value = venue
        dao.set_google_business_status.return_value = True
        return GooglePlacesEnrichmentService(_FakeGoogleClient(details), dao), dao

    async def test_default_uses_besttime_fallback(self):
        # A pending venue with a stored BestTime tier and no Google price keeps the
        # BestTime tier when google_only_price is False (unchanged cron/add behavior).
        venue = make_venue(besttime_price_level=3, price_level=None)
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake", business_status="OPERATIONAL",
            rating=4.1, user_rating_count=9, price_level=None, price_range=None,
        )
        service, dao = await self._service(venue, details)
        await service.enrich_venue("v", "ChIJfake", force_refresh=True, google_only_price=False)
        upserted = dao.upsert_venue.call_args[0][0]
        assert upserted.price_level == 3
        assert upserted.price_level_source == "besttime"

    async def test_google_only_suppresses_besttime_fallback(self):
        venue = make_venue(besttime_price_level=3, price_level=None)
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake", business_status="OPERATIONAL",
            rating=4.1, user_rating_count=9, price_level=None, price_range=None,
        )
        service, dao = await self._service(venue, details)
        await service.enrich_venue("v", "ChIJfake", force_refresh=True, google_only_price=True)
        upserted = dao.upsert_venue.call_args[0][0]
        assert upserted.price_level is None
        assert upserted.price_level_source is None

    async def test_google_only_still_takes_google_enum(self):
        # Google-only doesn't mean "no price" — a Google enum still wins.
        venue = make_venue(besttime_price_level=1, price_level=None)
        details = GooglePlacesDetailsResponse(
            place_id="ChIJfake", business_status="OPERATIONAL",
            rating=4.1, user_rating_count=9, price_level="PRICE_LEVEL_EXPENSIVE",
        )
        service, dao = await self._service(venue, details)
        await service.enrich_venue("v", "ChIJfake", force_refresh=True, google_only_price=True)
        upserted = dao.upsert_venue.call_args[0][0]
        assert upserted.price_level == 3
        assert upserted.price_level_source == "google_enum"


@pytest.mark.asyncio
class TestEnrichPendingVenues:
    """Backfill selection + no-match marker idempotency over a real fake DAO."""

    def _service(self, details, place_id="ChIJbf"):
        import fakeredis
        from app.dao.venue_repository import VenueRepository
        from app.db.geo_redis_client import GeoRedisClient
        from tests.rds_fake import InMemoryRdsVenueStore

        repo = VenueRepository(
            GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)),
            rds_store=InMemoryRdsVenueStore(),
        )
        client = _FakeGoogleClient(details)
        client.search_place_id = AsyncMock(return_value=place_id)
        return GooglePlacesEnrichmentService(client, repo), repo, client

    def _seed(self, repo, vid, *, enriched=False):
        repo.upsert_venue(Venue(
            venue_id=vid, venue_name=f"Bar {vid}", venue_address="a",
            venue_lat=-8.05, venue_lng=-34.88, venue_type="BAR",
        ))
        if enriched:
            repo.set_vibe_attributes(VibeAttributes(
                venue_id=vid, google_place_id=f"places/{vid}", google_primary_type="bar",
            ))

    async def test_enriches_only_pending_and_skips_enriched(self):
        details = GooglePlacesDetailsResponse(
            place_id="ChIJbf", business_status="OPERATIONAL", primary_type="bar",
            rating=4.4, user_rating_count=50, price_level="PRICE_LEVEL_MODERATE",
        )
        service, repo, client = self._service(details)
        self._seed(repo, "enriched_one", enriched=True)
        self._seed(repo, "pending_one")

        summary = await service.enrich_pending_venues()

        assert summary["enriched"] == 1
        assert summary["skipped_cached"] == 1
        # Enriched venue's place_id untouched; pending venue now has a real type.
        assert repo.get_vibe_attributes("enriched_one").google_place_id == "places/enriched_one"
        assert repo.get_vibe_attributes("pending_one").google_primary_type == "bar"

    async def test_no_match_marks_and_second_run_skips(self):
        details = GooglePlacesDetailsResponse(
            place_id="", business_status="OPERATIONAL", primary_type=None,
        )
        service, repo, client = self._service(details)
        client.search_place_id = AsyncMock(return_value=None)  # no Google match
        self._seed(repo, "nomatch_one")

        first = await service.enrich_pending_venues()
        assert first["no_google_match"] == 1
        # Empty marker written (google_place_id="").
        marker = repo.get_vibe_attributes("nomatch_one")
        assert marker is not None and not marker.google_primary_type

        # Second run: the marker makes it skipped_cached; Google not called again.
        client.search_place_id.reset_mock()
        second = await service.enrich_pending_venues()
        assert second["skipped_cached"] == 1
        assert second["no_google_match"] == 0
        client.search_place_id.assert_not_awaited()

    async def test_backfill_makes_no_besttime_call(self):
        # The fake Google client + repo never touch BestTime; assert the summary
        # is coherent and nothing raises (BestTime-free by construction).
        details = GooglePlacesDetailsResponse(
            place_id="ChIJbf", business_status="OPERATIONAL", primary_type="bar",
            rating=4.0, user_rating_count=5, price_level="PRICE_LEVEL_INEXPENSIVE",
        )
        service, repo, client = self._service(details)
        self._seed(repo, "p1")
        summary = await service.enrich_pending_venues()
        assert summary["enriched"] == 1 and summary["error"] == 0
