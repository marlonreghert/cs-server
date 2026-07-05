"""Unit tests for app/models/venue_category.py — PARK category resolution.

Covers the park-category-eligibility feature's resolution rules: the four
Google types (park, plaza, city_park, historical_landmark) and three BestTime
types (PARK, PLAZA, CITY_PARK) resolving to PARK, garden/national_park staying
OTHER, the PARK display tokens, and the granular labels.
"""
from app.models.venue_category import (
    CATEGORIES,
    GRANULAR_LABELS,
    get_category_info,
    get_granular_label,
    resolve_category,
    resolve_venue_display,
)


class TestResolveCategoryGoogleTypesToPark:
    def test_park(self):
        assert resolve_category(google_type="park") == "PARK"

    def test_plaza(self):
        assert resolve_category(google_type="plaza") == "PARK"

    def test_city_park(self):
        assert resolve_category(google_type="city_park") == "PARK"

    def test_historical_landmark(self):
        assert resolve_category(google_type="historical_landmark") == "PARK"

    def test_case_insensitive(self):
        assert resolve_category(google_type="PARK") == "PARK"
        assert resolve_category(google_type="Plaza") == "PARK"


class TestResolveCategoryStaysOther:
    def test_garden(self):
        assert resolve_category(google_type="garden") == "OTHER"

    def test_national_park(self):
        assert resolve_category(google_type="national_park") == "OTHER"


class TestResolveCategoryBestTimeTypesToPark:
    def test_park(self):
        assert resolve_category(besttime_type="PARK") == "PARK"

    def test_plaza(self):
        assert resolve_category(besttime_type="PLAZA") == "PARK"

    def test_city_park(self):
        assert resolve_category(besttime_type="CITY_PARK") == "PARK"

    def test_case_insensitive(self):
        assert resolve_category(besttime_type="park") == "PARK"

    def test_besttime_used_when_no_google_type(self):
        assert resolve_category(google_type=None, besttime_type="CITY_PARK") == "PARK"


class TestParkDisplayTokens:
    def test_categories_entry(self):
        assert CATEGORIES["PARK"] == {
            "label": "Ao Ar Livre",
            "emoji": "🌳",
            "color": "#16A34A",
        }

    def test_get_category_info(self):
        info = get_category_info("PARK")
        assert info["label"] == "Ao Ar Livre"
        assert info["emoji"] == "🌳"
        assert info["color"] == "#16A34A"

    def test_resolve_venue_display_for_plaza(self):
        display = resolve_venue_display(google_type="plaza")
        assert display["category"] == "PARK"
        assert display["label"] == "Ao Ar Livre"
        assert display["emoji"] == "🌳"
        assert display["color"] == "#16A34A"
        assert display["granular_label"] == "Praça"


class TestGranularLabels:
    def test_plaza(self):
        assert GRANULAR_LABELS["plaza"] == "Praça"
        assert get_granular_label("plaza") == "Praça"

    def test_city_park(self):
        assert GRANULAR_LABELS["city_park"] == "Parque Urbano"
        assert get_granular_label("city_park") == "Parque Urbano"

    def test_park(self):
        assert GRANULAR_LABELS["park"] == "Parque"
        assert get_granular_label("park") == "Parque"

    def test_historical_landmark(self):
        assert GRANULAR_LABELS["historical_landmark"] == "Marco Histórico"
        assert get_granular_label("historical_landmark") == "Marco Histórico"
