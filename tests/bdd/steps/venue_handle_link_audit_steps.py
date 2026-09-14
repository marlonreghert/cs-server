"""Behave steps for
tests/bdd/enrichment/venue-handle-link-audit.feature.

See plans/260913_venue-handle-link-audit.md. Self-contained per scenario
(own fresh `InMemoryRdsVenueStore` + fakeredis), mirroring
`tests/bdd/steps/events_venue_night_duplication_backlog_steps.py`'s own
pattern — this feature also reads a REPORT over rows seeded directly
through the DAO.

Every concrete `location_text`/venue-name value used below is a real,
re-verified value from this plan's own Evidence section (never an
invented string), so a scenario failure here is a failure against real
data shapes, not a synthetic fixture's own conveniences.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services.event_reconciliation import new_event_id
from app.services.event_venue_resolution import DEFAULT_CONFIDENCE_FLOOR
from app.services.instagram_cascade_service import name_similarity
from app.services.venue_link_audit import (
    ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY,
    collect_venue_link_audit,
    venue_corroborates_location_text,
)
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

# The single canonical mapped venue scenarios 1-10 (the per-comparison
# level) share — real, re-verified values (Theater Luís Mendonça, Boa
# Viagem), so "a spelling variant of its own name" and "combines the name
# with its own neighbourhood" are the REAL measured strings, not invented
# ones.
_SOLO_VENUE_NAME = "Theater Luís Mendonça"
_SOLO_VENUE_NEIGHBOURHOOD = "Boa Viagem"
_SOLO_HANDLE = "solo_handle"


def _ensure(context) -> None:
    if hasattr(context, "vla_dao"):
        return
    context.vla_dao = InMemoryRdsVenueStore()
    context.vla_redis = fakeredis.FakeRedis(decode_responses=True)
    context.vla_client = None
    context.vla_seq = 0
    context.vla_venues: dict = {}


def _make_venue(context, name: str, neighbourhood: str) -> str:
    _ensure(context)
    context.vla_seq += 1
    venue_id = f"vla_venue_{context.vla_seq}"
    context.vla_dao.upsert_venue(Venue(
        venue_id=venue_id, venue_name=name, venue_lat=-8.05, venue_lng=-34.88,
        venue_address=f"{neighbourhood}, Recife - PE",
    ))
    context.vla_dao.update_venue_address_components(
        venue_id, source="operator", neighborhood=neighbourhood,
    )
    context.vla_venues[name] = venue_id
    return venue_id


def _map_handle_to_venue(context, handle: str, venue_id: str) -> None:
    _ensure(context)
    context.vla_dao.enrichment.setdefault("instagram.handle", {})[venue_id] = {
        "instagram_handle": handle,
    }


def _register_crawl_target(context, handle: str, *, enabled: bool = True) -> None:
    _ensure(context)
    context.vla_dao.upsert_crawl_target(handle, {
        "kind": "venue", "enabled": enabled, "cron": "0 0 * * *",
    })


def _seed_event(context, handle: str, location_text: str, *, venue_id=None, starts_at=None) -> str:
    _ensure(context)
    context.vla_seq += 1
    seen = _NOW + timedelta(seconds=context.vla_seq)
    # Each seeded event gets its OWN day by default (never colliding on the
    # same venue-night) so this harness never incidentally creates a
    # venue-night duplicate as a side effect of seeding several events for
    # one venue — that is a DIFFERENT existing feature
    # (`event_dedup_backlog`'s own `venue_night_groups`) this harness must
    # not perturb unless a scenario explicitly asks for it.
    default_starts_at = _NOW + timedelta(days=context.vla_seq)
    fields = {
        "event_id": new_event_id(), "venue_id": venue_id,
        "starts_at": starts_at or default_starts_at, "title": "EVENT", "post_type": "event", "status": "accepted",
        "location_text": location_text, "lineup": [],
        "source_kind": "venue_post", "source_handle": handle,
        "source_shortcode": f"vla_sc_{context.vla_seq}",
        "first_seen_at": seen, "last_seen_at": seen,
    }
    context.vla_dao.insert_event(fields)
    return fields["event_id"]


def _client(context) -> TestClient:
    _ensure(context)
    if context.vla_client is None:
        app = FastAPI()
        app.include_router(admin_events_router)
        set_events_container(type("C", (), {
            "pipeline_repository": context.vla_dao,
            "redis_client": context.vla_redis,
        })())
        context.vla_client = TestClient(app)
    return context.vla_client


# ── Background ───────────────────────────────────────────────────────────
@given("the venue-handle link audit fixture corpus of re-verified real cases")
def step_given_fixture_corpus(context):
    """No-op besides establishing the harness: the concrete per-scenario
    Given steps below build their own data directly from the same
    re-verified real values this plan's Evidence section and
    `tests/fixtures/venue_link_audit/corpus.json` both carry. The
    measurement-script scenarios at the bottom of the feature read the
    committed fixture file itself, not this harness."""
    _ensure(context)


# ── the core comparison (scenarios 1-4) ──────────────────────────────────
@given("a kind='venue' crawl target mapped to one venue")
def step_given_target_mapped_to_one_venue(context):
    venue_id = _make_venue(context, _SOLO_VENUE_NAME, _SOLO_VENUE_NEIGHBOURHOOD)
    _map_handle_to_venue(context, _SOLO_HANDLE, venue_id)
    _register_crawl_target(context, _SOLO_HANDLE)
    context.vla_mapped_venue_name = _SOLO_VENUE_NAME
    context.vla_mapped_venue_neighbourhood = _SOLO_VENUE_NEIGHBOURHOOD


@given("one of its posts' own location text names a specific, different place by name")
def step_given_text_names_different_place(context):
    # Real, re-verified value: "Torre Malakoff" against the mapped venue
    # "Theater Luís Mendonça" — name_similarity 0.28, no neighbourhood hit.
    context.vla_location_text = "Torre Malakoff"


@given("one of its posts' own location text is a spelling variant of the mapped venue's own name")
def step_given_text_is_name_variant(context):
    # Real, re-verified value: "Teatro Luiz Mendonça" scores 0.8649 against
    # the catalog's "Theater Luís Mendonça".
    context.vla_location_text = "Teatro Luiz Mendonça"


@given("one of its posts' own location text combines the mapped venue's name with its own neighbourhood")
def step_given_text_combines_name_and_neighbourhood(context):
    # Real, re-verified value: combining the name with its own address
    # DROPS the raw name_similarity score to 0.4776 (below the 0.55
    # floor) — this text is only recovered by the neighbourhood check.
    context.vla_location_text = (
        "Teatro Luiz Mendonça — Av. Boa Viagem, S/N, Boa Viagem, Recife/PE"
    )


@given("one of its posts' own location text is empty")
def step_given_text_is_empty(context):
    context.vla_location_text = ""


@when("the venue-handle link audit comparison runs for that post against the mapped venue")
def step_when_comparison_runs(context):
    context.vla_verdict = venue_corroborates_location_text(
        context.vla_mapped_venue_name, context.vla_mapped_venue_neighbourhood,
        context.vla_location_text,
    )


@then("the post is reported as not corroborating the mapped venue")
def step_then_not_corroborating(context):
    assert context.vla_verdict is False, context.vla_verdict


@then("the post is reported as corroborating the mapped venue")
def step_then_corroborating(context):
    assert context.vla_verdict is True, context.vla_verdict


@then("the post is reported as corroborating the mapped venue, via the neighbourhood match")
def step_then_corroborating_via_neighbourhood(context):
    assert context.vla_verdict is True, context.vla_verdict
    # Proves the neighbourhood check is doing real work here, not riding
    # along on a coincidentally high name score.
    raw_name_score = name_similarity(
        context.vla_mapped_venue_name, context.vla_location_text, None,
    )
    assert raw_name_score < DEFAULT_CONFIDENCE_FLOOR, raw_name_score


@then("the post is excluded from both the corroborating and non-corroborating counts")
def step_then_excluded(context):
    assert context.vla_verdict is None, context.vla_verdict


# ── aggregation and the false-positive guard ────────────────────────────
_PATTERN_HANDLE = "pattern_handle"
_SINGLE_MENTION_HANDLE = "single_mention_handle"
_DOUBLE_HANDLE = "double_mapped_handle"
_ORPHAN_HANDLE = "orphan_handle"


@given("a kind='venue' crawl target whose posts give at least two non-corroborating, checkable pieces of location text for its mapped venue")
def step_given_pattern_non_corroborating(context):
    venue_id = _make_venue(context, "Casa da Cultura de Pernambuco", "São José")
    _map_handle_to_venue(context, _PATTERN_HANDLE, venue_id)
    _register_crawl_target(context, _PATTERN_HANDLE)
    # The two real, decisive, re-verified editaisculturape texts.
    for text in ("Torre Malakoff", "Casa de Câmara e Cadeia de Brejo da Madre de Deus"):
        _seed_event(context, _PATTERN_HANDLE, text, venue_id=venue_id)
    context.vla_target_handle = _PATTERN_HANDLE
    context.vla_target_venue_id = venue_id


@given("a kind='venue' crawl target whose posts consistently corroborate its mapped venue except for exactly one post naming an unrelated place in passing")
def step_given_single_incidental_mention(context):
    venue_id = _make_venue(context, _SOLO_VENUE_NAME, _SOLO_VENUE_NEIGHBOURHOOD)
    _map_handle_to_venue(context, _SINGLE_MENTION_HANDLE, venue_id)
    _register_crawl_target(context, _SINGLE_MENTION_HANDLE)
    for _ in range(3):
        _seed_event(context, _SINGLE_MENTION_HANDLE, "Teatro Luiz Mendonça", venue_id=venue_id)
    # Exactly ONE non-corroborating mention — below the pattern bar of 2.
    _seed_event(context, _SINGLE_MENTION_HANDLE, "Torre Malakoff", venue_id=venue_id)
    context.vla_target_handle = _SINGLE_MENTION_HANDLE
    context.vla_target_venue_id = venue_id


@given("a kind='venue' crawl target currently mapped to two venues, where every post's location text corroborates only one of them")
def step_given_double_mapped_handle(context):
    # The real, re-verified real.botequim shape.
    correct_id = _make_venue(context, "Bar Real Botequim", "Casa Forte")
    wrong_id = _make_venue(context, "Real Bar e Lanches", "Pinheiros")
    _map_handle_to_venue(context, _DOUBLE_HANDLE, correct_id)
    _map_handle_to_venue(context, _DOUBLE_HANDLE, wrong_id)
    _register_crawl_target(context, _DOUBLE_HANDLE)
    for _ in range(3):
        _seed_event(
            context, _DOUBLE_HANDLE,
            "Real Botequim\nAv. 17 de agosto, 1761 - Casa Forte",
            venue_id=correct_id,
        )
    context.vla_target_handle = _DOUBLE_HANDLE
    context.vla_correct_venue_id = correct_id
    context.vla_wrong_venue_id = wrong_id


@given("a kind='venue' crawl target with no venue currently mapped to its handle")
def step_given_orphaned_target(context):
    _register_crawl_target(context, _ORPHAN_HANDLE)
    context.vla_target_handle = _ORPHAN_HANDLE


@when("the venue-handle link audit aggregation runs for that target")
def step_when_aggregation_runs(context):
    context.vla_candidates = collect_venue_link_audit(context.vla_dao)


@then("the target's mapped venue is reported as a venue-link-audit candidate")
def step_then_venue_is_candidate(context):
    by_handle = {c.handle: c for c in context.vla_candidates}
    candidate = by_handle.get(context.vla_target_handle)
    assert candidate is not None, context.vla_candidates
    flagged_ids = {v.venue_id for v in candidate.mapped_venues if v.flagged}
    assert context.vla_target_venue_id in flagged_ids, candidate


@then("the target's mapped venue is not reported as a venue-link-audit candidate")
def step_then_venue_not_candidate(context):
    by_handle = {c.handle: c for c in context.vla_candidates}
    candidate = by_handle.get(context.vla_target_handle)
    if candidate is None:
        return
    flagged_ids = {v.venue_id for v in candidate.mapped_venues if v.flagged}
    assert context.vla_target_venue_id not in flagged_ids, candidate


@then("only the uncorroborated venue is reported as a venue-link-audit candidate")
def step_then_only_uncorroborated_flagged(context):
    by_handle = {c.handle: c for c in context.vla_candidates}
    candidate = by_handle[context.vla_target_handle]
    flagged_ids = {v.venue_id for v in candidate.mapped_venues if v.flagged}
    assert flagged_ids == {context.vla_wrong_venue_id}, candidate


@then("the corroborated venue is not reported")
def step_then_corroborated_not_reported(context):
    by_handle = {c.handle: c for c in context.vla_candidates}
    candidate = by_handle[context.vla_target_handle]
    flagged_ids = {v.venue_id for v in candidate.mapped_venues if v.flagged}
    assert context.vla_correct_venue_id not in flagged_ids, candidate
    # Still visible in the SAME record, never dropped — the plan's own
    # stated requirement for a double-mapped handle.
    present_ids = {v.venue_id for v in candidate.mapped_venues}
    assert context.vla_correct_venue_id in present_ids, candidate


@then("the target produces no venue-link-audit candidate and no error")
def step_then_orphan_no_candidate(context):
    handles = {c.handle for c in context.vla_candidates}
    assert _ORPHAN_HANDLE not in handles, context.vla_candidates


# ── the existing admin surface, gated off by default ────────────────────
@given("venue link auditing is disabled in admin config")
def step_given_audit_disabled(context):
    _ensure(context)
    context.vla_redis.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY, json.dumps(False))


@given("venue link auditing is enabled in admin config")
def step_given_audit_enabled(context):
    _ensure(context)
    context.vla_redis.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY, json.dumps(True))


@given("a kind='venue' crawl target that would otherwise be flagged")
@given("a kind='venue' crawl target that would be flagged")
def step_given_target_would_be_flagged(context):
    venue_id = _make_venue(context, "Casa da Cultura de Pernambuco", "São José")
    handle = "router_handle"
    _map_handle_to_venue(context, handle, venue_id)
    _register_crawl_target(context, handle)
    for text in ("Torre Malakoff", "Casa de Câmara e Cadeia de Brejo da Madre de Deus"):
        _seed_event(context, handle, text, venue_id=venue_id)
    context.vla_target_handle = handle
    context.vla_target_venue_id = venue_id


@when("an operator requests the dedup-backlog report")
def step_when_operator_requests_backlog(context):
    import sys

    # `sys.modules` lookup, never `import app.routers.admin_events_router
    # as router_module`: `app/routers/__init__.py` does `from
    # app.routers.admin_events_router import router as admin_events_router`,
    # which overwrites the `admin_events_router` ATTRIBUTE on the
    # `app.routers` package with the APIRouter instance — the classic
    # Python gotcha where a package's own `__init__.py` shadows its
    # submodule's attribute slot. `import ... as x` resolves via that
    # (shadowed) attribute, not via `sys.modules` directly, so it would
    # silently bind `x` to the router instance, not the module.
    router_module = sys.modules["app.routers.admin_events_router"]

    context.vla_audit_calls = 0
    original = router_module.collect_venue_link_audit

    def _spy(*args, **kwargs):
        context.vla_audit_calls += 1
        return original(*args, **kwargs)

    router_module.collect_venue_link_audit = _spy
    try:
        response = _client(context).get("/admin/events/dedup-backlog")
    finally:
        router_module.collect_venue_link_audit = original
    assert response.status_code == 200, response.text
    context.vla_response = response.json()


@then("the report's venue-link-audit fields are empty")
def step_then_fields_empty(context):
    body = context.vla_response
    assert body["venue_link_audit_candidates"] == [], body
    assert body["venue_link_audit_total"] == 0, body


@then("the venue-handle link audit aggregation is never invoked")
def step_then_aggregation_never_invoked(context):
    assert context.vla_audit_calls == 0, context.vla_audit_calls


@then("the report includes that target as a venue-link-audit candidate")
def step_then_report_includes_candidate(context):
    body = context.vla_response
    handles = {c["handle"] for c in body["venue_link_audit_candidates"]}
    assert context.vla_target_handle in handles, body


@then("every other field of the report is unchanged from today's shape")
def step_then_other_fields_unchanged(context):
    body = context.vla_response
    for key in (
        "live_rows", "venue_night_group_total", "venue_night_excess_rows",
        "refused_disjoint_pairs", "refused_no_distinctive_tokens_pairs",
        "pending_suggestions", "attribution_disputed_rows",
        "attribution_dispute_total", "limit", "offset",
        "venue_night_groups", "attribution_disputes",
    ):
        assert key in body, body
    assert body["venue_night_groups"] == [], body
    assert body["attribution_disputes"] == [], body


# ── read-only measurement ────────────────────────────────────────────────
@when("the venue-handle link audit measurement script runs against the fixture corpus")
def step_when_measurement_script_runs(context):
    from scripts.measure_venue_link_audit import measure_corpus

    context.vla_report = measure_corpus()


@then("it flags the re-verified wrong-venue case")
def step_then_flags_wrong_venue_case(context):
    result = context.vla_report["real_botequim_double_mapped_handle"]
    assert result["flagged"] is True, result
    assert result["matches_expectation"], result


@then("it flags the re-verified force-assigned-misattribution case")
def step_then_flags_misattribution_case(context):
    result = context.vla_report["editaisculturape_force_assigned_wrong_venue"]
    assert result["flagged"] is True, result
    assert result["matches_expectation"], result


@then("it does not flag the re-verified benign case")
def step_then_does_not_flag_benign_case(context):
    result = context.vla_report["teatroluizmendonca_benign_control"]
    assert result["flagged"] is False, result
    assert result["matches_expectation"], result


@then("no event, venue, or crawl-target row is written")
def step_then_nothing_written(context):
    # Structural guarantee, mirroring
    # `measure_agentic_mitigation_baseline.py`'s own guard: the corpus
    # replay never opens a write transaction anywhere in its code path.
    import inspect

    from scripts import measure_venue_link_audit

    source = inspect.getsource(measure_venue_link_audit)
    for forbidden in ("insert_event", "upsert_venue", "upsert_crawl_target", "update_event", ".begin("):
        assert forbidden not in source, f"{forbidden!r} must not appear in measure_venue_link_audit.py"
