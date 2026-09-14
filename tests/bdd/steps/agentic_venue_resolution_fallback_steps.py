"""Behave steps for
tests/bdd/enrichment/agentic-venue-resolution-fallback.feature.

See plans/260914_agentic-venue-resolution-fallback.md. Self-contained per
scenario (own fresh `InMemoryRdsVenueStore` + fakeredis), mirroring
`tests/bdd/steps/venue_handle_link_audit_steps.py` — this feature layers a
second pass on top of that one's report and seeds its rows the same way,
through the DAO.

## The input is the REAL audit, never a hand-built pair list

Every scenario seeds a `kind='venue'` crawl target, an `instagram.handle`
enrichment row and real events, then lets `collect_venue_link_audit` decide
what is flagged. That is the whole point of the pass (plan §B): a
single-mapped `kind='venue'` target force-assigns its events and never runs
the resolution ladder, so 8 of the 9 originally-named handles have ZERO
`event_venue_link_candidate` rows and a pass sourced from that table would do
literally nothing for them. A harness that handed the reviewer a synthetic
pair list would silently stop exercising that.

## Every concrete value is real

Venue names, streets, neighbourhoods, cities and `location_text` values all
come from `tests/fixtures/venue_link_audit_reviewer/corpus.json`, whose
MANIFEST sources each one to a read-only production read on 2026-09-14. A
failure here is a failure against real data shapes, not a synthetic fixture's
own conveniences.

## The model is a stub, always

`_ProgrammableReviewClient` stands in for
`app.api.venue_link_review_client.VenueLinkReviewClient` and returns canned
RAW reply strings. BDD never reaches the network (this repo's CLAUDE.md
forbids it outright), and the stub counts its calls so the "no model call is
charged" scenarios can assert exactly zero — the gate that keeps a structural
exclusion structural rather than merely advisory.

## Two steps are reworded against the shared namespace

`tests/bdd/steps/` is ONE namespace across ~129 files. Two of this feature's
own phrasings already exist there, so they are reworded here AND in the
.feature file (this repo's rule, commit 483ecb5 — reword the step, never
dispatch-patch a merged file):

- "the run does not fail"  -> "the reviewer run does not fail"
  (taken by `extract_by_handle_steps.py`)
- "no model call is made"  -> "no model call is charged for any pair"
  (taken by `event_venue_targeting_steps.py`)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services.event_reconciliation import STATUS_CONFIRMED, new_event_id
from app.services.event_venue_resolution import RESOLUTION_MANUAL, _fold_for_containment
from app.services.venue_link_audit import (
    ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY,
    venue_corroborates_location_text,
)
from app.services.venue_link_audit_reviewer import (
    ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY,
    ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY,
    DEFAULT_REVIEW_CONSENSUS_K,
    DECISION_REJECTED,
    OUTCOME_CONFIRMED,
    OUTCOME_CONTRADICTED,
    OUTCOME_EMPTY_RESPONSE,
    OUTCOME_INSUFFICIENT,
    OUTCOME_NO_CONSENSUS,
    OUTCOME_SKIPPED_ALL_EVENTS_PROTECTED,
    OUTCOME_SKIPPED_MULTI_MAPPED,
    OUTCOME_SKIPPED_OPERATOR_DECIDED,
    OUTCOME_VALIDATOR_REJECTED,
    VenueLinkAuditReviewerService,
)
from app.services.venue_link_audit_reviewer_validator import (
    FIELD_NEIGHBORHOOD,
    FIELD_NONE,
    FIELD_STREET,
    REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE,
    REJECTION_EVIDENCE_NOT_VERBATIM,
    REJECTION_VERDICT_NOT_RECOGNISED,
    VERDICT_CONFIRM,
    ReviewAnswer,
    validate_venue_link_review,
)
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

_CORPUS_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "venue_link_audit_reviewer" / "corpus.json"
)

# The canonical single-mapped flagged pair. Used by every scenario whose own
# Given does not name a shape — including "the reviewer flag is disabled",
# which would otherwise assert "the audit's flagged output is unchanged"
# against an EMPTY audit and pass for the wrong reason.
_DEFAULT_CASE_ID = "conchittasbar_street_number_confirms"

# Every outcome the pass can record — the closed set "reaches a recorded
# outcome" is checked against, so a pair that silently acquired some new,
# unenumerated outcome would fail rather than pass.
_ALL_OUTCOMES = (
    OUTCOME_CONFIRMED, OUTCOME_CONTRADICTED, OUTCOME_INSUFFICIENT,
    OUTCOME_NO_CONSENSUS, OUTCOME_SKIPPED_MULTI_MAPPED,
    OUTCOME_SKIPPED_OPERATOR_DECIDED, OUTCOME_SKIPPED_ALL_EVENTS_PROTECTED,
    OUTCOME_VALIDATOR_REJECTED, OUTCOME_EMPTY_RESPONSE,
)

_corpus_cache: dict | None = None


def _corpus() -> dict:
    global _corpus_cache
    if _corpus_cache is None:
        _corpus_cache = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    return _corpus_cache


def _case(case_id: str) -> dict:
    for case in _corpus()["cases"]:
        if case["case_id"] == case_id:
            return case
    raise AssertionError(f"no corpus case {case_id!r} in {_CORPUS_PATH}")


# ── the model stub (never a real OpenAI call) ────────────────────────────


class _ProgrammableReviewClient:
    """Stands in for `VenueLinkReviewClient`, same duck type: an
    `async review_venue_link(*, evidence) -> str` returning the RAW reply
    text, plus the `model` attribute `_persist` records on the review row.

    Replies are queued PER HANDLE (a scenario can seed several pairs at
    once and program a different answer for each); a queue shorter than K
    repeats its last entry, so "ten identical confirms" is one entry and "one
    dissenter" is an explicit list. A queued `BaseException` is raised, which
    is how the error path is exercised without patching anything."""

    def __init__(self) -> None:
        self.model = "bdd-fake-review-model"
        self.calls = 0
        self.evidence_seen: list = []
        self.queues: dict[str, list] = {}
        self.default_queue: list = []
        self._counts: dict[str, int] = {}

    def program(self, responses, *, handle=None) -> None:
        if handle is None:
            self.default_queue = list(responses)
        else:
            self.queues[handle] = list(responses)

    def queue_for(self, handle: str) -> list:
        return self.queues.get(handle, self.default_queue)

    def evidence_for(self, handle: str, venue_id: str):
        for evidence in self.evidence_seen:
            if evidence.handle == handle and evidence.venue_id == venue_id:
                return evidence
        return None

    async def review_venue_link(self, *, evidence) -> str:
        handle = evidence.handle
        queue = self.queues.get(handle) or self.default_queue
        assert queue, f"BDD harness: no canned reply programmed for handle {handle!r}"
        n = self._counts.get(handle, 0)
        self._counts[handle] = n + 1
        self.calls += 1
        self.evidence_seen.append(evidence)
        item = queue[n] if n < len(queue) else queue[-1]
        if isinstance(item, BaseException):
            raise item
        return item


def _raw(verdict, *, quote=None, field=None, reason="bdd canned reason") -> str:
    return json.dumps(
        {
            "verdict": verdict, "evidence_quote": quote,
            "matched_field": field, "reason": reason,
        },
        ensure_ascii=False,
    )


def _raw_from_case(case: dict, key: str = "canned_model_answer") -> str:
    return json.dumps(case[key], ensure_ascii=False)


# ── harness ──────────────────────────────────────────────────────────────


def _ensure(context) -> None:
    if hasattr(context, "vlr_dao"):
        return
    context.vlr_dao = InMemoryRdsVenueStore()
    context.vlr_redis = fakeredis.FakeRedis(decode_responses=True)
    # `venue_link_audit_enabled` is this feature's PRECONDITION, not one of
    # its own two flags: it is live True in production (plan §A) and the
    # reviewer has nothing to review while the audit itself is off.
    context.vlr_redis.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY, json.dumps(True))
    context.vlr_stub = _ProgrammableReviewClient()
    context.vlr_http = None
    context.vlr_seq = 0
    context.vlr_pairs = []
    context.vlr_result = None
    context.vlr_error = None
    context.vlr_flagged_before = {}
    context.vlr_flagged_after = {}
    context.vlr_event_venues_before = {}


def _set_flag(context, key: str, value: bool) -> None:
    _ensure(context)
    context.vlr_redis.set(key, json.dumps(value))


def _register_crawl_target(context, handle: str) -> None:
    context.vlr_dao.upsert_crawl_target(handle, {
        "kind": "venue", "enabled": True, "cron": "0 0 * * *",
    })


def _map_handle_to_venue(context, handle: str, venue_id: str) -> None:
    context.vlr_dao.enrichment.setdefault("instagram.handle", {})[venue_id] = {
        "instagram_handle": handle,
    }


def _seed_event(context, handle: str, location_text: str, *, venue_id=None, **extra) -> str:
    context.vlr_seq += 1
    seen = _NOW + timedelta(seconds=context.vlr_seq)
    fields = {
        "event_id": new_event_id(), "venue_id": venue_id,
        # Its own day, so seeding several events for one venue never
        # incidentally creates a venue-night duplicate — a DIFFERENT
        # feature's report, which this harness must not perturb.
        "starts_at": _NOW + timedelta(days=context.vlr_seq),
        "title": "EVENT", "post_type": "event", "status": "accepted",
        "location_text": location_text, "lineup": [],
        "source_kind": "venue_post", "source_handle": handle,
        "source_shortcode": f"vlr_sc_{context.vlr_seq}",
        "first_seen_at": seen, "last_seen_at": seen,
    }
    fields.update(extra)
    context.vlr_dao.insert_event(fields)
    return fields["event_id"]


def _seed_case(context, case_id: str, *, drop_address=False, event_hook=None):
    """Seed one corpus case exactly as production stores it, and return
    `(case, handle, [venue_id, ...])`.

    A single-mapped handle's events carry `venue_id=<the mapped venue>` (the
    force-assigned shape plan §B measured); a double-mapped handle's carry
    `venue_id=None`, because that one DOES run the resolution ladder and both
    of its candidates fail the confidence floor.

    `event_hook(index, location_text) -> dict` injects extra event columns
    (a protected status, a manual resolution) for the skip-table scenarios.
    """
    _ensure(context)
    case = _case(case_id)
    handle = case["handle"]
    venue_ids = []
    for mapped in case["mapped_venues"]:
        venue_id = mapped["venue_id"]
        context.vlr_dao.upsert_venue(Venue(
            venue_id=venue_id, venue_name=mapped["venue_name"],
            venue_lat=-8.05, venue_lng=-34.88,
            venue_address=(
                f"{mapped.get('street') or ''}, {mapped.get('neighborhood') or ''}, "
                f"{mapped.get('city') or ''} - PE"
            ),
        ))
        context.vlr_dao.update_venue_address_components(
            venue_id, source="operator",
            street=mapped.get("street"), neighborhood=mapped.get("neighborhood"),
            city=mapped.get("city"),
        )
        if drop_address:
            # The real LEFT JOIN's missing-row shape: no `venues.address`
            # row at all, so neither the audit's neighbourhood check nor the
            # reviewer's street/city has anything to read.
            context.vlr_dao.addresses.pop(venue_id, None)
        _map_handle_to_venue(context, handle, venue_id)
        venue_ids.append(venue_id)
    _register_crawl_target(context, handle)

    multi_mapped = len(venue_ids) > 1
    for index, event in enumerate(case["events"]):
        extra = (event_hook(index, event["location_text"]) or {}) if event_hook else {}
        _seed_event(
            context, handle, event["location_text"],
            venue_id=None if multi_mapped else venue_ids[0], **extra,
        )

    for venue_id in venue_ids:
        context.vlr_pairs.append((handle, venue_id))
    context.vlr_case = case
    return case, handle, venue_ids


def _seed_default_pair_if_empty(context) -> None:
    _ensure(context)
    if context.vlr_pairs:
        return
    case, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID)
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)


def _non_corroborating_texts(case: dict, mapped: dict) -> list:
    return [
        e["location_text"] for e in case["events"]
        if venue_corroborates_location_text(
            mapped["venue_name"], mapped["neighborhood"], e["location_text"],
        ) is False
    ]


def _http(context) -> TestClient:
    _ensure(context)
    if context.vlr_http is None:
        app = FastAPI()
        app.include_router(admin_events_router)
        set_events_container(type("C", (), {
            "pipeline_repository": context.vlr_dao,
            "redis_client": context.vlr_redis,
        })())
        context.vlr_http = TestClient(app)
    return context.vlr_http


def _flagged_output(context) -> dict:
    """`{(handle, venue_id): venue_dict}` for every still-flagged pair, read
    through the REAL admin surface — so the router's own fold-in of stored
    reviews (`apply_reviews_to_audit`) is exercised, not bypassed."""
    response = _http(context).get("/admin/events/dedup-backlog")
    assert response.status_code == 200, response.text
    out = {}
    for candidate in response.json()["venue_link_audit_candidates"]:
        for venue in candidate["mapped_venues"]:
            if venue["flagged"]:
                out[(candidate["handle"], venue["venue_id"])] = venue
    return out


def _event_venue_map(context) -> dict:
    return {
        event_id: row.get("venue_id")
        for event_id, row in context.vlr_dao.events.items()
    }


def _run_reviewer(context) -> None:
    """Run the pass, recording the audit's flagged output and every event's
    venue on BOTH sides of it. Never lets an exception escape — "the reviewer
    run does not fail" must be an ASSERTION, not behave erroring the scenario
    before the assertion is reached."""
    _ensure(context)
    context.vlr_flagged_before = _flagged_output(context)
    context.vlr_event_venues_before = _event_venue_map(context)
    service = VenueLinkAuditReviewerService(
        context.vlr_dao, context.vlr_stub, redis_client=context.vlr_redis,
    )
    context.vlr_error = None
    try:
        context.vlr_result = asyncio.run(service.run())
    except Exception as e:  # pragma: no cover - the failure this pass forbids
        context.vlr_error = e
        context.vlr_result = None
    context.vlr_flagged_after = _flagged_output(context)


def _require_result(context) -> dict:
    if context.vlr_error is not None:
        raise AssertionError(
            f"the reviewer pass raised into its caller: {context.vlr_error!r}"
        )
    assert context.vlr_result is not None, "the reviewer never ran"
    return context.vlr_result


def _review_row(context, handle: str, venue_id: str):
    return context.vlr_dao.get_venue_link_audit_review(handle, venue_id)


def _reported_outcome(context, handle: str, venue_id: str):
    """What the RUN said about this pair, as distinct from what the stored
    record says. The two differ by design for a pair the run only observed:
    an operator-decided pair is reported `skipped_operator_decided` while its
    row keeps the machine verdict the operator rejected."""
    for outcome in _require_result(context)["outcomes"]:
        if (outcome["handle"], outcome["venue_id"]) == (handle, venue_id):
            return outcome
    raise AssertionError(
        f"the run reported nothing for {handle}/{venue_id}: "
        f"{_require_result(context)['outcomes']}"
    )


def _pair_row(context):
    handle, venue_id = context.vlr_pair
    row = _review_row(context, handle, venue_id)
    assert row is not None, (
        f"no review record for {handle}/{venue_id}; "
        f"stored={sorted(context.vlr_dao.venue_link_audit_reviews)}"
    )
    return row


def _pair_location_texts(context) -> list:
    """Every `location_text` the pair's own events carry, read back from the
    DAO — deliberately NOT the evidence bundle, so the verbatim assertion is
    independent of what the pass chose to show the model."""
    handle, _venue_id = context.vlr_pair
    return [
        (row.get("location_text") or "")
        for row in context.vlr_dao.list_events_by_handle(handle)
    ]


# ── Background ───────────────────────────────────────────────────────────


@given("the venue-link-audit reviewer fixture corpus of re-verified real pairs")
def step_given_reviewer_corpus(context):
    """Establishes the harness and pins that the committed corpus is the one
    the per-scenario Givens below draw every venue, address and
    `location_text` from — see
    `tests/fixtures/venue_link_audit_reviewer/MANIFEST.md` for each value's
    provenance."""
    _ensure(context)
    cases = _corpus()["cases"]
    assert cases, f"empty corpus at {_CORPUS_PATH}"
    context.vlr_corpus = {c["case_id"]: c for c in cases}


@given("the venue-link-audit reviewer flag is enabled")
def step_given_reviewer_flag_enabled(context):
    _set_flag(context, ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY, True)


@given("the venue-link-audit reviewer auto-apply flag is enabled")
def step_given_reviewer_auto_apply_enabled(context):
    _set_flag(context, ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY, True)


@given("the venue-link-audit reviewer flag is disabled")
def step_given_reviewer_flag_disabled(context):
    _set_flag(context, ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY, False)


@given("the venue-link-audit reviewer auto-apply flag is disabled")
def step_given_reviewer_auto_apply_disabled(context):
    _set_flag(context, ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY, False)


# ── the address is the signal the audit's own check cannot see ───────────


@given("a flagged pair whose posts repeatedly give the mapped venue's own street and number")
def step_given_street_and_number_pair(context):
    case, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID)
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)
    # The premise, asserted rather than assumed: the venue's own catalogued
    # street appears in NO post verbatim (Teresa vs Tereza), which is exactly
    # why the audit's name+neighbourhood check cannot rescue the pair.
    street = case["mapped_venues"][0]["street"]
    texts = [e["location_text"] for e in case["events"]]
    assert not any(street in t for t in texts), street


@given("a flagged pair whose catalogued neighbourhood disagrees with every post")
def step_given_catalogued_neighbourhood_wrong(context):
    case, handle, venue_ids = _seed_case(
        context, "lacasarecife_catalogued_neighbourhood_wrong",
    )
    context.vlr_pair = (handle, venue_ids[0])
    neighbourhood = case["mapped_venues"][0]["neighborhood"]
    context.vlr_catalogued_neighbourhood = neighbourhood
    folded = _fold_for_containment(neighbourhood)
    for event in case["events"]:
        assert folded not in _fold_for_containment(event["location_text"]), event


@given("whose posts give the mapped venue's own street and number")
def step_given_and_posts_give_street(context):
    case = context.vlr_case
    context.vlr_stub.program([_raw_from_case(case)], handle=case["handle"])


@given("a flagged pair whose posts name a short form contained in the mapped venue's own long catalogue name")
def step_given_short_name_form(context):
    case, handle, venue_ids = _seed_case(
        context, "rockandribsmarcozero_short_name_form_confirms",
    )
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)
    # The premise: the short form really IS a literal substring of the long
    # catalogue name — the audit flags it purely as a scoring artefact.
    short_form = case["canned_model_answer"]["evidence_quote"]
    assert short_form in case["mapped_venues"][0]["venue_name"], short_form


@given("a flagged pair whose mapped venue has a full structured address")
def step_given_full_structured_address(context):
    case, handle, venue_ids = _seed_case(
        context, "downtownbeergarden_street_number_confirms",
    )
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)
    address = context.vlr_dao.get_address(venue_ids[0])
    for field in ("street", "neighborhood", "city"):
        assert address.get(field), (field, address)


@given("a flagged pair whose mapped venue has no stored address row")
def step_given_no_stored_address_row(context):
    case, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID, drop_address=True)
    context.vlr_pair = (handle, venue_ids[0])
    assert context.vlr_dao.get_address(venue_ids[0]) is None
    # With no address row the neighbourhood check is gone too, so the only
    # text that still corroborates is the bare venue name — which is the one
    # the model may quote.
    context.vlr_stub.program(
        [_raw(VERDICT_CONFIRM, quote="Conchittas Bar", field="name",
              reason="The posts name this venue directly.")],
        handle=handle,
    )


# ── a contradiction escalates, and never force-fits a venue ──────────────


@given("a flagged pair whose posts consistently give a street and neighbourhood that are not the mapped venue's")
def step_given_contradicting_street(context):
    case, handle, venue_ids = _seed_case(
        context, "botecobeer_jsp_different_street_contradicts",
    )
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)
    mapped = case["mapped_venues"][0]
    assert _non_corroborating_texts(case, mapped) == [
        e["location_text"] for e in case["events"]
    ], "every post in this case must fail the audit's own check"


# ── the gates that decide whether a verdict may act ──────────────────────


@given("a flagged handle mapped to two sibling venues")
def step_given_double_mapped_handle(context):
    case, handle, venue_ids = _seed_case(
        context, "entreamigosobode_double_mapped_siblings",
    )
    assert len(venue_ids) == 2, venue_ids
    context.vlr_siblings = [(handle, v) for v in venue_ids]
    context.vlr_pair = context.vlr_siblings[0]


@given('every independent call returns "confirm" for both siblings')
def step_given_every_call_confirms_both_siblings(context):
    # Programmed deliberately: if the structural gate were advisory rather
    # than structural, this is precisely the answer that would wrongly close
    # BOTH siblings' flags (live: confirm 10/10 for each, on a shared pool).
    case = context.vlr_case
    context.vlr_stub.program(
        [_raw_from_case(case)] * DEFAULT_REVIEW_CONSENSUS_K, handle=case["handle"],
    )


@given('a flagged pair whose independent calls return a mixture of "confirm" and "insufficient"')
def step_given_mixed_calls(context):
    case, handle, venue_ids = _seed_case(
        context, "saladerebocorecife_genuine_offsite_gigs",
    )
    context.vlr_pair = (handle, venue_ids[0])
    confirms = 7
    insufficients = DEFAULT_REVIEW_CONSENSUS_K - confirms
    context.vlr_expected_tally = {"confirm": confirms, "insufficient": insufficients}
    context.vlr_stub.program(
        [_raw_from_case(case)] * confirms
        + [_raw_from_case(case, "canned_dissenting_answer")] * insufficients,
        handle=handle,
    )


@given('a flagged pair whose independent calls all return "confirm"')
def step_given_all_calls_confirm(context):
    case, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID)
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program(
        [_raw_from_case(case)] * DEFAULT_REVIEW_CONSENSUS_K, handle=handle,
    )


@given("one of those calls quotes text that was never supplied")
def step_given_one_call_quotes_unsupplied_text(context):
    handle, _venue_id = context.vlr_pair
    queue = context.vlr_stub.queue_for(handle)
    assert len(queue) == DEFAULT_REVIEW_CONSENSUS_K, len(queue)
    # A real location_text — from a DIFFERENT handle's live posts — so the
    # quote is provably absent from the texts THIS pair was shown, which is
    # exactly the "remembered rather than copied" failure the gate catches.
    unsupplied = _case(
        "botecobeer_jsp_different_street_contradicts"
    )["canned_model_answer"]["evidence_quote"]
    assert not any(
        unsupplied in e["location_text"] for e in context.vlr_case["events"]
    ), unsupplied
    queue[0] = _raw(
        VERDICT_CONFIRM, quote=unsupplied, field=FIELD_STREET,
        reason="A quote the model remembered rather than copied.",
    )


# ── the validator gate (driven directly) ─────────────────────────────────


def _supplied_texts(context) -> list:
    case = _case(_DEFAULT_CASE_ID)
    context.vlr_supplied_texts = [e["location_text"] for e in case["events"]]
    return context.vlr_supplied_texts


@given("a model answer whose evidence quote appears in no supplied location text")
def step_given_answer_quote_not_supplied(context):
    _ensure(context)
    texts = _supplied_texts(context)
    unsupplied = _case(
        "botecobeer_jsp_different_street_contradicts"
    )["canned_model_answer"]["evidence_quote"]
    assert not any(unsupplied in t for t in texts), unsupplied
    context.vlr_answer = ReviewAnswer(
        verdict=VERDICT_CONFIRM, evidence_quote=unsupplied,
        matched_field=FIELD_STREET, reason="bdd",
    )


@given("a model answer whose verdict is not one of confirm, contradict or insufficient")
def step_given_answer_unrecognised_verdict(context):
    _ensure(context)
    texts = _supplied_texts(context)
    context.vlr_answer = ReviewAnswer(
        verdict="probably_the_same_place", evidence_quote=texts[0],
        matched_field=FIELD_STREET, reason="bdd",
    )


@given('a model answer whose verdict is "confirm" and whose matched field is "none"')
def step_given_answer_acting_verdict_without_field(context):
    _ensure(context)
    texts = _supplied_texts(context)
    # The quote IS verbatim, so the only thing wrong with this answer is the
    # missing matched field — otherwise the rejection could not be attributed.
    context.vlr_answer = ReviewAnswer(
        verdict=VERDICT_CONFIRM, evidence_quote=texts[0],
        matched_field=FIELD_NONE, reason="bdd",
    )


@given("a flagged pair whose model call returns an empty response")
def step_given_empty_model_response(context):
    _case_, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID)
    context.vlr_pair = (handle, venue_ids[0])
    # The plan §F confound, reproduced exactly: a reasoning model whose
    # max_completion_tokens is too small returns an EMPTY body, never a
    # malformed one.
    context.vlr_stub.program([""], handle=handle)


# ── the skip table still decides what may be touched ─────────────────────


@given("a flagged pair whose non-corroborating events are all confirmed")
def step_given_non_corroborating_events_confirmed(context):
    _ensure(context)
    case = _case(_DEFAULT_CASE_ID)
    mapped = case["mapped_venues"][0]
    protected = set(_non_corroborating_texts(case, mapped))
    assert protected, case["case_id"]

    def _hook(_index, location_text):
        if location_text in protected:
            return {"status": STATUS_CONFIRMED}
        return {}

    _case_, handle, venue_ids = _seed_case(
        context, _DEFAULT_CASE_ID, event_hook=_hook,
    )
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_protected_texts = sorted(protected)
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)


def _seed_pair_with_one_protected_event(context, extra_columns: dict):
    """The corpus case seeded normally except that its ONE uniquely-spelled
    non-corroborating post carries a protection. Unique spelling matters: a
    text that also appears on an unprotected event would still reach the
    evidence and the absence assertion would prove nothing."""
    _ensure(context)
    case = _case(_DEFAULT_CASE_ID)
    mapped = case["mapped_venues"][0]
    counts: dict[str, int] = {}
    for event in case["events"]:
        counts[event["location_text"]] = counts.get(event["location_text"], 0) + 1
    unique_non_corroborating = [
        t for t in _non_corroborating_texts(case, mapped) if counts[t] == 1
    ]
    assert unique_non_corroborating, counts
    target = unique_non_corroborating[0]

    def _hook(_index, location_text):
        return dict(extra_columns) if location_text == target else {}

    _case_, handle, venue_ids = _seed_case(
        context, _DEFAULT_CASE_ID, event_hook=_hook,
    )
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_protected_text = target
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)


@given("a flagged pair carrying an event whose location resolution is manual")
def step_given_manual_linked_event(context):
    _seed_pair_with_one_protected_event(
        context, {"location_resolution": RESOLUTION_MANUAL},
    )


@given("a flagged pair carrying an event whose operator-edited fields include its venue")
def step_given_operator_edited_venue_event(context):
    _seed_pair_with_one_protected_event(
        context, {"operator_edited_fields": ["venue_id"]},
    )


# ── the record is durable, re-openable, and an operator overrides it ─────


_EVERY_KIND = (
    (_DEFAULT_CASE_ID, OUTCOME_CONFIRMED),
    ("botecobeer_jsp_different_street_contradicts", OUTCOME_CONTRADICTED),
    ("entreamigosobode_double_mapped_siblings", OUTCOME_SKIPPED_MULTI_MAPPED),
    ("saladerebocorecife_genuine_offsite_gigs", OUTCOME_NO_CONSENSUS),
)


def _seed_every_kind(context) -> None:
    """One flagged pair of each outcome shape, all in the same store, so the
    record/list scenarios see a realistic mixed queue rather than one row."""
    _ensure(context)
    expected: dict = {}
    for case_id, outcome in _EVERY_KIND:
        case, handle, venue_ids = _seed_case(context, case_id)
        if outcome is OUTCOME_NO_CONSENSUS:
            confirms = 7
            context.vlr_stub.program(
                [_raw_from_case(case)] * confirms
                + [_raw_from_case(case, "canned_dissenting_answer")]
                * (DEFAULT_REVIEW_CONSENSUS_K - confirms),
                handle=handle,
            )
        else:
            context.vlr_stub.program([_raw_from_case(case)], handle=handle)
        for venue_id in venue_ids:
            expected[(handle, venue_id)] = outcome
    context.vlr_expected_outcomes = expected


@given("flagged pairs that the reviewer confirms, contradicts, skips and fails to agree on")
def step_given_pairs_of_every_kind(context):
    _seed_every_kind(context)


@given("the reviewer has recorded verdicts of every kind")
def step_given_recorded_verdicts_of_every_kind(context):
    _seed_every_kind(context)
    _run_reviewer(context)
    _require_result(context)


def _seed_and_confirm_pair(context) -> None:
    case, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID)
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program([_raw_from_case(case)], handle=handle)
    _run_reviewer(context)
    _require_result(context)
    row = _pair_row(context)
    assert row["verdict"] == VERDICT_CONFIRM, row
    assert row["outcome"] == OUTCOME_CONFIRMED, row
    assert context.vlr_pair in context.vlr_flagged_before, context.vlr_flagged_before
    assert context.vlr_pair not in context.vlr_flagged_after, context.vlr_flagged_after
    # The SUPPRESSED baseline, kept separately: the re-opening scenarios must
    # compare against "the pair was gone from the report", not against the
    # pre-review snapshot, where it was still flagged.
    context.vlr_flagged_when_suppressed = dict(context.vlr_flagged_after)
    # The staleness anchor must actually be stored, or the re-opening
    # scenario would "pass" against a review that could never suppress.
    assert row["non_corroborating_count_at_review"] is not None, row


@given("a pair the reviewer already confirmed and suppressed")
def step_given_already_confirmed_and_suppressed(context):
    _seed_and_confirm_pair(context)


@given("a pair the reviewer confirmed and suppressed")
def step_given_confirmed_and_suppressed(context):
    _seed_and_confirm_pair(context)


@given("a flagged pair every independent call confirms")
def step_given_pair_every_call_confirms(context):
    case, handle, venue_ids = _seed_case(context, _DEFAULT_CASE_ID)
    context.vlr_pair = (handle, venue_ids[0])
    context.vlr_stub.program(
        [_raw_from_case(case)] * DEFAULT_REVIEW_CONSENSUS_K, handle=handle,
    )


@given("the reviewer has already run over the audit's flagged pairs")
def step_given_reviewer_already_ran(context):
    _seed_and_confirm_pair(context)
    context.vlr_first_rows = {
        key: dict(row)
        for key, row in context.vlr_dao.venue_link_audit_reviews.items()
    }
    context.vlr_first_flagged = dict(context.vlr_flagged_after)
    context.vlr_first_calls = context.vlr_stub.calls


# ── When ─────────────────────────────────────────────────────────────────


@when("the reviewer runs over the audit's flagged pairs")
def step_when_reviewer_runs(context):
    _seed_default_pair_if_empty(context)
    _run_reviewer(context)


@when("the reviewer runs again over the same unchanged data")
def step_when_reviewer_runs_again(context):
    _run_reviewer(context)


@when("the reviewer builds the evidence for that pair")
def step_when_reviewer_builds_evidence(context):
    _run_reviewer(context)
    _require_result(context)
    handle, venue_id = context.vlr_pair
    context.vlr_evidence = context.vlr_stub.evidence_for(handle, venue_id)
    assert context.vlr_evidence is not None, (
        f"the pass never built evidence for {handle}/{venue_id}"
    )


@when("the reviewer validates that answer")
def step_when_reviewer_validates(context):
    context.vlr_validation = validate_venue_link_review(
        context.vlr_answer, location_texts=context.vlr_supplied_texts,
    )


@when("the pair's non-corroborating count grows beyond the count recorded at review")
def step_when_non_corroborating_count_grows(context):
    handle, venue_id = context.vlr_pair
    case = context.vlr_case
    mapped = case["mapped_venues"][0]
    new_text = _non_corroborating_texts(case, mapped)[0]
    for _ in range(2):
        _seed_event(context, handle, new_text, venue_id=venue_id)
    grown = _http(context).get("/admin/events/dedup-backlog")
    assert grown.status_code == 200, grown.text
    context.vlr_flagged_after = _flagged_output(context)


@when("an operator rejects that review")
def step_when_operator_rejects_review(context):
    handle, venue_id = context.vlr_pair
    response = _http(context).post(
        f"/admin/events/venue-link-audit/reviews/{handle}/{venue_id}/decision",
        json={"decision": DECISION_REJECTED, "note": "the machine is wrong here"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["operator_decision"] == DECISION_REJECTED, response.json()
    context.vlr_flagged_after = _flagged_output(context)


@when("an operator requests the venue-link-audit review list")
def step_when_operator_requests_review_list(context):
    response = _http(context).get("/admin/events/venue-link-audit/reviews")
    assert response.status_code == 200, response.text
    context.vlr_review_list = response.json()


# ── Then: verdict, matched field, evidence ───────────────────────────────


@then('the pair\'s recorded verdict is "{verdict}"')
def step_then_recorded_verdict(context, verdict):
    _require_result(context)
    row = _pair_row(context)
    assert row["verdict"] == verdict, row


@then('the recorded matched field is "{field}"')
def step_then_recorded_matched_field(context, field):
    row = _pair_row(context)
    assert row["matched_field"] == field, row


@then("the recorded evidence quote appears verbatim in one of the pair's location texts")
def step_then_evidence_quote_verbatim(context):
    row = _pair_row(context)
    quote = row["evidence_quote"]
    assert quote, row
    texts = _pair_location_texts(context)
    assert any(quote in t for t in texts), (quote, texts)


@then("the pair no longer appears in the audit's flagged output")
def step_then_pair_no_longer_flagged(context):
    assert context.vlr_pair in context.vlr_flagged_before, (
        "the audit never flagged this pair, so suppressing it proves nothing"
    )
    assert context.vlr_pair not in context.vlr_flagged_after, context.vlr_flagged_after


@then("the review record notes that the catalogued neighbourhood was not the matching field")
def step_then_record_notes_neighbourhood_not_matching(context):
    row = _pair_row(context)
    assert row["matched_field"] == FIELD_STREET, row
    assert row["matched_field"] != FIELD_NEIGHBORHOOD, row
    assert (row["reason"] or "").strip(), row
    # Not circular: the record cites a quote from a text that does NOT carry
    # the catalogued neighbourhood, and the catalogued neighbourhood appears
    # in none of the pair's own texts at all.
    folded_neighbourhood = _fold_for_containment(context.vlr_catalogued_neighbourhood)
    quoted_from = [t for t in _pair_location_texts(context) if row["evidence_quote"] in t]
    assert quoted_from, row
    for text in quoted_from:
        assert folded_neighbourhood not in _fold_for_containment(text), text


@then("the evidence includes the venue's street, neighbourhood and city")
def step_then_evidence_includes_address(context):
    evidence = context.vlr_evidence
    address = context.vlr_dao.get_address(context.vlr_pair[1])
    assert evidence.street == address["street"], (evidence, address)
    assert evidence.neighborhood == address["neighborhood"], (evidence, address)
    assert evidence.city == address["city"], (evidence, address)
    assert evidence.venue_name, evidence


@then("the evidence includes both the corroborating and the non-corroborating location texts")
def step_then_evidence_includes_both_sides(context):
    evidence = context.vlr_evidence
    case = context.vlr_case
    mapped = case["mapped_venues"][0]
    expected_non = set(_non_corroborating_texts(case, mapped))
    expected_corroborating = {
        e["location_text"] for e in case["events"]
    } - expected_non
    assert set(evidence.non_corroborating) == expected_non, evidence
    assert set(evidence.corroborating) == expected_corroborating, evidence
    assert expected_corroborating and expected_non, case["case_id"]


@then("the reviewer still reaches a recorded outcome for that pair")
def step_then_still_reaches_outcome(context):
    _require_result(context)
    row = _pair_row(context)
    assert row["outcome"] in _ALL_OUTCOMES, row
    # The degrade, pinned: no address row, so no street/neighbourhood/city
    # reached the model — but the venue's NAME still did.
    evidence = context.vlr_stub.evidence_for(*context.vlr_pair)
    assert evidence is not None, context.vlr_stub.evidence_seen
    assert evidence.street is None and evidence.neighborhood is None, evidence
    assert evidence.city is None, evidence
    assert evidence.venue_name, evidence


@then("the reviewer run does not fail")
def step_then_reviewer_run_does_not_fail(context):
    # Reworded from the feature's original "the run does not fail", which is
    # already registered by tests/bdd/steps/extract_by_handle_steps.py in
    # this repo's single shared step namespace.
    assert context.vlr_error is None, context.vlr_error
    result = _require_result(context)
    assert result.get("enabled") is True, result
    assert isinstance(result.get("outcomes"), list), result


# ── Then: contradiction ──────────────────────────────────────────────────


@then("the pair still appears in the audit's flagged output")
def step_then_pair_still_flagged(context):
    assert context.vlr_pair in context.vlr_flagged_before, context.vlr_flagged_before
    assert context.vlr_pair in context.vlr_flagged_after, context.vlr_flagged_after


@then("the review record states that the mapped venue is not the place the posts describe")
def step_then_record_states_not_the_place(context):
    row = _pair_row(context)
    assert row["outcome"] == OUTCOME_CONTRADICTED, row
    assert (row["reason"] or "").strip(), row
    assert row["evidence_quote"], row
    # The quote is one of the pair's OWN non-corroborating texts — the machine
    # cited the street the posts actually give, not the catalogued one.
    case = context.vlr_case
    mapped = case["mapped_venues"][0]
    non_corroborating = _non_corroborating_texts(case, mapped)
    assert any(row["evidence_quote"] in t for t in non_corroborating), row
    # …and it is NOT the catalogued street: a contradict never force-fits.
    assert row["evidence_quote"] not in (mapped["street"] or ""), row
    # The still-flagged pair carries its machine verdict inline for an
    # operator, additively, on the audit's own output.
    inline = context.vlr_flagged_after[context.vlr_pair]["review"]
    assert inline["verdict"] == row["verdict"], inline
    assert inline["outcome"] == row["outcome"], inline


@then("no event row's venue is changed")
def step_then_no_event_venue_changed(context):
    after = _event_venue_map(context)
    assert after == context.vlr_event_venues_before, (
        context.vlr_event_venues_before, after,
    )
    # Nothing else on the row either — this pass writes no events.post_item
    # column at all (plan Non-goals).
    for row in context.vlr_dao.events.values():
        assert row.get("location_resolution") in (None, RESOLUTION_MANUAL), row
        assert row.get("linked_by") is None, row


# ── Then: the structural multi-mapped gate ───────────────────────────────


@then("neither sibling is suppressed from the audit's flagged output")
def step_then_neither_sibling_suppressed(context):
    for pair in context.vlr_siblings:
        assert pair in context.vlr_flagged_before, (pair, context.vlr_flagged_before)
        assert pair in context.vlr_flagged_after, (pair, context.vlr_flagged_after)


@then('each sibling\'s recorded outcome is "{outcome}"')
def step_then_each_sibling_outcome(context, outcome):
    _require_result(context)
    for handle, venue_id in context.vlr_siblings:
        row = _review_row(context, handle, venue_id)
        assert row is not None, (handle, venue_id)
        assert row["outcome"] == outcome, row
        assert row["verdict"] is None, row


@then("no model call is charged for either sibling")
def step_then_no_model_call_for_either_sibling(context):
    assert context.vlr_stub.calls == 0, context.vlr_stub.calls
    assert context.vlr_stub.evidence_seen == [], context.vlr_stub.evidence_seen


# ── Then: consensus, validator, empty ────────────────────────────────────


@then('the pair\'s recorded outcome is "{outcome}"')
def step_then_recorded_outcome(context, outcome):
    _require_result(context)
    row = _pair_row(context)
    assert row["outcome"] == outcome, row


@then("the review record carries the tally of every verdict returned")
def step_then_record_carries_tally(context):
    row = _pair_row(context)
    assert row["tally"] == context.vlr_expected_tally, row
    assert sum(row["tally"].values()) == DEFAULT_REVIEW_CONSENSUS_K, row
    assert row["consensus_k"] == DEFAULT_REVIEW_CONSENSUS_K, row


@then("the pair is not suppressed from the audit's flagged output")
def step_then_pair_not_suppressed(context):
    assert context.vlr_pair in context.vlr_flagged_before, context.vlr_flagged_before
    assert context.vlr_pair in context.vlr_flagged_after, context.vlr_flagged_after


@then("the answer is rejected for evidence that is not verbatim")
def step_then_rejected_not_verbatim(context):
    result = context.vlr_validation
    assert result.accepted is False, result
    assert result.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM, result


@then("the answer is rejected as unrecognised")
def step_then_rejected_unrecognised(context):
    result = context.vlr_validation
    assert result.accepted is False, result
    assert result.rejection_reason == REJECTION_VERDICT_NOT_RECOGNISED, result


@then("the answer is rejected")
def step_then_answer_rejected(context):
    result = context.vlr_validation
    assert result.accepted is False, result
    assert result.rejection_reason == REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE, result


@then("nothing is written for that pair")
def step_then_nothing_written_for_pair(context):
    # The gate itself writes nothing, and hands the caller nothing actionable
    # — a rejected ReviewVerdict must never carry a readable verdict.
    result = context.vlr_validation
    assert result.verdict is None, result
    assert result.evidence_quote is None, result
    assert result.matched_field is None, result
    assert context.vlr_dao.venue_link_audit_reviews == {}, (
        context.vlr_dao.venue_link_audit_reviews
    )
    assert context.vlr_dao.events == {}, context.vlr_dao.events


@then("the outcome is not counted as a validator rejection")
def step_then_not_counted_as_validator_rejection(context):
    result = _require_result(context)
    counts = result["counts"]
    assert counts.get(OUTCOME_EMPTY_RESPONSE) == 1, counts
    assert OUTCOME_VALIDATOR_REJECTED not in counts, counts
    row = _pair_row(context)
    assert OUTCOME_VALIDATOR_REJECTED not in (row["tally"] or {}), row
    assert (row["tally"] or {}).get(OUTCOME_EMPTY_RESPONSE) == DEFAULT_REVIEW_CONSENSUS_K, row


# ── Then: the skip table ─────────────────────────────────────────────────


@then("those events are not shown to the model")
def step_then_protected_events_not_shown(context):
    assert context.vlr_stub.calls == 0, context.vlr_stub.calls
    for evidence in context.vlr_stub.evidence_seen:
        for text in context.vlr_protected_texts:
            assert text not in evidence.location_texts, (text, evidence)


@then("that event's location text is not included in the evidence")
def step_then_protected_text_not_in_evidence(context):
    evidence = context.vlr_evidence
    assert context.vlr_protected_text not in evidence.location_texts, (
        context.vlr_protected_text, evidence.location_texts,
    )
    # The pair was still reviewed on its remaining, unprotected evidence —
    # the protection removes one row, it does not silence the whole pair.
    assert evidence.non_corroborating, evidence
    assert evidence.protected_event_count == 1, evidence


# ── Then: the durable record ─────────────────────────────────────────────


@then("each of those pairs has exactly one review record")
def step_then_one_record_per_pair(context):
    _require_result(context)
    stored = context.vlr_dao.venue_link_audit_reviews
    expected = set(context.vlr_expected_outcomes)
    assert set(stored) == expected, (sorted(stored), sorted(expected))
    assert len(stored) == len(expected), stored


@then("each record carries its verdict, its outcome and the audit counts as of the review")
def step_then_record_carries_verdict_outcome_counts(context):
    for pair, expected_outcome in context.vlr_expected_outcomes.items():
        row = _review_row(context, *pair)
        assert row is not None, pair
        assert row["outcome"] == expected_outcome, row
        flagged = context.vlr_flagged_before[pair]
        assert row["checkable_count_at_review"] == flagged["checkable_count"], (row, flagged)
        assert (
            row["non_corroborating_count_at_review"] == flagged["non_corroborating_count"]
        ), (row, flagged)
        if expected_outcome in (OUTCOME_CONFIRMED, OUTCOME_CONTRADICTED, OUTCOME_INSUFFICIENT):
            assert row["verdict"], row
            assert row["evidence_quote"], row
            assert row["matched_field"], row
        else:
            # A skipped or undecided pair must not invent a verdict it never
            # reached — an absent verdict and a verdict are different states.
            assert row["verdict"] is None, row
    outcomes = {r["outcome"] for r in context.vlr_dao.venue_link_audit_reviews.values()}
    assert outcomes == set(context.vlr_expected_outcomes.values()), outcomes


@then("the pair appears in the audit's flagged output again")
def step_then_pair_flagged_again(context):
    assert context.vlr_pair not in context.vlr_flagged_when_suppressed, (
        "the pair was not suppressed to begin with, so re-opening proves nothing"
    )
    assert context.vlr_pair in context.vlr_flagged_after, context.vlr_flagged_after


@then("a later reviewer run makes no model call for that pair")
def step_then_later_run_makes_no_model_call(context):
    calls_before = context.vlr_stub.calls
    _run_reviewer(context)
    _require_result(context)
    assert context.vlr_stub.calls == calls_before, (
        calls_before, context.vlr_stub.calls,
    )
    reported = _reported_outcome(context, *context.vlr_pair)
    assert reported["outcome"] == OUTCOME_SKIPPED_OPERATOR_DECIDED, reported
    assert reported["persist"] is False, reported
    # The RUN skips it; the RECORD keeps the machine verdict the operator
    # rejected, which is the whole point of the durable log — a re-run must
    # never overwrite what a human decided about.
    row = _pair_row(context)
    assert row["operator_decision"] == DECISION_REJECTED, row
    assert row["verdict"] == VERDICT_CONFIRM, row
    assert row["outcome"] == OUTCOME_CONFIRMED, row
    # Permanent: still flagged after the re-run, never re-suppressed.
    assert context.vlr_pair in context.vlr_flagged_after, context.vlr_flagged_after


@then("every recorded review is returned with its verdict, evidence quote and reason")
def step_then_review_list_complete(context):
    listed = {(r["handle"], r["venue_id"]): r for r in context.vlr_review_list}
    stored = context.vlr_dao.venue_link_audit_reviews
    assert set(listed) == set(stored), (sorted(listed), sorted(stored))
    for pair, row in listed.items():
        stored_row = stored[pair]
        assert row["outcome"] == stored_row["outcome"], row
        assert row["verdict"] == stored_row["verdict"], row
        assert row["evidence_quote"] == stored_row["evidence_quote"], row
        assert row["reason"] == stored_row["reason"], row
        assert row["tally"] == (stored_row["tally"] or {}), row
    acting = [r for r in listed.values() if r["verdict"] in ("confirm", "contradict")]
    assert acting, listed
    for row in acting:
        assert row["evidence_quote"], row
        assert (row["reason"] or "").strip(), row


@then("the list can be narrowed to a single verdict")
def step_then_review_list_filterable(context):
    response = _http(context).get(
        "/admin/events/venue-link-audit/reviews", params={"verdict": VERDICT_CONFIRM},
    )
    assert response.status_code == 200, response.text
    narrowed = response.json()
    assert narrowed, narrowed
    assert len(narrowed) < len(context.vlr_review_list), (narrowed, context.vlr_review_list)
    assert {r["verdict"] for r in narrowed} == {VERDICT_CONFIRM}, narrowed


# ── Then: off by default, and unchanged on a second run ──────────────────


@then("no model call is charged for any pair")
def step_then_no_model_call_charged_for_any_pair(context):
    # Reworded from the feature's original "no model call is made", which is
    # already registered by tests/bdd/steps/event_venue_targeting_steps.py in
    # this repo's single shared step namespace.
    assert context.vlr_stub.calls == 0, context.vlr_stub.calls
    result = _require_result(context)
    assert result.get("enabled") is False, result
    assert result["outcomes"] == [], result


@then("no review record is written")
def step_then_no_review_record_written(context):
    assert context.vlr_dao.venue_link_audit_reviews == {}, (
        context.vlr_dao.venue_link_audit_reviews
    )


def _comparable(flagged: dict) -> dict:
    return {
        pair: (venue["checkable_count"], venue["non_corroborating_count"], venue["review"])
        for pair, venue in flagged.items()
    }


@then("the audit's flagged output is unchanged")
def step_then_flagged_output_unchanged(context):
    assert context.vlr_flagged_before, "nothing was flagged, so this proves nothing"
    assert _comparable(context.vlr_flagged_after) == _comparable(context.vlr_flagged_before)


@then("the pair's verdict is recorded")
def step_then_verdict_recorded(context):
    _require_result(context)
    row = _pair_row(context)
    assert row["verdict"] == VERDICT_CONFIRM, row
    assert row["outcome"] == OUTCOME_CONFIRMED, row
    # Shadow mode is genuinely inert, not merely hidden: with auto-apply off
    # the row stores no count-at-review, which is what suppression requires.
    assert row["non_corroborating_count_at_review"] is None, row


@then("no review record's verdict changes")
def step_then_no_review_verdict_changes(context):
    _require_result(context)
    second = context.vlr_dao.venue_link_audit_reviews
    first = context.vlr_first_rows
    assert set(second) == set(first), (sorted(second), sorted(first))
    for pair, row in second.items():
        before = first[pair]
        for field in (
            "verdict", "outcome", "evidence_quote", "matched_field", "reason",
            "tally", "consensus_k", "model", "checkable_count_at_review",
            "non_corroborating_count_at_review",
        ):
            assert row[field] == before[field], (pair, field, before[field], row[field])
    # …and it was free: a second pass over unchanged data re-spends nothing.
    assert context.vlr_stub.calls == context.vlr_first_calls, (
        context.vlr_first_calls, context.vlr_stub.calls,
    )


@then("the audit's flagged output is the same as after the first run")
def step_then_flagged_output_same_as_first_run(context):
    assert _comparable(context.vlr_flagged_after) == _comparable(context.vlr_first_flagged)
