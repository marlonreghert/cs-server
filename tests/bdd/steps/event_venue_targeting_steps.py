"""Behave steps for tests/bdd/enrichment/event-venue-targeting.feature.

Drives the REAL EventVenueTargetingService, category_gate, evidence gate, and
the event_candidates eligibility mode over a REAL VenueRepository backed by
the shared in-memory RDS fake (tests/rds_fake.py — the deterministic stand-in
required by AGENTS.md; a real Postgres is validated post-provisioning, not
here). The only test-only fakes are the Instagram-archive evidence source
(no live Apify/S3) and the admin-config Redis mirror (dict-backed get/set).

Background resets all shared state fresh for each scenario (Background steps
run before every scenario in a feature), so scenario-specific "Given N
venues..." counts are never polluted by another scenario's fixtures.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.dao.venue_repository import VenueRepository
from app.models.instagram import InstagramPost, VenueInstagram, VenueInstagramPosts
from app.models.vibe_attributes import VibeAttributes
from app.models.vibe_profile import TaxonomyCategory, VenueVibeProfile
from app.models.venue import Venue
from app.models.venue_category import representative_google_type
from app.services.event_venue_targeting import (
    ADMIN_CONFIG_EVENT_CANDIDATE_CATEGORIES_KEY,
    DEFAULT_ALLOWED_CATEGORIES,
    EventVenueTargetingService,
    TIER_CATEGORY_CANDIDATE,
    TIER_EVIDENCE_REJECTED,
    TIER_EXCLUDED_CATEGORY,
    load_event_candidate_categories,
)
from tests.rds_fake import InMemoryRdsVenueStore

_LOOP: "asyncio.AbstractEventLoop | None" = None
RECIFE_LAT, RECIFE_LNG = -8.05, -34.88  # inside the default 40km Recife geo-fence


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── fakes at the true boundary (no live Apify/S3, no live Redis) ─────────────
class _FakeAdminRedis:
    """Minimal admin_config mirror: get/set of JSON strings, like the real
    Redis mirror AdminConfigService/load_event_candidate_categories read."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


class _FakeFlyerEvidenceSource:
    """Deterministic evidence source: per-venue flyer counts injected directly
    by the scenario, no S3/Apify manifest walking involved. Counts calls so
    scenarios can assert "no external call made"."""

    def __init__(self):
        self.counts: dict[str, tuple[int, list[str]]] = {}
        self.calls = 0

    async def flyer_count(self, venue_id, since):
        self.calls += 1
        return self.counts.get(venue_id, (0, []))


# ── context setup (Background resets everything fresh per scenario) ─────────
def _reset_context(context) -> None:
    context.rds_store = InMemoryRdsVenueStore()
    context.event_dao = VenueRepository(client=None, rds_store=context.rds_store)
    context.event_redis = _FakeAdminRedis()
    context.event_flyer_source = _FakeFlyerEvidenceSource()
    context.event_service = EventVenueTargetingService(
        venue_dao=context.event_dao,
        redis_client=context.event_redis,
        flyer_evidence_source=context.event_flyer_source,
    )
    context.event_venue_counter = 0
    context.event_venue_ids: dict[str, str] = {}
    context.event_tier_venues: dict[str, list[str]] = {}
    context.event_last_venue_id = None
    context.event_recent_batch: list[str] = []
    context.event_run_config: dict = {}
    context.event_result = None
    context.event_selected_ids: list[str] = []
    context.event_summary: list[dict] = []


def _ensure_context(context) -> None:
    if getattr(context, "event_dao", None) is None:
        _reset_context(context)


def _new_venue_id(context) -> str:
    context.event_venue_counter += 1
    return f"event_venue_{context.event_venue_counter}"


def _create_venue(
    context, category, *, priority=5, venue_source="besttime",
    reviews=None, rating=None,
) -> str:
    vid = _new_venue_id(context)
    venue = Venue(
        venue_id=vid, venue_name=f"Venue {vid}",
        venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
        priority=priority, venue_source=venue_source,
        reviews=reviews, rating=rating,
    )
    context.event_dao.upsert_venue(venue)
    google_type = representative_google_type(category) if category != "OTHER" else None
    context.event_dao.set_vibe_attributes(
        VibeAttributes(venue_id=vid, google_primary_type=google_type)
    )
    context.event_last_venue_id = vid
    return vid


def _ensure_instagram(context, venue_id: str) -> None:
    instagram = context.event_dao.get_venue_instagram(venue_id)
    if instagram is not None and instagram.has_instagram():
        return
    context.event_dao.set_venue_instagram(VenueInstagram(
        venue_id=venue_id, instagram_handle=f"{venue_id}_ig", status="found",
    ))


def _run_targeting(context, **overrides) -> dict:
    cfg = dict(context.event_run_config or {})
    cfg.update(overrides)
    context.event_result = _run(context.event_service.run(cfg))
    return context.event_result


# ── Background ────────────────────────────────────────────────────────────────
@given("the servable catalog holds venues of several categories")
def step_given_the_servable_catalog_holds_venues_of_several_categories(context):
    _reset_context(context)


@given("the event candidate categories are the shipped defaults")
def step_given_the_event_candidate_categories_are_the_shipped_defaults(context):
    _ensure_context(context)
    # No admin override written -> load_event_candidate_categories falls
    # through to the in-code defaults on its own.


# ── category gate givens ──────────────────────────────────────────────────────
@given('a venue whose category is "{category}"')
def step_given_a_venue_whose_category_is_category(context, category):
    _ensure_context(context)
    vid = _create_venue(context, category)
    context.event_venue_ids[category] = vid


@given('its vibe profile carries the music format "{label}"')
def step_given_its_vibe_profile_carries_the_music_format_label(context, label):
    vid = context.event_last_venue_id
    context.event_dao.set_venue_vibe_profile(VenueVibeProfile(
        venue_id=vid, music_format=TaxonomyCategory(labels=[label]),
    ))


@given('the admin config adds "{category}" to the event candidate categories')
def step_given_the_admin_config_adds_category_to_the_event_candidate_catego(context, category):
    _ensure_context(context)
    allowed = list(DEFAULT_ALLOWED_CATEGORIES) + [category]
    context.event_redis.set(
        ADMIN_CONFIG_EVENT_CANDIDATE_CATEGORIES_KEY,
        json.dumps({"allowed_categories": allowed}),
    )


@given("the admin config for event candidate categories is malformed")
def step_given_the_admin_config_for_event_candidate_categories_is_malformed(context):
    _ensure_context(context)
    context.event_redis.set(ADMIN_CONFIG_EVENT_CANDIDATE_CATEGORIES_KEY, "not-json{{")
    # A malformed admin config falling back to defaults must not empty the
    # candidate set — this venue is what makes "not empty" a meaningful check.
    _create_venue(context, "NIGHTCLUB")


# ── priority / evidence bound givens ──────────────────────────────────────────
@given("{n:d} venues pass the category gate")
def step_given_n_d_venues_pass_the_category_gate(context, n):
    _ensure_context(context)
    for i in range(n):
        _create_venue(context, "NIGHTCLUB", priority=i, reviews=1000 - i)


@given("the run evaluates at most {n:d} venues for evidence")
def step_given_the_run_evaluates_at_most_n_d_venues_for_evidence(context, n):
    context.event_run_config["max_evidence_venues"] = n


@given('a venue whose source is "{source}"')
def step_given_a_venue_whose_source_is_source(context, source):
    _ensure_context(context)
    _create_venue(context, "NIGHTCLUB", venue_source=source)


@given("it passes the category gate")
def step_given_it_passes_the_category_gate(context):
    pass  # the venue's category (set at creation) already passes by default.


@given("it is within the top N by priority")
def step_given_it_is_within_the_top_n_by_priority(context):
    vid = context.event_last_venue_id
    context.rds_store.venues[vid]["priority"] = 0
    context.event_run_config.setdefault("max_evidence_venues", 25)


# ── evidence gate givens ──────────────────────────────────────────────────────
@given("a venue that passed the category gate")
def step_given_a_venue_that_passed_the_category_gate(context):
    _ensure_context(context)
    _create_venue(context, "NIGHTCLUB")


@given('it has {n:d} photos classified as "flyer" within the lookback window')
def step_given_it_has_n_d_photos_classified_as_flyer_within_the_lookback_wi(context, n):
    vid = context.event_last_venue_id
    _ensure_instagram(context, vid)
    context.event_flyer_source.counts[vid] = (n, [f"{n} flyer photo(s)"])


@given("it has {n:d} post of event evidence within the lookback window")
@given("it has {n:d} posts of event evidence within the lookback window")
def step_given_it_has_n_d_posts_of_event_evidence_within_the_lookback_windo(context, n):
    vid = context.event_last_venue_id
    _ensure_instagram(context, vid)
    posts = [
        InstagramPost(
            caption=f"Ingressos abertos para o evento de hoje #{i}!",
            timestamp="2026-08-01T20:00:00.000Z",
        )
        for i in range(n)
    ]
    context.event_dao.set_venue_ig_posts(
        VenueInstagramPosts(venue_id=vid, instagram_handle=f"{vid}_ig", posts=posts)
    )


@given("the run requires {n:d} posts of evidence")
def step_given_the_run_requires_n_d_posts_of_evidence(context, n):
    context.event_run_config["min_evidence_posts"] = n


@given("it has no Instagram handle")
def step_given_it_has_no_instagram_handle(context):
    pass  # no-op: "a venue that passed the category gate" sets no handle.


# ── tier fixtures (summary / eligibility-mode / recompute scenarios) ─────────
@given('a venue with the tier "{tier}" and no evaluation timestamp')
def step_given_a_venue_with_the_tier_tier_and_no_evaluation_timestamp(context, tier):
    _ensure_context(context)
    vid = _create_venue(context, "NIGHTCLUB")
    context.event_dao.upsert_venue_event_profile(
        vid, tier=tier, category_pass=True, category_reason="NIGHTCLUB",
        evidence_score=None, evidence_sample=None, evaluated_at=None,
    )
    context.event_tier_venues.setdefault(tier, []).append(vid)


@given('a venue with the tier "{tier}"')
def step_given_a_venue_with_the_tier_tier(context, tier):
    _ensure_context(context)
    vid = _create_venue(context, "NIGHTCLUB")
    evaluated_at = (
        None if tier == TIER_CATEGORY_CANDIDATE
        else datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    context.event_dao.upsert_venue_event_profile(
        vid, tier=tier, category_pass=True, category_reason="NIGHTCLUB",
        evidence_score=5, evidence_sample={"note": "seed"}, evaluated_at=evaluated_at,
    )
    context.event_tier_venues.setdefault(tier, []).append(vid)


@given('{n:d} venues with the tier "{tier}"')
def step_given_n_d_venues_with_the_tier_tier(context, n, tier):
    _ensure_context(context)
    for _ in range(n):
        vid = _create_venue(context, "NIGHTCLUB")
        evaluated_at = (
            None if tier == TIER_CATEGORY_CANDIDATE
            else datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        context.event_dao.upsert_venue_event_profile(
            vid, tier=tier, category_pass=True, category_reason="NIGHTCLUB",
            evidence_score=5, evidence_sample={"note": "seed"}, evaluated_at=evaluated_at,
        )
        context.event_tier_venues.setdefault(tier, []).append(vid)


@given("the run caps venues at {n:d}")
def step_given_the_run_caps_venues_at_n_d(context, n):
    context.event_run_config["max_venues"] = n


@given('a venue already recorded with the tier "{tier}"')
def step_given_a_venue_already_recorded_with_the_tier_tier(context, tier):
    _ensure_context(context)
    vid = _create_venue(context, "NIGHTCLUB")
    _ensure_instagram(context, vid)
    context.event_dao.upsert_venue_event_profile(
        vid, tier=tier, category_pass=True, category_reason="NIGHTCLUB",
        evidence_score=5, evidence_sample={"flyer_count": 5, "caption_matches": 0, "samples": []},
        evaluated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    context.event_seed_evaluated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Fresh evidence still confirms on recompute (score stays >= default
    # threshold), so a re-evaluation is provable purely by evaluated_at moving.
    context.event_flyer_source.counts[vid] = (5, ["seed"])


# ── dry-run / batch givens ─────────────────────────────────────────────────────
@given("{n:d} venues that passed the category gate")
def step_given_n_d_venues_that_passed_the_category_gate(context, n):
    _ensure_context(context)
    context.event_recent_batch = [_create_venue(context, "NIGHTCLUB") for _ in range(n)]


# ── when ──────────────────────────────────────────────────────────────────────
@when("event targeting runs")
def step_when_event_targeting_runs(context):
    _run_targeting(context)


@when("event targeting runs with the default settings")
def step_when_event_targeting_runs_with_the_default_settings(context):
    _run_targeting(context)


@when("event targeting runs as a dry run")
def step_when_event_targeting_runs_as_a_dry_run(context):
    _run_targeting(context, dry_run=True)


@when("event targeting runs without recompute")
def step_when_event_targeting_runs_without_recompute(context):
    _run_targeting(context, recompute=False)


@when("event targeting runs with recompute")
def step_when_event_targeting_runs_with_recompute(context):
    _run_targeting(context, recompute=True)


@when("the targeting summary is requested")
def step_when_the_targeting_summary_is_requested(context):
    context.event_summary = context.event_dao.list_venue_event_profiles()


@when('a run is targeted with the eligibility mode "{mode}"')
def step_when_a_run_is_targeted_with_the_eligibility_mode_mode(context, mode):
    from app.services.venue_photo_archive_service import VenuePhotoArchiveService, parse_config

    archive = VenuePhotoArchiveService(
        google_places_client=None, venue_dao=context.event_dao, media_store=None,
    )
    raw_cfg = dict(context.event_run_config or {})
    raw_cfg["eligibility"] = {"mode": mode}
    cfg = parse_config(raw_cfg, default_max_venues=1000, default_max_photos=5)
    selected, _unknown, _eligible_total = archive.select_venues(cfg)
    context.event_selected_ids = selected


# ── then: category gate ───────────────────────────────────────────────────────
@then("the nightclub passes the category gate")
def step_then_the_nightclub_passes_the_category_gate(context):
    vid = context.event_venue_ids["NIGHTCLUB"]
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile is not None, "no venue_event_profile written for the nightclub"
    assert profile["category_pass"] is True, profile
    assert profile["tier"] != TIER_EXCLUDED_CATEGORY, profile


@then('the restaurant is recorded with the tier "{tier}"')
def step_then_the_restaurant_is_recorded_with_the_tier_tier(context, tier):
    vid = context.event_venue_ids["RESTAURANT"]
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile is not None, "no venue_event_profile written for the restaurant"
    assert profile["tier"] == tier, profile


@then("no external call is made")
def step_then_no_external_call_is_made(context):
    assert context.event_flyer_source.calls == 0


@then("that venue passes the category gate")
def step_then_that_venue_passes_the_category_gate(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile is not None
    assert profile["category_pass"] is True, profile
    assert profile["tier"] != TIER_EXCLUDED_CATEGORY, profile


@then("its category reason names the vibe signal that promoted it")
def step_then_its_category_reason_names_the_vibe_signal_that_promoted_it(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile["category_reason"] == "vibe:Banda ao vivo", profile


@then("the shipped default allow-list is used")
def step_then_the_shipped_default_allow_list_is_used(context):
    cfg, _reason = load_event_candidate_categories(context.event_redis)
    assert cfg.allowed_categories == frozenset(DEFAULT_ALLOWED_CATEGORIES), cfg


@then("the candidate set is not empty")
def step_then_the_candidate_set_is_not_empty(context):
    passing = [
        p for p in context.event_dao.list_venue_event_profiles()
        if p.get("category_pass")
    ]
    assert passing, "expected at least one category-passing venue"


@then("a config fallback is counted")
def step_then_a_config_fallback_is_counted(context):
    assert context.event_result is not None
    assert context.event_result.get("config_fallback") is not None, context.event_result


# ── then: bounded evidence-gate selection ────────────────────────────────────
@then("the {n:d} highest priority candidates are evidence-evaluated")
def step_then_the_n_d_highest_priority_candidates_are_evidence_evaluated(context, n):
    evaluated = [
        vid for vid in context.rds_store.venues
        if (p := context.event_dao.get_venue_event_profile(vid)) and p.get("evaluated_at")
    ]
    assert len(evaluated) == n, evaluated


@then('the remaining {n:d} keep the tier "{tier}"')
def step_then_the_remaining_n_d_keep_the_tier_tier(context, n, tier):
    remaining = [
        vid for vid in context.rds_store.venues
        if (p := context.event_dao.get_venue_event_profile(vid)) and p.get("evaluated_at") is None
    ]
    assert len(remaining) == n, remaining
    for vid in remaining:
        profile = context.event_dao.get_venue_event_profile(vid)
        assert profile["tier"] == tier, profile


@then("the remaining {n:d} have no evaluation timestamp")
def step_then_the_remaining_n_d_have_no_evaluation_timestamp(context, n):
    remaining = [
        vid for vid in context.rds_store.venues
        if (p := context.event_dao.get_venue_event_profile(vid)) and p.get("evaluated_at") is None
    ]
    assert len(remaining) == n, remaining


@then("that venue is evidence-evaluated")
def step_then_that_venue_is_evidence_evaluated(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile is not None
    assert profile.get("evaluated_at") is not None, profile


@then("it is not excluded for having no BestTime id")
def step_then_it_is_not_excluded_for_having_no_besttime_id(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile["tier"] != TIER_EXCLUDED_CATEGORY, profile


# ── then: evidence verdict + sample ───────────────────────────────────────────
@then('that venue is recorded with the tier "{tier}"')
def step_then_that_venue_is_recorded_with_the_tier_tier(context, tier):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile is not None
    assert profile["tier"] == tier, profile


@then("its evidence sample records the flyer count")
def step_then_its_evidence_sample_records_the_flyer_count(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    sample = profile.get("evidence_sample") or {}
    assert sample.get("flyer_count", 0) > 0, sample


@then("its evidence sample records what was examined")
def step_then_its_evidence_sample_records_what_was_examined(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    sample = profile.get("evidence_sample") or {}
    assert "flyer_count" in sample and "caption_matches" in sample, sample


# ── then: never-examined vs rejected ─────────────────────────────────────────
@then("the two venues are reported under different tiers")
def step_then_the_two_venues_are_reported_under_different_tiers(context):
    by_id = {p["venue_id"]: p for p in context.event_summary}
    cc_vid = context.event_tier_venues[TIER_CATEGORY_CANDIDATE][0]
    rej_vid = context.event_tier_venues[TIER_EVIDENCE_REJECTED][0]
    assert by_id[cc_vid]["tier"] != by_id[rej_vid]["tier"]


@then("only the rejected venue carries an evaluation timestamp")
def step_then_only_the_rejected_venue_carries_an_evaluation_timestamp(context):
    by_id = {p["venue_id"]: p for p in context.event_summary}
    cc_vid = context.event_tier_venues[TIER_CATEGORY_CANDIDATE][0]
    rej_vid = context.event_tier_venues[TIER_EVIDENCE_REJECTED][0]
    assert by_id[cc_vid]["evaluated_at"] is None, by_id[cc_vid]
    assert by_id[rej_vid]["evaluated_at"] is not None, by_id[rej_vid]


# ── then: zero-spend guardrails ────────────────────────────────────────────────
@then("no model call is made")
def step_then_no_model_call_is_made(context):
    # This service has no model-client dependency at all (no OpenAI wiring) —
    # its absence IS the guarantee; there is no call site to have counted.
    assert not hasattr(context.event_service, "openai_client")
    assert not hasattr(context.event_service, "model_client")


@then("no external API call is made")
def step_then_no_external_api_call_is_made(context):
    # None of the batch's venues have an Instagram handle, so the evidence
    # gate must short-circuit to `unevaluated` without ever touching the
    # injected flyer evidence source (its call count is the proof).
    assert context.event_flyer_source.calls == 0


# ── then: eligibility mode resolution ─────────────────────────────────────────
@then("exactly the {n:d} confirmed venues are selected")
def step_then_exactly_the_n_d_confirmed_venues_are_selected(context, n):
    from app.services.event_venue_targeting import TIER_EVIDENCE_CONFIRMED

    expected = set(context.event_tier_venues.get(TIER_EVIDENCE_CONFIRMED, []))
    assert len(expected) == n, expected
    assert set(context.event_selected_ids) == expected, context.event_selected_ids


@then("{n:d} venues are selected")
def step_then_n_d_venues_are_selected(context, n):
    assert len(context.event_selected_ids) == n, context.event_selected_ids


# ── then: recompute skip/re-evaluate ──────────────────────────────────────────
@then("that venue is not re-evaluated")
def step_then_that_venue_is_not_re_evaluated(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile["evaluated_at"] == context.event_seed_evaluated_at, profile


@then("that venue is re-evaluated")
def step_then_that_venue_is_re_evaluated(context):
    vid = context.event_last_venue_id
    profile = context.event_dao.get_venue_event_profile(vid)
    assert profile["evaluated_at"] != context.event_seed_evaluated_at, profile


# ── then: dry run ──────────────────────────────────────────────────────────────
@then("the tier changes it would make are reported")
def step_then_the_tier_changes_it_would_make_are_reported(context):
    assert context.event_result is not None
    assert context.event_result["dry_run"] is True
    assert context.event_result["tier_changes"], context.event_result


@then("no venue event profile is written")
def step_then_no_venue_event_profile_is_written(context):
    for vid in context.event_recent_batch:
        assert context.event_dao.get_venue_event_profile(vid) is None
