"""Tests for venue type filtering and dual categorization."""
import pytest
from app.services.venues_refresher_service import VENUE_TYPES, DEFAULT_BLOCKED_VENUE_TYPES


class TestVenueTypes:
    """Verify VENUE_TYPES includes the right types for broad fetching."""

    def test_includes_nightlife_types(self):
        for t in ["BAR", "BREWERY", "CLUBS", "CONCERT_HALL", "EVENT_VENUE", "WINERY", "CASINO"]:
            assert t in VENUE_TYPES, f"{t} should be in VENUE_TYPES"

    def test_includes_other_for_broad_coverage(self):
        assert "OTHER" in VENUE_TYPES, "OTHER captures ~60% of BestTime venues"

    def test_includes_beer_and_bistro(self):
        assert "BEER" in VENUE_TYPES
        assert "BISTRO" in VENUE_TYPES

    def test_includes_food_and_drink(self):
        assert "FOOD_AND_DRINK" in VENUE_TYPES

    def test_excludes_restaurant_and_cafe(self):
        """RESTAURANT and CAFE pull in too much junk — they should be blocked, not fetched."""
        assert "RESTAURANT" not in VENUE_TYPES
        assert "CAFE" not in VENUE_TYPES


class TestBlockedVenueTypes:
    """Verify DEFAULT_BLOCKED_VENUE_TYPES blocks junk types."""

    def test_does_not_block_parks(self):
        """PARK/CITY_PARK were unblocked so praças/urban parks resolve to the
        PARK category and serve (park-category-eligibility feature)."""
        assert "PARK" not in DEFAULT_BLOCKED_VENUE_TYPES
        assert "CITY_PARK" not in DEFAULT_BLOCKED_VENUE_TYPES

    def test_blocks_shopping(self):
        assert "SHOPPING" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "SHOPPING_CENTER" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "DEPARTMENT_STORE" in DEFAULT_BLOCKED_VENUE_TYPES

    def test_blocks_food_retail(self):
        assert "SUPERMARKET" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "GROCERY" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "BAKERY" in DEFAULT_BLOCKED_VENUE_TYPES

    def test_blocks_restaurants_and_cafes(self):
        assert "RESTAURANT" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "CAFE" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "COFFEE" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "FAST_FOOD" in DEFAULT_BLOCKED_VENUE_TYPES

    def test_blocks_services(self):
        for t in ["HOSPITAL", "PHARMACY", "BANK", "GAS_STATION", "GYM", "FITNESS"]:
            assert t in DEFAULT_BLOCKED_VENUE_TYPES, f"{t} should be blocked"

    def test_blocks_religious(self):
        assert "CHURCH" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "TEMPLE" in DEFAULT_BLOCKED_VENUE_TYPES

    def test_blocks_museums(self):
        assert "MUSEUM" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "MODERN_ART_MUSEUM" in DEFAULT_BLOCKED_VENUE_TYPES

    def test_blocks_sports(self):
        assert "SPORTS_COMPLEX" in DEFAULT_BLOCKED_VENUE_TYPES
        assert "GOLF" in DEFAULT_BLOCKED_VENUE_TYPES

    def test_does_not_block_nightlife(self):
        """Nightlife types must NEVER be in the blocked set."""
        for t in ["BAR", "BREWERY", "CLUBS", "CONCERT_HALL", "EVENT_VENUE",
                   "WINERY", "CASINO", "FOOD_AND_DRINK", "BEER", "BISTRO", "OTHER"]:
            assert t not in DEFAULT_BLOCKED_VENUE_TYPES, f"{t} should NOT be blocked"

    def test_no_overlap_with_venue_types(self):
        """Nothing in VENUE_TYPES should also be in BLOCKED — that would fetch then immediately drop."""
        overlap = set(VENUE_TYPES) & DEFAULT_BLOCKED_VENUE_TYPES
        assert overlap == set(), f"Types in both VENUE_TYPES and BLOCKED: {overlap}"


class TestVibeAttributesGoogleType:
    """Verify VibeAttributes stores Google Places type."""

    def test_google_primary_type_field_exists(self):
        from app.models.vibe_attributes import VibeAttributes
        attrs = VibeAttributes(venue_id="test123", google_primary_type="bar")
        assert attrs.google_primary_type == "bar"

    def test_google_primary_type_defaults_to_none(self):
        from app.models.vibe_attributes import VibeAttributes
        attrs = VibeAttributes(venue_id="test123")
        assert attrs.google_primary_type is None

    def test_google_place_id_stored(self):
        from app.models.vibe_attributes import VibeAttributes
        attrs = VibeAttributes(venue_id="test123", google_place_id="ChIJ_abc")
        assert attrs.google_place_id == "ChIJ_abc"


class TestMinifiedVenueGoogleType:
    """Verify MinifiedVenue includes google_places_type."""

    def test_google_places_type_field_exists(self):
        from app.models.venue import MinifiedVenue
        v = MinifiedVenue(
            forecast=True, processed=True,
            venue_address="test", venue_lat=-8.0, venue_lng=-34.9,
            venue_name="Test Bar", venue_type="OTHER",
            google_places_type="bar",
        )
        assert v.google_places_type == "bar"

    def test_google_places_type_defaults_to_none(self):
        from app.models.venue import MinifiedVenue
        v = MinifiedVenue(
            forecast=True, processed=True,
            venue_address="test", venue_lat=-8.0, venue_lng=-34.9,
            venue_name="Test Bar",
        )
        assert v.google_places_type is None

    def test_google_places_type_serializes(self):
        import json
        from app.models.venue import MinifiedVenue
        v = MinifiedVenue(
            forecast=True, processed=True,
            venue_address="test", venue_lat=-8.0, venue_lng=-34.9,
            venue_name="Test Bar", google_places_type="night_club",
        )
        data = json.loads(v.model_dump_json())
        assert data["google_places_type"] == "night_club"


class TestGooglePlacesDetailsPrimaryType:
    """Verify GooglePlacesDetailsResponse parses primaryType."""

    def test_primary_type_field(self):
        from app.models.vibe_attributes import GooglePlacesDetailsResponse
        resp = GooglePlacesDetailsResponse(place_id="ChIJ_test", primary_type="bar")
        assert resp.primary_type == "bar"

    def test_primary_type_none_by_default(self):
        from app.models.vibe_attributes import GooglePlacesDetailsResponse
        resp = GooglePlacesDetailsResponse(place_id="ChIJ_test")
        assert resp.primary_type is None


class TestBlockedGoogleTypes:
    """Verify BLOCKED_GOOGLE_TYPES catches junk from Google Places."""

    def test_blocks_garden_and_national_park_only(self):
        """park/city_park/plaza were unblocked (PARK category); garden and
        national_park stay blocked (park-category-eligibility feature)."""
        from app.services.venues_refresher_service import BLOCKED_GOOGLE_TYPES
        for t in ["garden", "national_park"]:
            assert t in BLOCKED_GOOGLE_TYPES
        for t in ["park", "city_park", "plaza"]:
            assert t not in BLOCKED_GOOGLE_TYPES

    def test_blocks_shopping(self):
        from app.services.venues_refresher_service import BLOCKED_GOOGLE_TYPES
        for t in ["shopping_mall", "department_store", "store"]:
            assert t in BLOCKED_GOOGLE_TYPES

    def test_blocks_museums(self):
        from app.services.venues_refresher_service import BLOCKED_GOOGLE_TYPES
        for t in ["museum", "art_museum", "history_museum"]:
            assert t in BLOCKED_GOOGLE_TYPES

    def test_does_not_block_nightlife(self):
        from app.services.venues_refresher_service import BLOCKED_GOOGLE_TYPES
        for t in ["bar", "night_club", "cocktail_bar", "pub", "irish_pub",
                   "bar_and_grill", "brewery", "bistro", "event_venue"]:
            assert t not in BLOCKED_GOOGLE_TYPES, f"{t} should NOT be blocked"

    def test_does_not_block_restaurants(self):
        from app.services.venues_refresher_service import BLOCKED_GOOGLE_TYPES
        for t in ["restaurant", "brazilian_restaurant", "buffet_restaurant",
                   "snack_bar", "cafeteria", "deli"]:
            assert t not in BLOCKED_GOOGLE_TYPES, f"{t} should NOT be blocked"
