"""Unit tests for app/services/venue_eligibility.py."""
import json

import fakeredis
import pytest

from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    BLOCKED_NAME_KEYWORDS,
    DEFAULT_AMBIGUOUS_NAME_KEYWORDS,
    DEFAULT_HARD_BLOCKED_NAME_KEYWORDS,
    EligibilityConfig,
    REASON_BESTTIME_TYPE,
    REASON_EMPTY_NAME,
    REASON_GOOGLE_TYPE,
    REASON_NAME_KEYWORD,
    evaluate,
    load_eligibility_config,
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


class TestEligibilitySweepService:
    """Service-level coverage for the cache-first sweep and write-time filter."""

    def _refresher(self):
        from app.dao import RedisVenueDAO
        from app.db.geo_redis_client import GeoRedisClient
        from app.services.venues_refresher_service import VenuesRefresherService

        fake = fakeredis.FakeRedis(decode_responses=True)
        dao = RedisVenueDAO(GeoRedisClient(fake))
        refresher = VenuesRefresherService(
            venue_dao=dao, besttime_api=None, redis_client=fake
        )
        return refresher, dao

    def _seed(self, dao, venue_id, name, *, venue_type=None, google_type=None):
        from app.models import Venue
        from app.models.vibe_attributes import VibeAttributes

        dao.upsert_venue(
            Venue(
                venue_id=venue_id,
                venue_name=name,
                venue_address="addr",
                venue_lat=-8.05,
                venue_lng=-34.88,
                venue_type=venue_type,
            )
        )
        if google_type is not None:
            dao.set_vibe_attributes(
                VibeAttributes(venue_id=venue_id, google_primary_type=google_type)
            )

    @pytest.mark.asyncio
    async def test_sweep_soft_deletes_hard_keeps_ambiguous_and_unknown(self):
        refresher, dao = self._refresher()
        self._seed(dao, "hard", "Drogaria São Paulo")
        self._seed(dao, "amb", "Bar do Mercado")  # ambiguous, unlabeled
        self._seed(dao, "unknown", "Espaço XYZ", venue_type="OTHER")
        self._seed(dao, "gjunk", "Loja X", google_type="supermarket")

        summary = await refresher.run_eligibility_sweep()

        assert dao.get_venue("hard").is_deprecated()
        assert dao.get_venue("hard").deprecated_reason == REASON_NAME_KEYWORD
        assert dao.get_venue("gjunk").is_deprecated()
        assert dao.get_venue("gjunk").deprecated_reason == REASON_GOOGLE_TYPE
        assert dao.get_venue("amb").is_active()
        assert dao.get_venue("unknown").is_active()
        assert summary["soft_deleted"] == 2
        assert summary["kept"] == 2
        assert summary["by_reason"][REASON_NAME_KEYWORD] == 1
        assert summary["by_reason"][REASON_GOOGLE_TYPE] == 1

    @pytest.mark.asyncio
    async def test_sweep_is_idempotent(self):
        refresher, dao = self._refresher()
        self._seed(dao, "hard", "Igreja Batista")
        await refresher.run_eligibility_sweep()
        first = dao.get_venue("hard").deprecated_at
        summary = await refresher.run_eligibility_sweep()
        # Already deprecated → not re-seen, not reactivated, timestamp stable.
        assert summary["seen"] == 0
        assert dao.get_venue("hard").deprecated_at == first
        assert dao.get_venue("hard").is_deprecated()

    def test_write_time_born_deprecate_high_confidence_only(self):
        from app.models import Venue

        refresher, _ = self._refresher()
        config = EligibilityConfig.defaults()

        junk = Venue(venue_id="j", venue_name="", venue_lat=-8.0, venue_lng=-34.9)
        assert refresher._born_deprecate_if_ineligible(junk, config) == REASON_EMPTY_NAME
        assert junk.is_deprecated()

        amb = Venue(venue_id="a", venue_name="Bar do Mercado", venue_lat=-8.0, venue_lng=-34.9)
        assert refresher._born_deprecate_if_ineligible(amb, config) is None
        assert amb.is_active()
