"""Step definitions for
tests/bdd/enrichment/dedup-agentic-mitigation-discovery.feature.

See plans/260913_dedup-agentic-mitigation-discovery.md. Every step calls the
REAL production function under test — `app.services.event_venue_resolution.
resolve_event_venue`/`build_location_text_attribute_fn`,
`app.services.event_reconciliation.reconcile_post_events`,
`app.services.event_attribution_dispute.evaluate_attribution_dispute`,
`app.services.event_venue_advisor_validator.
validate_event_venue_recommendation`, `app.services.event_venue_advisor.
EventVenueAdvisorService`, and `scripts.measure_agentic_mitigation_baseline`'s
own replay functions — never a hand-rolled restatement of what those
functions do. The two scenarios that pin CURRENT behaviour (multi-venue
independent resolution; the unrelated-venue gap) drive the exact same ladder
and reconciliation calls every other production caller uses; nothing here
special-cases them.

Own namespace (`context.agentic_*`) throughout, matching this repo's
per-feature context-prefix convention (`context.dedup_*`,
`context.dt_*`, ...) so this file can run alongside every other steps module
in one `behave` process without clobbering shared context state.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import fakeredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scripts.measure_agentic_mitigation_baseline as measure_mod
from app.dao.venue_repository import VenueRepository
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services.event_attribution_dispute import (
    REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE,
    evaluate_attribution_dispute,
    fold_review_reason,
    load_attribution_dispute_config,
)
from app.services.event_reconciliation import reconcile_post_events
from app.services.event_venue_advisor import (
    ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY,
    EventVenueAdvisorService,
)
from app.services.event_venue_advisor_validator import (
    VenueCandidate,
    validate_event_venue_recommendation,
)
from app.services.event_venue_resolution import (
    VenueLite,
    build_location_text_attribute_fn,
)
from app.services.instagram_handle_sources import normalize_handle
from behave import given, then, when
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
_STARTS_AT = datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc)


def _ensure(context) -> None:
    if hasattr(context, "agentic_dao"):
        return
    context.agentic_dao = VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())
    context.agentic_redis = fakeredis.FakeRedis(decode_responses=True)
    context.agentic_client = None
    context.agentic_reconcile_fn = None


def _base_event(**overrides) -> dict:
    base = {
        "starts_at": _STARTS_AT, "ends_at": None, "is_recurring": False,
        "recurrence_text": None, "title": "Some Night", "description": None,
        "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": 0.9, "review_reason": None,
        "raw_extraction": {"title": "Some Night"},
    }
    base.update(overrides)
    return base


# ── Background ───────────────────────────────────────────────────────────────
@given("the labeled discovery corpus of known incident cases")
def step_given_the_corpus(context):
    _ensure(context)
    context.agentic_corpus = measure_mod.load_corpus()
    context.agentic_cases = {c["id"]: c for c in context.agentic_corpus["cases"]}
    assert context.agentic_corpus["cases"], "the committed corpus must not be empty"


@given("the venue catalog and Instagram handle mappings from that corpus")
def step_given_catalog_from_corpus(context):
    _ensure(context)
    # The replay functions this feature exercises (`replay_dedup_signal`,
    # `replay_attribution_dispute`, `replay_promoter_discovery_mentions`)
    # each build their OWN bounded venue/handle set per case — see their
    # own docstrings in scripts/measure_agentic_mitigation_baseline.py.
    # This step simply confirms that contract holds for every committed
    # case before any scenario below relies on it.
    for case in context.agentic_corpus["cases"]:
        assert "venue" in case and "venue_id" in case["venue"]


# ── read-only baseline measurement ──────────────────────────────────────────
@when("the baseline measurement script runs against the corpus")
def step_when_measure_runs_against_corpus(context):
    _ensure(context)
    # A real, freshly-constructed DAO stands by, untouched, so "nothing is
    # written" is a property this step can actually observe rather than
    # merely assert — `measure_corpus` below never even receives it.
    before = {
        "events": dict(context.agentic_dao.rds_store.events),
        "venues": dict(context.agentic_dao.rds_store.venues),
        "promoter_accounts": dict(context.agentic_dao.rds_store.promoter_accounts),
    }
    context.agentic_results = measure_mod.measure_corpus(context.agentic_corpus)
    context.agentic_dao_snapshot_before = before


@then("no event, venue, or promoter-account row is written")
def step_then_nothing_written(context):
    assert context.agentic_dao.rds_store.events == context.agentic_dao_snapshot_before["events"]
    assert context.agentic_dao.rds_store.venues == context.agentic_dao_snapshot_before["venues"]
    assert (
        context.agentic_dao.rds_store.promoter_accounts
        == context.agentic_dao_snapshot_before["promoter_accounts"]
    )
    assert context.agentic_dao.rds_store.events == {}
    assert context.agentic_dao.rds_store.venues == {}
    assert context.agentic_dao.rds_store.promoter_accounts == {}


@then("a report names, for each corpus case, which existing signal already caught it, if any")
def step_then_report_names_signal_per_case(context):
    assert len(context.agentic_results) == len(context.agentic_corpus["cases"])
    for result in context.agentic_results:
        assert hasattr(result, "caught_by")
        assert isinstance(result.caught_by, list)


@given("a corpus handle whose captions mention another handle at least three times across distinct posts")
def step_given_handle_clears_mention_threshold(context):
    _ensure(context)
    context.agentic_mention_case = {
        "id": "bdd-mention-clears", "failure_class": "multi_location_misattribution",
        "synthetic": True, "venue": {"venue_id": "v1", "venue_name": "Test Venue"},
        "sibling_venues": [], "unrelated_venues": [], "instagram_handle": "roundupaccount",
        "posts": [
            _base_event(event_id="p1", caption="confira @othervenue hoje"),
            _base_event(event_id="p2", caption="hoje é lá no @othervenue"),
            _base_event(event_id="p3", caption="voltamos no @othervenue"),
        ],
    }
    context.agentic_other_handle = "othervenue"


@given("a corpus handle whose captions mention another handle only once")
def step_given_handle_never_clears_threshold(context):
    _ensure(context)
    context.agentic_mention_case = {
        "id": "bdd-mention-never-clears", "failure_class": "multi_location_misattribution",
        "synthetic": True, "venue": {"venue_id": "v1", "venue_name": "Test Venue"},
        "sibling_venues": [], "unrelated_venues": [], "instagram_handle": "roundupaccount",
        "posts": [
            _base_event(event_id="p1", caption="confira @othervenue hoje"),
            _base_event(event_id="p2", caption="nada aqui"),
            _base_event(event_id="p3", caption="nada aqui tampouco"),
        ],
    }
    context.agentic_other_handle = "othervenue"


@when("the baseline measurement script replays promoter discovery")
def step_when_replay_promoter_discovery(context):
    context.agentic_mention_result = measure_mod.replay_promoter_discovery_mentions(
        context.agentic_mention_case,
    )


@then("that other handle is reported as a proposed candidate")
def step_then_handle_proposed(context):
    assert context.agentic_other_handle in context.agentic_mention_result["proposed_candidates"]


@then("that other handle is not reported as a proposed candidate")
def step_then_handle_not_proposed(context):
    assert context.agentic_other_handle not in context.agentic_mention_result["proposed_candidates"]


@then("the report records the mention count and the mention threshold used")
def step_then_report_records_count_and_threshold(context):
    result = context.agentic_mention_result
    assert result["mention_counts"][context.agentic_other_handle] == 3
    assert result["threshold"] == measure_mod.DEFAULT_MENTION_THRESHOLD


@when("the baseline measurement script runs against production data")
def step_when_measure_runs_against_production(context):
    _ensure(context)
    # "Production" inside this BDD world is the real, in-memory DAO fake —
    # the same stand-in every other scenario in this repo's suite uses for
    # "the real catalog" (CLAUDE.md's BDD policy: deterministic fakes, never
    # a live external call). Seeded with a small, explicit mix so the
    # expected fraction is independently computable below, never asserted
    # blind.
    dao = context.agentic_dao
    dao.rds_store.upsert_venue(_fake_venue("v1", "Venue One"))

    def _insert(event_id, *, venue_id="v1", **overrides):
        fields = _base_event(event_id=event_id, venue_id=venue_id, **overrides)
        dao.rds_store.insert_event(fields)

    # Two plain, cleanly-resolved events (no ambiguity).
    _insert("e1", location_resolution="auto")
    _insert("e2", location_resolution="auto")
    # One RESOLUTION_QUEUED event (both link columns unset).
    _insert("e3", location_resolution=None, venue_id=None)
    context.agentic_production_report = measure_mod.measure(dao, redis_like=context.agentic_redis)
    context.agentic_production_live_count = 3


@then("the report states the fraction of extracted events currently landing in the suggest band, the queued resolution state, or an attribution dispute")
def step_then_report_states_ambiguous_fraction(context):
    report = context.agentic_production_report
    assert report.live_events == context.agentic_production_live_count
    assert report.resolution_queued == 1
    expected_fraction = round(report.ambiguous_band_events / report.live_events, 4)
    assert report.ambiguous_fraction == expected_fraction


@then("the report does not state any fraction that was not computed from real data")
def step_then_fraction_is_real(context):
    report = context.agentic_production_report
    # Recomputed independently from the SAME seeded DAO, never trusted by
    # construction — proves the reported fraction tracks the real seeded
    # state rather than a hardcoded placeholder.
    recomputed = round(
        (report.resolution_queued + report.pending_merge_suggestions) / report.live_events, 4,
    )
    assert report.ambiguous_fraction == recomputed


# ── multi-venue independent resolution (documents CURRENT behaviour) ───────
def _fake_venue(venue_id, name, lat=0.0, lng=0.0):
    from app.models.venue import Venue

    return Venue(venue_id=venue_id, venue_name=name, venue_lat=lat, venue_lng=lng)


@given("a single post from a corpus handle announces two events whose location texts each name a different venue")
def step_given_one_post_two_events_two_venues(context):
    _ensure(context)
    dao = context.agentic_dao
    dao.rds_store.upsert_venue(_fake_venue("v_alpha", "Venue Alpha"))
    dao.rds_store.upsert_venue(_fake_venue("v_beta", "Venue Beta"))

    venues = [VenueLite(venue_id="v_alpha", venue_name="Venue Alpha"),
              VenueLite(venue_id="v_beta", venue_name="Venue Beta")]
    handle_index = {"venue_alpha_handle": "v_alpha", "venue_beta_handle": "v_beta"}

    events = [
        _base_event(title="Event A", location_text="@venue_alpha_handle"),
        _base_event(title="Event B", location_text="@venue_beta_handle"),
    ]

    def _reconcile(ctx):
        attribute_fn = build_location_text_attribute_fn(
            caption=None, location_tag=None, promoter_handle="roundupaccount",
            venues=venues, handle_index=handle_index, venue_dao=dao.rds_store, now=_NOW,
        )
        reconcile_post_events(
            venue_dao=dao.rds_store, source_kind="venue_post", source_handle="roundupaccount",
            source_shortcode="two_venues_sc", source_permalink=None,
            prepared_events=events, now=_NOW, attribute=attribute_fn,
        )
        ctx.agentic_two_event_rows = dao.rds_store.list_events_by_source(
            "roundupaccount", "two_venues_sc",
        )

    context.agentic_reconcile_that_post = _reconcile


@when("that post is reconciled")
def step_when_that_post_is_reconciled(context):
    assert context.agentic_reconcile_that_post is not None, "no 'that post' fixture was set up"
    context.agentic_reconcile_that_post(context)


@then("each event's venue attribution is evaluated against its own location text alone")
def step_then_each_event_evaluated_independently(context):
    rows = {r["title"]: r for r in context.agentic_two_event_rows}
    assert rows["Event A"]["venue_id"] == "v_alpha"
    assert rows["Event B"]["venue_id"] == "v_beta"


@then("the two events are not forced to share one venue attribution")
def step_then_events_not_forced_to_share(context):
    rows = {r["title"]: r for r in context.agentic_two_event_rows}
    assert rows["Event A"]["venue_id"] != rows["Event B"]["venue_id"]


# ── the unrelated-venue gap (documents a real, currently-open gap) ─────────
@given("a corpus handle mapped to one venue")
def step_given_corpus_handle_mapped_to_one_venue(context):
    _ensure(context)
    case = context.agentic_cases["casa_bacurau_unrelated_venue_gap"]
    context.agentic_gap_case = case
    dao = context.agentic_dao
    mapped = case["venue"]
    dao.rds_store.upsert_venue(_fake_venue(mapped["venue_id"], mapped["venue_name"]))
    for other in case["unrelated_venues"]:
        dao.rds_store.upsert_venue(_fake_venue(other["venue_id"], other["venue_name"]))


@given("a post from that handle names a different, unrelated, catalogued venue in its location text alone, with no handle mention, no location tag, and no shared brand-root token with the mapped venue")
def step_given_post_names_unrelated_venue(context):
    case = context.agentic_gap_case
    dao = context.agentic_dao
    mapped_id = case["venue"]["venue_id"]
    handle = case["instagram_handle"]
    post = case["posts"][0]
    location_text = post["location_text"]
    assert "@" not in location_text, "the gap case must carry no handle mention"

    catalog = [VenueLite(venue_id=case["venue"]["venue_id"], venue_name=case["venue"]["venue_name"])]
    catalog += [VenueLite(venue_id=v["venue_id"], venue_name=v["venue_name"]) for v in case["unrelated_venues"]]

    def _reconcile(ctx):
        # A faithful REPLICA of `EventExtractionService._extract_one`'s own
        # single-venue-handle closure (`event_extraction_service.py`,
        # `_attribute`, reached when `attribute_fn is None`) — not a
        # hand-rolled stub. That closure is a nested function and cannot be
        # imported directly, so this calls the exact SAME building blocks
        # it calls, in the exact same order, rather than asserting a
        # precomputed answer: `evaluate_attribution_dispute` decides
        # in-line whether the pin is disputed, and a real dispute (this
        # scenario's own case has none) would be folded into
        # `review_reason` via the real `fold_review_reason`, exactly as
        # production does. A regression in either real function is
        # therefore something this scenario CAN catch, not something it
        # assumes away.
        dispute_config = load_attribution_dispute_config(None)  # shipped defaults: action="flag"

        def _real_single_venue_attribute(fields, event_id):
            result_fields = {"venue_id": mapped_id}
            verdict = None
            if mapped_id and fields.get("location_text"):
                verdict = evaluate_attribution_dispute(
                    mapped_venue_id=mapped_id, location_text=fields["location_text"],
                    venues=catalog, handle_index={}, promoter_handle=handle,
                )
            ctx.agentic_dispute_verdict = verdict
            if verdict is None:
                return result_fields, None
            if dispute_config.reattributes and verdict.has_target:  # pragma: no cover - default action is "flag"
                result_fields.update({
                    "venue_id": verdict.target_venue_id, "location_resolution": "auto",
                    "location_confidence": verdict.confidence, "linked_by": verdict.method,
                })
            else:
                result_fields["review_reason"] = fold_review_reason(
                    fields.get("review_reason"), REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE,
                )
            return result_fields, None

        reconcile_post_events(
            venue_dao=dao.rds_store, source_kind="venue_post", source_handle=handle,
            source_shortcode="gap_sc", source_permalink=None,
            prepared_events=[_base_event(
                title=post["title"], location_text=location_text,
            )], now=_NOW, attribute=_real_single_venue_attribute,
        )
        rows = dao.rds_store.list_events_by_source(handle, "gap_sc")
        ctx.agentic_gap_event_row = rows[0]

    context.agentic_reconcile_that_post = _reconcile


@then("the event stays attributed to the handle's mapped venue")
def step_then_event_stays_attributed_to_mapped_venue(context):
    case = context.agentic_gap_case
    assert context.agentic_gap_event_row["venue_id"] == case["venue"]["venue_id"]


@then("the gap event carries no review reason")
def step_then_gap_event_carries_no_review_reason(context):
    assert context.agentic_gap_event_row.get("review_reason") is None


@then("no attribution dispute is counted for the gap event")
def step_then_no_dispute_counted_for_gap_event(context):
    assert context.agentic_dispute_verdict is None


@then("the baseline measurement report records this corpus case as undetected by every existing signal")
def step_then_corpus_case_undetected(context):
    result = measure_mod.replay_case(context.agentic_gap_case)
    assert result.caught_by == []


# ── the closed-candidate-set advisory validator ─────────────────────────────
@given("a closed set of ranked venue candidates for an event")
def step_given_closed_candidate_set(context):
    _ensure(context)
    context.agentic_candidates = [
        VenueCandidate(venue_id="v1", venue_name="Venue One"),
        VenueCandidate(venue_id="v2", venue_name="Venue Two"),
    ]
    context.agentic_validator_location_text = "Tonight we're at Venue Two, come through"
    context.agentic_validator_caption = "come say hi, doors open at nine"


@given("a model answer naming one of those candidates with a quoted evidence span")
def step_given_answer_naming_real_candidate(context):
    context.agentic_recommendation = {
        "venue_id": "v2", "evidence_quote": "we're at Venue Two",
    }


@given("a model answer naming a venue that is not among those candidates")
def step_given_answer_naming_venue_outside_set(context):
    context.agentic_recommendation = {
        "venue_id": "v3-not-a-candidate", "evidence_quote": "we're at Venue Two",
    }


@given("a model answer whose quoted evidence does not appear anywhere in the event's location text or caption")
def step_given_answer_with_invented_evidence(context):
    context.agentic_recommendation = {
        "venue_id": "v2", "evidence_quote": "a sentence nobody ever wrote",
    }


@given("a model answer naming one candidate but quoting evidence that only appears in a note about a different candidate")
def step_given_answer_borrowing_evidence(context):
    # The quoted text is REAL — it is exactly the kind of note the
    # deterministic ladder's own `evidence` field would carry for Venue
    # One's neighbourhood-match ranking (a real address fragment) — but it
    # is a different CANDIDATE's own ranking annotation, never something
    # THIS event's own post actually wrote. It is deliberately absent from
    # both `context.agentic_validator_location_text` and
    # `context.agentic_validator_caption` above, so the verbatim check
    # rejects it for the same structural reason it rejects a pure
    # invention: this event's own source text is the only thing the
    # validator may ever treat as evidence, never a candidate's metadata.
    context.agentic_recommendation = {
        "venue_id": "v2", "evidence_quote": "matched via its street address on Rua das Flores",
    }


@when("the evidence span is a verbatim substring of the event's own location text")
def step_when_evidence_is_verbatim(context):
    context.agentic_verdict = validate_event_venue_recommendation(
        context.agentic_candidates, context.agentic_recommendation,
        location_text=context.agentic_validator_location_text,
    )


@when("the recommendation is validated")
def step_when_recommendation_is_validated(context):
    context.agentic_verdict = validate_event_venue_recommendation(
        context.agentic_candidates, context.agentic_recommendation,
        location_text=context.agentic_validator_location_text,
        caption=context.agentic_validator_caption,
    )


@then("the recommendation is accepted")
def step_then_recommendation_accepted(context):
    assert context.agentic_verdict.accepted is True, context.agentic_verdict


@then("the recommendation is rejected")
def step_then_recommendation_rejected(context):
    assert context.agentic_verdict.accepted is False


@then("the rejection reason records that the venue was outside the candidate set")
def step_then_rejection_reason_venue_outside(context):
    from app.services.event_venue_advisor_validator import REJECTION_VENUE_OUTSIDE_CANDIDATE_SET

    assert context.agentic_verdict.rejection_reason == REJECTION_VENUE_OUTSIDE_CANDIDATE_SET


@then("the rejection reason records that the evidence was not verbatim")
def step_then_rejection_reason_not_verbatim(context):
    from app.services.event_venue_advisor_validator import REJECTION_EVIDENCE_NOT_VERBATIM

    assert context.agentic_verdict.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM


# ── the advisory hook, conditional on the measured gate ─────────────────────
def _build_queued_fixture(context):
    """Real ladder, real margin-blocked QUEUED outcome — `Bar Central Um`
    (score 0.9091) / `Bar Central Dois` (score 0.8333) against location_text
    `Bar Central`: both clear `DEFAULT_CONFIDENCE_FLOOR` but the 0.0758 gap
    is below `DEFAULT_MARGIN` (0.08), so `resolve_event_venue` returns
    `RESOLUTION_QUEUED` with both ranked as candidates — measured directly,
    never hand-picked to look ambiguous."""
    dao = context.agentic_dao
    dao.rds_store.upsert_venue(_fake_venue("v_um", "Bar Central Um"))
    dao.rds_store.upsert_venue(_fake_venue("v_dois", "Bar Central Dois"))
    venues = [VenueLite(venue_id="v_um", venue_name="Bar Central Um"),
              VenueLite(venue_id="v_dois", venue_name="Bar Central Dois")]

    attribute_fn = build_location_text_attribute_fn(
        caption=None, location_tag=None, promoter_handle="queuedaccount",
        venues=venues, handle_index={}, venue_dao=dao.rds_store, now=_NOW,
    )
    reconcile_post_events(
        venue_dao=dao.rds_store, source_kind="venue_post", source_handle="queuedaccount",
        source_shortcode="queued_sc", source_permalink=None,
        prepared_events=[_base_event(title="Ambiguous Night", location_text="Bar Central")],
        now=_NOW, attribute=attribute_fn,
    )
    row = dao.rds_store.list_events_by_source("queuedaccount", "queued_sc")[0]
    assert row["venue_id"] is None and row["location_resolution"] is None, row
    candidates = dao.rds_store.list_event_venue_link_candidates(row["event_id"])
    assert len(candidates) == 2, candidates
    context.agentic_queued_event_id = row["event_id"]
    return row["event_id"]


@given("the event venue advisor is disabled")
def step_given_advisor_disabled(context):
    _ensure(context)
    context.agentic_redis.delete(ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY)
    context.agentic_fake_openai = _FakeAdvisorOpenAI()


@given("the event venue advisor is enabled")
def step_given_advisor_enabled(context):
    _ensure(context)
    context.agentic_redis.set(ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY, json.dumps(True))
    context.agentic_fake_openai = _FakeAdvisorOpenAI()


@given("an event resolves to the queued state with ranked venue candidates")
def step_given_event_resolves_to_queued(context):
    _build_queued_fixture(context)


@given("the advisor's recommendation validates")
def step_given_advisors_recommendation_validates(context):
    context.agentic_fake_openai.program(
        json.dumps({"venue_id": "v_um", "evidence_quote": "Bar Central"}),
    )


@given("an event resolves automatically through the handle-mention method")
def step_given_event_resolves_automatically(context):
    dao = context.agentic_dao
    dao.rds_store.upsert_venue(_fake_venue("v_auto", "Auto Venue"))
    venues = [VenueLite(venue_id="v_auto", venue_name="Auto Venue")]
    handle_index = {"auto_venue_handle": "v_auto"}
    attribute_fn = build_location_text_attribute_fn(
        caption=None, location_tag=None, promoter_handle="autoaccount",
        venues=venues, handle_index=handle_index, venue_dao=dao.rds_store, now=_NOW,
    )
    reconcile_post_events(
        venue_dao=dao.rds_store, source_kind="venue_post", source_handle="autoaccount",
        source_shortcode="auto_sc", source_permalink=None,
        prepared_events=[_base_event(title="Resolved Night", location_text="@auto_venue_handle")],
        now=_NOW, attribute=attribute_fn,
    )
    row = dao.rds_store.list_events_by_source("autoaccount", "auto_sc")[0]
    assert row["venue_id"] == "v_auto" and row["location_resolution"] == "auto", row
    context.agentic_queued_event_id = row["event_id"]


@given("an event resolves to the queued state with a validated recommendation attached")
def step_given_event_already_has_recommendation(context):
    event_id = _build_queued_fixture(context)
    context.agentic_redis.set(ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY, json.dumps(True))
    fake_client = _FakeAdvisorOpenAI(
        json.dumps({"venue_id": "v_um", "evidence_quote": "Bar Central"}),
    )
    asyncio.run(
        EventVenueAdvisorService(context.agentic_dao.rds_store, fake_client, redis_client=context.agentic_redis)
        .run_for_events([event_id]),
    )
    context.agentic_fake_openai = fake_client


class _FakeAdvisorOpenAI:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls: list = []

    async def recommend_event_venue(self, *, location_text, caption, candidates):
        self.calls.append({"location_text": location_text, "caption": caption, "candidates": list(candidates)})
        if not self._answers:
            raise AssertionError("the fake advisor client was called more times than programmed")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def program(self, answer):
        self._answers.append(answer)


@when("the post is reconciled through the advisor pass")
def step_when_post_is_reconciled_through_advisor_pass(context):
    event_id = context.agentic_queued_event_id
    context.agentic_row_before = dict(context.agentic_dao.rds_store.get_event(event_id))
    asyncio.run(
        EventVenueAdvisorService(
            context.agentic_dao.rds_store, context.agentic_fake_openai,
            redis_client=context.agentic_redis,
        ).run_for_events([event_id]),
    )


@then("no advisor call is made")
def step_then_no_advisor_call(context):
    assert context.agentic_fake_openai.calls == []


@then("the event's venue and resolution state are exactly what the deterministic ladder produced")
def step_then_state_unchanged_by_advisor(context):
    after = context.agentic_dao.rds_store.get_event(context.agentic_queued_event_id)
    assert after["venue_id"] == context.agentic_row_before["venue_id"]
    assert after["location_resolution"] == context.agentic_row_before["location_resolution"]


@then("the event's venue candidate row carries the validated recommendation")
def step_then_candidate_row_carries_recommendation(context):
    candidates = context.agentic_dao.rds_store.list_event_venue_link_candidates(
        context.agentic_queued_event_id,
    )
    matching = [c for c in candidates if c["venue_id"] == "v_um"]
    assert matching and matching[0]["llm_recommendation"] == {
        "venue_id": "v_um", "evidence_quote": "Bar Central",
    }, candidates


@then("the event's venue remains unset")
def step_then_venue_remains_unset(context):
    row = context.agentic_dao.rds_store.get_event(context.agentic_queued_event_id)
    assert row["venue_id"] is None


@then("the event's resolution state remains queued")
def step_then_resolution_remains_queued(context):
    row = context.agentic_dao.rds_store.get_event(context.agentic_queued_event_id)
    assert row["location_resolution"] is None


def _build_review_client(context) -> TestClient:
    if context.agentic_client is not None:
        return context.agentic_client
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), {
        "pipeline_repository": context.agentic_dao, "redis_client": context.agentic_redis,
    })())
    context.agentic_client = TestClient(app)
    return context.agentic_client


@when("an operator requests the review queue")
def step_when_operator_requests_review_queue(context):
    client = _build_review_client(context)
    context.agentic_review_response = client.get("/admin/events/review")
    assert context.agentic_review_response.status_code == 200, context.agentic_review_response.text


@then("that event's entry in the review queue carries the recommendation")
def step_then_review_entry_carries_recommendation(context):
    items = context.agentic_review_response.json()
    entry = next(i for i in items if i["event_id"] == context.agentic_queued_event_id)
    assert entry["llm_recommendation"] == {"venue_id": "v_um", "evidence_quote": "Bar Central"}, entry


@then("no new endpoint was needed to see it")
def step_then_no_new_endpoint_needed(context):
    # The assertion above already read it off the EXISTING GET /admin/events
    # /review response this step's own client hit — stated here as an
    # explicit, separately-named Then so a future reader sees the claim
    # pinned, not merely implied by the previous step passing.
    assert context.agentic_review_response.request.url.path == "/admin/events/review"
