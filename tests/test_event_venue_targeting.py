"""Unit tests for app/services/event_venue_targeting.py — the category gate,
the evidence-scoring pure functions, config parsing, and admin-config parsing.
See plans/260804_event-venue-targeting.md.

Tier-transition and full-run behavior (confirmed -> rejected on recompute,
zero-call guarantees, priority bounding) is covered end-to-end by
tests/bdd/enrichment/event-venue-targeting.feature; this file protects the
pure decision functions and config validators in isolation.
"""
import asyncio

import pytest

from app.models.venue_category import CATEGORIES
from app.services.event_venue_targeting import (
    DEFAULT_ALLOWED_CATEGORIES,
    EventCandidateCategoryConfig,
    InvalidEventTargetingConfig,
    TIER_EVIDENCE_CONFIRMED,
    TIER_EVIDENCE_REJECTED,
    ArchivedFlyerEvidenceSource,
    EventVenueTargetingService,
    NullFlyerEvidenceSource,
    caption_evidence,
    category_gate,
    evidence_verdict,
    load_event_candidate_categories,
    parse_event_targeting_config,
    resolve_event_candidate_ids,
    validate_event_candidate_categories_config,
)
from app.dao.venue_repository import VenueRepository
from app.models.instagram import InstagramPost, VenueInstagram, VenueInstagramPosts
from app.models.vibe_attributes import VibeAttributes
from app.models.vibe_profile import TaxonomyCategory, VenueVibeProfile
from app.models.venue import Venue
from app.models.venue_category import representative_google_type
from tests.rds_fake import InMemoryRdsVenueStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeRedis:
    def __init__(self, value=None):
        self.value = value

    def get(self, key):
        return self.value


class TestCategoryGateAcrossAllCategories:
    """Every category in CATEGORIES resolves through the gate without error,
    and the allow/deny split matches the shipped default posture."""

    @pytest.mark.parametrize("category", sorted(CATEGORIES.keys()))
    def test_every_category_resolves(self, category):
        result = category_gate(category, None)
        expected = category in DEFAULT_ALLOWED_CATEGORIES
        assert result.passed is expected
        if result.passed:
            assert result.reason == category

    def test_other_is_excluded_by_default(self):
        """Deliberately the opposite of venue_eligibility's block-list default:
        an unknown venue merely goes uncrawled here, so OTHER is NOT a
        candidate unless a vibe signal promotes it."""
        result = category_gate("OTHER", None)
        assert result.passed is False

    def test_unknown_category_string_treated_as_other(self):
        result = category_gate("NOT_A_REAL_CATEGORY", None)
        assert result.passed is False


class TestVibeSignalOverride:
    def test_music_format_override_promotes_a_failing_category(self):
        result = category_gate("RESTAURANT", {"music_format": ["Banda ao vivo"]})
        assert result.passed is True
        assert result.reason == "vibe:Banda ao vivo"

    def test_estilo_override_promotes_a_failing_category(self):
        result = category_gate("RESTAURANT", {"estilo_do_lugar": ["Balada"]})
        assert result.passed is True
        assert result.reason == "vibe:Balada"

    def test_non_override_label_does_not_promote(self):
        result = category_gate("RESTAURANT", {"music_format": ["Sertanejo"]})
        assert result.passed is False

    def test_venue_with_no_vibe_profile_is_not_promoted(self):
        result = category_gate("RESTAURANT", None)
        assert result.passed is False
        assert result.reason == "RESTAURANT"

    def test_passing_category_is_not_overridden_by_vibe_lookup(self):
        """A category that already passes should not even need the vibe
        signal — reason stays the category name, not a vibe label."""
        result = category_gate("NIGHTCLUB", {"music_format": ["DJ"]})
        assert result.passed is True
        assert result.reason == "NIGHTCLUB"


class TestEvidenceVerdict:
    def test_just_below_threshold_is_rejected(self):
        assert evidence_verdict(2, min_evidence_posts=3) == TIER_EVIDENCE_REJECTED

    def test_at_threshold_is_confirmed(self):
        assert evidence_verdict(3, min_evidence_posts=3) == TIER_EVIDENCE_CONFIRMED

    def test_just_above_threshold_is_confirmed(self):
        assert evidence_verdict(4, min_evidence_posts=3) == TIER_EVIDENCE_CONFIRMED

    def test_zero_score_is_rejected(self):
        assert evidence_verdict(0, min_evidence_posts=3) == TIER_EVIDENCE_REJECTED


class TestCaptionEvidence:
    def test_counts_only_matching_posts(self):
        posts = [
            {"caption": "Ingressos abertos!", "timestamp": "2026-08-01T20:00:00.000Z"},
            {"caption": "Bom dia a todos", "timestamp": "2026-08-01T20:00:00.000Z"},
        ]
        from datetime import datetime, timezone

        count, samples = caption_evidence(posts, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert count == 1
        assert samples == ["Ingressos abertos!"]

    def test_post_outside_lookback_window_is_excluded(self):
        from datetime import datetime, timezone

        posts = [{"caption": "Ingressos abertos!", "timestamp": "2020-01-01T00:00:00.000Z"}]
        count, _ = caption_evidence(posts, since=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert count == 0

    def test_post_with_unparseable_timestamp_is_kept(self):
        from datetime import datetime, timezone

        posts = [{"caption": "Ingressos abertos!", "timestamp": "not-a-date"}]
        count, _ = caption_evidence(posts, since=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert count == 1


class TestParseEventTargetingConfig:
    def test_defaults_applied_when_config_is_none(self):
        cfg = parse_event_targeting_config(None)
        assert cfg["max_evidence_venues"] == 25
        assert cfg["min_evidence_posts"] == 3
        assert cfg["lookback_days"] == 60
        assert cfg["recompute"] is False
        assert cfg["dry_run"] is False

    def test_overrides_applied(self):
        cfg = parse_event_targeting_config({
            "max_evidence_venues": 10, "min_evidence_posts": 5,
            "lookback_days": 30, "recompute": True, "dry_run": True,
        })
        assert cfg["max_evidence_venues"] == 10
        assert cfg["min_evidence_posts"] == 5
        assert cfg["lookback_days"] == 30
        assert cfg["recompute"] is True
        assert cfg["dry_run"] is True

    def test_negative_value_rejected(self):
        with pytest.raises(InvalidEventTargetingConfig):
            parse_event_targeting_config({"max_evidence_venues": -1})

    def test_non_numeric_value_rejected(self):
        with pytest.raises(InvalidEventTargetingConfig):
            parse_event_targeting_config({"lookback_days": "soon"})


class TestAdminConfigParsing:
    def test_valid_config_round_trips(self):
        cfg = EventCandidateCategoryConfig.from_dict({
            "allowed_categories": ["NIGHTCLUB", "RESTAURANT"],
        })
        assert cfg.allowed_categories == frozenset({"NIGHTCLUB", "RESTAURANT"})

    def test_empty_config_uses_defaults(self):
        cfg = EventCandidateCategoryConfig.from_dict({})
        assert cfg.allowed_categories == frozenset(DEFAULT_ALLOWED_CATEGORIES)

    def test_wrong_type_raises(self):
        with pytest.raises(TypeError):
            EventCandidateCategoryConfig.from_dict("not-a-dict")

    def test_wrong_type_for_list_field_raises(self):
        with pytest.raises(TypeError):
            EventCandidateCategoryConfig.from_dict({"allowed_categories": "NIGHTCLUB"})

    def test_unknown_category_name_raises(self):
        with pytest.raises(ValueError):
            EventCandidateCategoryConfig.from_dict({"allowed_categories": ["NOT_REAL"]})

    def test_load_falls_back_on_malformed_json(self):
        cfg, reason = load_event_candidate_categories(_FakeRedis("not-json{{"))
        assert cfg.allowed_categories == frozenset(DEFAULT_ALLOWED_CATEGORIES)
        assert reason == "invalid_json"

    def test_load_falls_back_on_invalid_shape(self):
        import json
        cfg, reason = load_event_candidate_categories(
            _FakeRedis(json.dumps({"allowed_categories": ["NOT_REAL"]}))
        )
        assert cfg.allowed_categories == frozenset(DEFAULT_ALLOWED_CATEGORIES)
        assert reason == "invalid_shape"

    def test_load_missing_key_is_not_a_fallback(self):
        cfg, reason = load_event_candidate_categories(_FakeRedis(None))
        assert cfg.allowed_categories == frozenset(DEFAULT_ALLOWED_CATEGORIES)
        assert reason is None

    def test_validator_normalizes_for_persistence(self):
        stored = validate_event_candidate_categories_config({
            "allowed_categories": ["nightclub", "Bar"],
        })
        assert stored["allowed_categories"] == ["BAR", "NIGHTCLUB"]

    def test_validator_rejects_unknown_category(self):
        with pytest.raises(ValueError):
            validate_event_candidate_categories_config({"allowed_categories": ["NOT_REAL"]})


def _venue(vid, *, category="NIGHTCLUB", priority=5, venue_source="besttime", has_ig=False):
    v = Venue(
        venue_id=vid, venue_name=f"Venue {vid}", venue_lat=-8.05, venue_lng=-34.88,
        priority=priority, venue_source=venue_source,
    )
    return v, category, has_ig


class TestTierTransitions:
    """Full-run tier transitions via the real EventVenueTargetingService over
    the in-memory RDS fake — confirmed -> rejected on recompute is the one
    scenario the feature file doesn't exercise directly (it tests skip vs
    recompute, not a REVERSED verdict on recompute)."""

    def _service(self, redis_value=None, flyer_source=None):
        store = InMemoryRdsVenueStore()
        dao = VenueRepository(client=None, rds_store=store)
        service = EventVenueTargetingService(
            venue_dao=dao, redis_client=_FakeRedis(redis_value),
            flyer_evidence_source=flyer_source or NullFlyerEvidenceSource(),
        )
        return store, dao, service

    def _seed_nightclub_with_instagram(self, dao, vid):
        dao.upsert_venue(Venue(
            venue_id=vid, venue_name=f"Venue {vid}", venue_lat=-8.05, venue_lng=-34.88,
        ))
        dao.set_vibe_attributes(VibeAttributes(
            venue_id=vid, google_primary_type=representative_google_type("NIGHTCLUB"),
        ))
        dao.set_venue_instagram(VenueInstagram(
            venue_id=vid, instagram_handle="h", status="found",
        ))

    def test_confirmed_flips_to_rejected_on_recompute_with_worse_evidence(self):
        class _CountingFlyerSource:
            def __init__(self):
                self.mode = "high"

            async def flyer_count(self, venue_id, since):
                return (5, []) if self.mode == "high" else (0, [])

        flyer = _CountingFlyerSource()
        store, dao, service = self._service(flyer_source=flyer)
        vid = "v1"
        self._seed_nightclub_with_instagram(dao, vid)

        result = _run(service.run({"min_evidence_posts": 3}))
        profile = dao.get_venue_event_profile(vid)
        assert profile["tier"] == TIER_EVIDENCE_CONFIRMED
        assert result["evidence_evaluated"] == 1

        flyer.mode = "low"
        _run(service.run({"min_evidence_posts": 3, "recompute": True}))
        profile = dao.get_venue_event_profile(vid)
        assert profile["tier"] == TIER_EVIDENCE_REJECTED


class TestResolveEventCandidateIds:
    def test_defaults_to_confirmed_only(self):
        store = InMemoryRdsVenueStore()
        dao = VenueRepository(client=None, rds_store=store)
        for vid, tier in [("a", "evidence_confirmed"), ("b", "category_candidate")]:
            dao.upsert_venue(Venue(
                venue_id=vid, venue_name=vid, venue_lat=-8.05, venue_lng=-34.88,
            ))
            dao.upsert_venue_event_profile(
                vid, tier=tier, category_pass=True, category_reason="NIGHTCLUB",
                evidence_score=5, evidence_sample=None, evaluated_at=None,
            )
        assert resolve_event_candidate_ids(dao) == ["a"]

    def test_include_category_candidates_widens_the_set(self):
        store = InMemoryRdsVenueStore()
        dao = VenueRepository(client=None, rds_store=store)
        for vid, tier in [("a", "evidence_confirmed"), ("b", "category_candidate")]:
            dao.upsert_venue(Venue(
                venue_id=vid, venue_name=vid, venue_lat=-8.05, venue_lng=-34.88,
            ))
            dao.upsert_venue_event_profile(
                vid, tier=tier, category_pass=True, category_reason="NIGHTCLUB",
                evidence_score=5, evidence_sample=None, evaluated_at=None,
            )
        assert resolve_event_candidate_ids(dao, include_category_candidates=True) == ["a", "b"]
