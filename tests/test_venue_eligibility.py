"""Unit tests for app/services/venue_eligibility.py."""
import json

import fakeredis
import pytest

from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    ADMIN_CONFIG_GEOFENCE_KEY,
    BLOCKED_NAME_KEYWORDS,
    DEFAULT_AMBIGUOUS_NAME_KEYWORDS,
    DEFAULT_GEO_FENCE,
    DEFAULT_HARD_BLOCKED_NAME_KEYWORDS,
    EligibilityConfig,
    REASON_BESTTIME_TYPE,
    REASON_EMPTY_NAME,
    REASON_GEO,
    REASON_GOOGLE_TYPE,
    REASON_NAME_KEYWORD,
    evaluate,
    geo_excluded,
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


# A point inside the default Recife box, and one outside it (São Paulo).
_IN = (-8.05, -34.88)
_OUT = (-23.55, -46.63)


class TestGeoExcluded:
    """geo_excluded() is a SEPARATE, reversible predicate — never a soft-delete.
    Serving membership is (not soft_deletable) AND (not geo_excluded)."""

    def test_inside_box_not_excluded(self):
        assert geo_excluded(_IN[0], _IN[1], DEFAULT_GEO_FENCE) is False

    def test_outside_box_excluded(self):
        assert geo_excluded(_OUT[0], _OUT[1], DEFAULT_GEO_FENCE) is True

    def test_on_boundary_is_inside(self):
        # Inclusive bounds: a venue exactly on min/max edges is inside.
        assert geo_excluded(
            DEFAULT_GEO_FENCE["min_lat"], DEFAULT_GEO_FENCE["min_lng"], DEFAULT_GEO_FENCE
        ) is False
        assert geo_excluded(
            DEFAULT_GEO_FENCE["max_lat"], DEFAULT_GEO_FENCE["max_lng"], DEFAULT_GEO_FENCE
        ) is False

    def test_missing_coords_fail_open(self):
        assert geo_excluded(None, None, DEFAULT_GEO_FENCE) is False
        assert geo_excluded(_OUT[0], None, DEFAULT_GEO_FENCE) is False
        assert geo_excluded(None, _OUT[1], DEFAULT_GEO_FENCE) is False

    def test_disabled_box_never_excludes(self):
        disabled = {**DEFAULT_GEO_FENCE, "enabled": False}
        assert geo_excluded(_OUT[0], _OUT[1], disabled) is False

    def test_none_or_malformed_box_fail_open(self):
        assert geo_excluded(_OUT[0], _OUT[1], None) is False
        assert geo_excluded(_OUT[0], _OUT[1], {}) is False
        assert geo_excluded(_OUT[0], _OUT[1], {"enabled": True}) is False  # missing keys

    def test_reason_geo_is_distinct(self):
        # ineligible_geo must not collide with the name/type reasons.
        assert REASON_GEO == "ineligible_geo"
        assert REASON_GEO not in (
            REASON_EMPTY_NAME, REASON_GOOGLE_TYPE, REASON_BESTTIME_TYPE, REASON_NAME_KEYWORD
        )


class TestValidateGeoFence:
    def _valid(self):
        return {"min_lat": -8.3, "max_lat": -7.85, "min_lng": -35.1, "max_lng": -34.8}

    def test_valid_defaults_enabled_true(self):
        box = validate_geo_fence(self._valid())
        assert box["enabled"] is True
        assert box["min_lat"] == -8.3 and box["max_lng"] == -34.8

    def test_missing_field_raises(self):
        bad = self._valid()
        del bad["max_lat"]
        with pytest.raises(ValueError):
            validate_geo_fence(bad)

    def test_min_ge_max_lat_raises(self):
        bad = {**self._valid(), "min_lat": 0.0, "max_lat": -10.0}
        with pytest.raises(ValueError):
            validate_geo_fence(bad)

    def test_min_ge_max_lng_raises(self):
        bad = {**self._valid(), "min_lng": 0.0, "max_lng": -10.0}
        with pytest.raises(ValueError):
            validate_geo_fence(bad)

    def test_lat_out_of_range_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence({**self._valid(), "min_lat": -100.0})

    def test_lng_out_of_range_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence({**self._valid(), "max_lng": 200.0})

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence({**self._valid(), "min_lat": "x"})

    def test_bool_is_not_a_number(self):
        with pytest.raises(ValueError):
            validate_geo_fence({**self._valid(), "min_lat": True})

    def test_non_bool_enabled_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence({**self._valid(), "enabled": "yes"})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            validate_geo_fence(["not", "a", "dict"])


class TestLoadGeoFence:
    def test_none_client_returns_default(self):
        assert load_geo_fence(None) == dict(DEFAULT_GEO_FENCE)

    def test_absent_key_returns_default(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        assert load_geo_fence(fake) == dict(DEFAULT_GEO_FENCE)

    def test_malformed_json_returns_default(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_GEOFENCE_KEY, "{not json")
        assert load_geo_fence(fake) == dict(DEFAULT_GEO_FENCE)

    def test_invalid_box_returns_default(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set(ADMIN_CONFIG_GEOFENCE_KEY, json.dumps({"min_lat": 0.0, "max_lat": -1.0}))
        assert load_geo_fence(fake) == dict(DEFAULT_GEO_FENCE)

    def test_valid_override_is_applied(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        box = {"min_lat": -9.0, "max_lat": -7.0, "min_lng": -36.0, "max_lng": -34.0, "enabled": False}
        fake.set(ADMIN_CONFIG_GEOFENCE_KEY, json.dumps(box))
        loaded = load_geo_fence(fake)
        assert loaded["min_lat"] == -9.0 and loaded["enabled"] is False
