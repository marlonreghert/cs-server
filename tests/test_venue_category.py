"""Unit tests for app/models/venue_category.py — PARK category resolution.

Covers the park-category-eligibility feature's resolution rules: the four
Google types (park, plaza, city_park, historical_landmark) and three BestTime
types (PARK, PLAZA, CITY_PARK) resolving to PARK, garden/national_park staying
OTHER, the PARK display tokens, and the granular labels.
"""
import json

import fakeredis
import pytest

from app.models.venue_category import (
    ADMIN_CONFIG_CATEGORY_MAP_KEY,
    CATEGORIES,
    GRANULAR_LABELS,
    get_category_info,
    get_granular_label,
    load_category_map,
    resolve_category,
    resolve_venue_display,
    validate_category_map_config,
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


class TestResolveCategoryWithInjectedMaps:
    def test_injected_google_map_wins(self):
        # hardware_store is not a default google type (→ OTHER); an injected map remaps it.
        assert resolve_category(google_type="hardware_store") == "OTHER"
        assert (
            resolve_category(
                google_type="hardware_store", google_map={"hardware_store": "FOOD_DRINK"}
            )
            == "FOOD_DRINK"
        )

    def test_injected_besttime_map_wins(self):
        assert (
            resolve_category(
                besttime_type="JUICE", besttime_map={"JUICE": "FOOD_DRINK"}
            )
            == "FOOD_DRINK"
        )

    def test_special_restaurant_vs_bar_rule_survives_injection(self):
        # Google=restaurant but BestTime=BAR still resolves to BAR even with maps
        # injected (the special rule is hardcoded, not table-driven).
        assert (
            resolve_category(
                google_type="restaurant",
                besttime_type="BAR",
                google_map={"restaurant": "RESTAURANT"},
                besttime_map={"BAR": "BAR"},
            )
            == "BAR"
        )

    def test_omitting_maps_preserves_default_behavior(self):
        assert resolve_category(google_type="bar") == "BAR"
        assert resolve_category(besttime_type="CLUBS") == "NIGHTCLUB"
        assert resolve_venue_display(google_type="bar")["category"] == "BAR"


class TestLoadCategoryMap:
    def test_none_redis_returns_defaults(self):
        google_map, besttime_map = load_category_map(None)
        assert google_map["bar"] == "BAR"
        assert besttime_map["CLUBS"] == "NIGHTCLUB"
        assert "hardware_store" not in google_map

    def test_missing_key_returns_defaults(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        google_map, _ = load_category_map(r)
        assert google_map["bar"] == "BAR"
        assert "hardware_store" not in google_map

    def test_malformed_json_falls_open_to_defaults(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        r.set(ADMIN_CONFIG_CATEGORY_MAP_KEY, "not-json{{{")
        google_map, besttime_map = load_category_map(r)
        assert google_map["bar"] == "BAR"
        assert besttime_map["CLUBS"] == "NIGHTCLUB"

    def test_non_object_falls_open_to_defaults(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        r.set(ADMIN_CONFIG_CATEGORY_MAP_KEY, json.dumps(["not", "a", "map"]))
        google_map, _ = load_category_map(r)
        assert google_map["bar"] == "BAR"

    def test_override_merges_over_defaults(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        r.set(
            ADMIN_CONFIG_CATEGORY_MAP_KEY,
            json.dumps({"google": {"bakery": "FOOD_DRINK"}, "besttime": {}}),
        )
        google_map, _ = load_category_map(r)
        assert google_map["bakery"] == "FOOD_DRINK"  # added
        assert google_map["bar"] == "BAR"  # default preserved

    def test_override_can_shadow_a_default(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        r.set(
            ADMIN_CONFIG_CATEGORY_MAP_KEY,
            json.dumps({"google": {"bar": "PUB"}}),
        )
        google_map, _ = load_category_map(r)
        assert google_map["bar"] == "PUB"

    def test_bad_entries_are_skipped_not_fatal(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        r.set(
            ADMIN_CONFIG_CATEGORY_MAP_KEY,
            json.dumps({"google": {"bakery": "FOOD_DRINK", "x": 5}}),
        )
        google_map, _ = load_category_map(r)
        assert google_map["bakery"] == "FOOD_DRINK"
        assert "x" not in google_map


class TestBakeryCategory:
    def test_bakery_is_a_category(self):
        assert CATEGORIES["BAKERY"]["label"] == "Padaria"
        assert CATEGORIES["BAKERY"]["emoji"] == "\U0001F950"  # 🥐

    def test_google_bakery_resolves_to_bakery(self):
        assert resolve_category(google_type="bakery") == "BAKERY"

    def test_bakery_display_full(self):
        display = resolve_venue_display(google_type="bakery")
        assert display["category"] == "BAKERY"
        assert display["label"] == "Padaria"
        assert display["emoji"] == "\U0001F950"
        assert display["granular_label"] == "Padaria"

    def test_bakery_granular_label(self):
        assert get_granular_label("bakery") == "Padaria"

    def test_admin_map_accepts_bakery_target(self):
        out = validate_category_map_config({"google": {"padaria_artesanal": "BAKERY"}})
        assert out["google"]["padaria_artesanal"] == "BAKERY"


class TestValidateCategoryMapConfig:
    def test_normalizes_key_casing(self):
        out = validate_category_map_config(
            {"google": {"BAKERY": "FOOD_DRINK"}, "besttime": {"juice": "FOOD_DRINK"}}
        )
        assert out == {
            "google": {"bakery": "FOOD_DRINK"},
            "besttime": {"JUICE": "FOOD_DRINK"},
        }

    def test_rejects_unknown_category(self):
        with pytest.raises(ValueError):
            validate_category_map_config({"google": {"bakery": "FOO"}})

    def test_rejects_non_dict_body(self):
        with pytest.raises(TypeError):
            validate_category_map_config(["not", "a", "map"])

    def test_rejects_non_dict_side(self):
        with pytest.raises(TypeError):
            validate_category_map_config({"google": ["bad"]})

    def test_rejects_non_string_values(self):
        with pytest.raises(TypeError):
            validate_category_map_config({"google": {"bakery": 5}})

    def test_accepts_partial_and_empty(self):
        assert validate_category_map_config({}) == {"google": {}, "besttime": {}}
        assert validate_category_map_config({"google": {"bakery": "FOOD_DRINK"}}) == {
            "google": {"bakery": "FOOD_DRINK"},
            "besttime": {},
        }

    def test_every_known_category_is_accepted(self):
        for cat in CATEGORIES:
            out = validate_category_map_config({"google": {"sometype": cat}})
            assert out["google"]["sometype"] == cat
