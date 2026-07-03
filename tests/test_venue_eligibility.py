"""Unit tests for app/services/venue_eligibility.py."""
import json

import fakeredis
import pytest

from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    ADMIN_CONFIG_GEOFENCE_KEY,
    BLOCKED_NAME_KEYWORDS,
    CAPITALS_BY_SLUG,
    DEFAULT_AMBIGUOUS_NAME_KEYWORDS,
    DEFAULT_GEO_FENCE,
    DEFAULT_HARD_BLOCKED_NAME_KEYWORDS,
    EligibilityConfig,
    MAX_RADIUS_KM,
    MIN_RADIUS_KM,
    REASON_BESTTIME_TYPE,
    REASON_EMPTY_NAME,
    REASON_GEO,
    REASON_GOOGLE_TYPE,
    REASON_NAME_KEYWORD,
    STATE_CAPITALS,
    default_geo_fence,
    evaluate,
    geo_excluded,
    haversine_km,
    load_eligibility_config,
    load_geo_fence,
    validate_geo_fence,
)


class TestEvaluateReasons:
    def test_empty_name_high_confidence(self):
        r = evaluate("")
        assert r.reason == REASON_EMPTY_NAME and r.soft_deletable

    def test_whitespace_name_is_empty(self):
        assert evaluate("   ").reason == REASON_EMPTY_NAME

    def test_blocked_google_type_wins_over_keyword(self):
        # "farmácia" is a hard keyword, but Google type is the accurate signal.
        r = evaluate("Farmácia Pague Menos", google_type="pharmacy")
        assert r.reason == REASON_GOOGLE_TYPE and r.soft_deletable

    def test_blocked_besttime_type(self):
        r = evaluate("Some Parish", besttime_type="CHURCH")
        assert r.reason == REASON_BESTTIME_TYPE and r.soft_deletable

    def test_hard_keyword_unlabeled_high_confidence(self):
        r = evaluate("Drogaria São Paulo")
        assert r.reason == REASON_NAME_KEYWORD
        assert r.confidence == "high" and r.soft_deletable

    def test_ambiguous_keyword_unlabeled_low_confidence(self):
        r = evaluate("Bar do Mercado")
        assert r.reason == REASON_NAME_KEYWORD
        assert r.confidence == "low"
        assert not r.soft_deletable  # never soft-deleted before labeling

    def test_ambiguous_keyword_labeled_nongood_high(self):
        r = evaluate("Mercado Central", google_type="supermarket")
        # supermarket is itself a blocked Google type → google reason.
        assert r.reason == REASON_GOOGLE_TYPE


class TestGoodCategorySuppression:
    def test_ambiguous_suppressed_by_good_besttime_type(self):
        # "Bar do Mercado" typed BAR by BestTime stays eligible.
        assert evaluate("Bar do Mercado", besttime_type="BAR").eligible

    def test_ambiguous_suppressed_by_good_google_type(self):
        assert evaluate("Parque Bar", google_type="bar").eligible

    def test_hard_keyword_suppressed_by_good_category(self):
        # Themed bars exist ("Bar Farmácia"); a positive category wins (safest).
        assert evaluate("Bar Farmácia", besttime_type="BAR").eligible

    def test_unknown_unlabeled_is_eligible(self):
        # Block-list policy: unknown/unlabeled venues stay eligible.
        r = evaluate("Espaço Cultural XYZ", besttime_type="OTHER")
        assert r.eligible

    def test_plain_name_eligible(self):
        assert evaluate("Boteco do Zé").eligible


class TestKeywordSplit:
    def test_every_keyword_in_exactly_one_list(self):
        hard = set(DEFAULT_HARD_BLOCKED_NAME_KEYWORDS)
        ambiguous = set(DEFAULT_AMBIGUOUS_NAME_KEYWORDS)
        assert hard.isdisjoint(ambiguous)
        assert set(BLOCKED_NAME_KEYWORDS) == hard | ambiguous

    def test_ambiguous_holds_bar_name_tokens(self):
        for token in ("mercado", "parque", "praça", "shopping"):
            assert token in DEFAULT_AMBIGUOUS_NAME_KEYWORDS

    def test_hard_holds_unambiguous_tokens(self):
        for token in ("drogaria", "igreja", "hospital", "farmácia"):
            assert token in DEFAULT_HARD_BLOCKED_NAME_KEYWORDS


class TestEligibilityConfigFromDict:
    def test_invalid_non_list_raises(self):
        with pytest.raises(ValueError):
            EligibilityConfig.from_dict({"blocked_venue_types": "not-a-list"})

    def test_invalid_list_with_non_string_raises(self):
        with pytest.raises(ValueError):
            EligibilityConfig.from_dict({"blocked_google_types": ["bar", 7]})

    def test_absent_fields_fall_back_to_defaults(self):
        cfg = EligibilityConfig.from_dict({})
        assert "CHURCH" in cfg.blocked_venue_types
        assert "pharmacy" in cfg.blocked_google_types

    def test_blocked_name_keywords_alias_appends_to_hard(self):
        cfg = EligibilityConfig.from_dict({"blocked_name_keywords": ["lounge"]})
        assert "lounge" in cfg.hard_blocked_name_keywords
        assert not evaluate("Sunset Lounge", config=cfg).eligible

    def test_to_public_dict_marks_source(self):
        assert EligibilityConfig.defaults().to_public_dict()["source"] == "defaults"
        override = EligibilityConfig.from_dict({}, from_admin_override=True)
        assert override.to_public_dict()["source"] == "admin_override"


class TestLoadEligibilityConfig:
    def test_none_client_returns_defaults(self):
        cfg = load_eligibility_config(None)
        assert not cfg.from_admin_override

    def test_missing_key_returns_defaults(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        cfg = load_eligibility_config(fake)
        assert not cfg.from_admin_override

    def test_malformed_json_falls_back_to_defaults(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_ELIGIBILITY_KEY, "{not json")
        cfg = load_eligibility_config(fake)
        assert not cfg.from_admin_override

    def test_invalid_shape_falls_back_to_defaults(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_ELIGIBILITY_KEY, json.dumps({"blocked_venue_types": "x"}))
        cfg = load_eligibility_config(fake)
        assert not cfg.from_admin_override

    def test_valid_override_is_applied(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(
            ADMIN_CONFIG_ELIGIBILITY_KEY,
            json.dumps({"blocked_name_keywords": ["lounge"]}),
        )
        cfg = load_eligibility_config(fake)
        assert cfg.from_admin_override
        assert "lounge" in cfg.hard_blocked_name_keywords


# The eligibility sweep + write-time born-deprecate were retired: eligibility is
# now a non-destructive serving view (serving.eligible_venue), exercised by
# tests/test_eligibility_serving_view_parity.py and the eligibility-serving-view
# BDD feature. evaluate() remains the parity reference and is covered above.


# Points relative to the default fence (recife @ 40 km): near the Recife
# center (inside), and São Paulo (far outside every circle).
_IN = (-8.05, -34.88)
_OUT = (-23.55, -46.63)
_RECIFE = CAPITALS_BY_SLUG["recife"]
_SALVADOR = CAPITALS_BY_SLUG["salvador"]
# Degrees of latitude per km at the predicate's Earth radius (pure-north offset).
_DEG_PER_KM = 1 / 111.195


def _fence(*pairs, enabled=True):
    return {
        "enabled": enabled,
        "cities": [
            {**CAPITALS_BY_SLUG[slug], "radius_km": float(r)} for slug, r in pairs
        ],
    }


class TestStateCapitalsCatalog:
    def test_27_capitals_with_unique_slugs(self):
        assert len(STATE_CAPITALS) == 27
        slugs = [c["slug"] for c in STATE_CAPITALS]
        assert len(set(slugs)) == 27

    def test_coordinates_inside_brazil(self):
        # Every capital lies within Brazil's bounding region — catches a
        # transposed lat/lng or a dropped minus sign.
        for c in STATE_CAPITALS:
            assert -34.0 <= c["lat"] <= 6.0, c
            assert -75.0 <= c["lng"] <= -34.0, c

    def test_recife_center_is_pinned(self):
        assert _RECIFE["lat"] == pytest.approx(-8.0476)
        assert _RECIFE["lng"] == pytest.approx(-34.8770)

    def test_default_fence_is_recife_at_40km_enabled(self):
        assert DEFAULT_GEO_FENCE["enabled"] is True
        assert [c["slug"] for c in DEFAULT_GEO_FENCE["cities"]] == ["recife"]
        assert DEFAULT_GEO_FENCE["cities"][0]["radius_km"] == 40.0

    def test_default_geo_fence_returns_a_fresh_deep_copy(self):
        copy1 = default_geo_fence()
        copy1["cities"][0]["radius_km"] = 999
        copy1["enabled"] = False
        assert DEFAULT_GEO_FENCE["cities"][0]["radius_km"] == 40.0
        assert DEFAULT_GEO_FENCE["enabled"] is True


class TestGeoExcluded:
    """geo_excluded() is a SEPARATE, reversible predicate — never a soft-delete.
    Serving membership is (not soft_deletable) AND (not geo_excluded)."""

    def test_inside_default_circle_not_excluded(self):
        assert geo_excluded(_IN[0], _IN[1], DEFAULT_GEO_FENCE) is False

    def test_outside_every_circle_excluded(self):
        assert geo_excluded(_OUT[0], _OUT[1], DEFAULT_GEO_FENCE) is True

    def test_boundary_one_meter_each_side(self):
        fence = _fence(("recife", 40))
        just_inside = _RECIFE["lat"] + 39.999 * _DEG_PER_KM
        just_outside = _RECIFE["lat"] + 40.001 * _DEG_PER_KM
        assert geo_excluded(just_inside, _RECIFE["lng"], fence) is False
        assert geo_excluded(just_outside, _RECIFE["lng"], fence) is True

    def test_inside_any_circle_is_enough(self):
        fence = _fence(("recife", 30), ("salvador", 25))
        near_salvador = (_SALVADOR["lat"] + 10 * _DEG_PER_KM, _SALVADOR["lng"])
        assert geo_excluded(near_salvador[0], near_salvador[1], fence) is False
        # São Paulo is outside both circles.
        assert geo_excluded(_OUT[0], _OUT[1], fence) is True

    def test_missing_coords_fail_open(self):
        assert geo_excluded(None, None, DEFAULT_GEO_FENCE) is False
        assert geo_excluded(_OUT[0], None, DEFAULT_GEO_FENCE) is False
        assert geo_excluded(None, _OUT[1], DEFAULT_GEO_FENCE) is False

    def test_disabled_fence_never_excludes(self):
        disabled = _fence(("recife", 40), enabled=False)
        assert geo_excluded(_OUT[0], _OUT[1], disabled) is False

    def test_empty_city_list_fails_open(self):
        assert geo_excluded(_OUT[0], _OUT[1], {"enabled": True, "cities": []}) is False

    def test_none_or_malformed_fence_fails_open(self):
        assert geo_excluded(_OUT[0], _OUT[1], None) is False
        assert geo_excluded(_OUT[0], _OUT[1], {}) is False
        assert geo_excluded(_OUT[0], _OUT[1], {"enabled": True}) is False  # no cities
        assert geo_excluded(_OUT[0], _OUT[1], {"enabled": True, "cities": "x"}) is False
        # A city entry missing its keys must fail open, not crash filtering.
        assert geo_excluded(
            _OUT[0], _OUT[1], {"enabled": True, "cities": [{"slug": "recife"}]}
        ) is False

    def test_haversine_matches_known_distance(self):
        # Recife → Salvador is ≈675 km (great-circle, mean Earth radius).
        d = haversine_km(_RECIFE["lat"], _RECIFE["lng"], _SALVADOR["lat"], _SALVADOR["lng"])
        assert d == pytest.approx(675, abs=10)

    def test_reason_geo_is_distinct(self):
        # ineligible_geo must not collide with the name/type reasons.
        assert REASON_GEO == "ineligible_geo"
        assert REASON_GEO not in (
            REASON_EMPTY_NAME, REASON_GOOGLE_TYPE, REASON_BESTTIME_TYPE, REASON_NAME_KEYWORD
        )


class TestValidateGeoFence:
    def _valid(self):
        return {"enabled": True, "cities": [{"slug": "recife", "radius_km": 40}]}

    def test_valid_resolves_catalog_coords(self):
        fence = validate_geo_fence(self._valid())
        assert fence["enabled"] is True
        city = fence["cities"][0]
        assert city["slug"] == "recife" and city["name"] == "Recife"
        assert city["lat"] == _RECIFE["lat"] and city["lng"] == _RECIFE["lng"]
        assert city["radius_km"] == 40.0

    def test_enabled_defaults_true(self):
        assert validate_geo_fence({"cities": [{"slug": "recife", "radius_km": 40}]})[
            "enabled"
        ] is True

    def test_caller_sent_coords_are_ignored(self):
        # The server owns coordinates: a payload smuggling lat/lng resolves to
        # the catalog values, not the caller's.
        fence = validate_geo_fence({
            "enabled": True,
            "cities": [{"slug": "recife", "radius_km": 40, "lat": 0.0, "lng": 0.0}],
        })
        assert fence["cities"][0]["lat"] == _RECIFE["lat"]
        assert fence["cities"][0]["lng"] == _RECIFE["lng"]

    def test_cities_sorted_by_name(self):
        fence = validate_geo_fence({"enabled": True, "cities": [
            {"slug": "salvador", "radius_km": 25},
            {"slug": "recife", "radius_km": 30},
        ]})
        assert [c["slug"] for c in fence["cities"]] == ["recife", "salvador"]

    def test_unknown_slug_raises(self):
        with pytest.raises(ValueError, match="unknown capital slug"):
            validate_geo_fence({"enabled": True, "cities": [{"slug": "caruaru", "radius_km": 30}]})

    def test_non_string_slug_raises(self):
        with pytest.raises(ValueError, match="unknown capital slug"):
            validate_geo_fence({"enabled": True, "cities": [{"slug": 7, "radius_km": 30}]})

    def test_duplicate_slug_raises(self):
        with pytest.raises(ValueError, match="duplicate capital slug"):
            validate_geo_fence({"enabled": True, "cities": [
                {"slug": "recife", "radius_km": 30},
                {"slug": "recife", "radius_km": 40},
            ]})

    def test_radius_bounds_inclusive(self):
        for ok in (MIN_RADIUS_KM, MAX_RADIUS_KM):
            fence = validate_geo_fence(
                {"enabled": True, "cities": [{"slug": "recife", "radius_km": ok}]}
            )
            assert fence["cities"][0]["radius_km"] == ok

    @pytest.mark.parametrize("bad", [0, 0.5, 201, -40, "40", True, None])
    def test_bad_radius_raises(self, bad):
        with pytest.raises(ValueError, match="radius_km"):
            validate_geo_fence({"enabled": True, "cities": [{"slug": "recife", "radius_km": bad}]})

    def test_missing_radius_raises(self):
        with pytest.raises(ValueError, match="radius_km"):
            validate_geo_fence({"enabled": True, "cities": [{"slug": "recife"}]})

    def test_enabled_with_no_cities_raises(self):
        with pytest.raises(ValueError, match="at least one city"):
            validate_geo_fence({"enabled": True, "cities": []})

    def test_disabled_with_no_cities_is_valid(self):
        fence = validate_geo_fence({"enabled": False, "cities": []})
        assert fence == {"enabled": False, "cities": []}

    def test_legacy_box_payload_raises_naming_cities(self):
        legacy = {"min_lat": -8.3, "max_lat": -7.85, "min_lng": -35.1, "max_lng": -34.8}
        with pytest.raises(ValueError, match="cities"):
            validate_geo_fence(legacy)

    def test_missing_cities_raises(self):
        with pytest.raises(ValueError, match="cities"):
            validate_geo_fence({"enabled": True})

    def test_non_list_cities_raises(self):
        with pytest.raises(ValueError, match="cities"):
            validate_geo_fence({"enabled": True, "cities": "recife"})

    def test_non_dict_city_entry_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence({"enabled": True, "cities": ["recife"]})

    def test_non_bool_enabled_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence({**self._valid(), "enabled": "yes"})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence(["not", "a", "dict"])


class TestLoadGeoFence:
    def test_none_client_returns_default(self):
        assert load_geo_fence(None) == DEFAULT_GEO_FENCE

    def test_absent_key_returns_default(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        assert load_geo_fence(fake) == DEFAULT_GEO_FENCE

    def test_malformed_json_returns_default(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_GEOFENCE_KEY, "{not json")
        assert load_geo_fence(fake) == DEFAULT_GEO_FENCE

    def test_legacy_box_mirror_returns_default(self):
        # A pre-0015 mirror blob (the old bounding box) must degrade to the
        # default fence, not break filtering.
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_GEOFENCE_KEY, json.dumps({
            "min_lat": -8.3, "max_lat": -7.85,
            "min_lng": -35.1, "max_lng": -34.8, "enabled": True,
        }))
        assert load_geo_fence(fake) == DEFAULT_GEO_FENCE

    def test_invalid_shape_returns_default(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_GEOFENCE_KEY, json.dumps(
            {"enabled": True, "cities": [{"slug": "narnia", "radius_km": 30}]}
        ))
        assert load_geo_fence(fake) == DEFAULT_GEO_FENCE

    def test_valid_override_is_applied(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_GEOFENCE_KEY, json.dumps(
            {"enabled": False, "cities": [{"slug": "salvador", "radius_km": 25}]}
        ))
        loaded = load_geo_fence(fake)
        assert loaded["enabled"] is False
        assert [c["slug"] for c in loaded["cities"]] == ["salvador"]
        assert loaded["cities"][0]["lat"] == _SALVADOR["lat"]  # catalog-resolved
