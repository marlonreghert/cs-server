"""Unit tests for `app.services.venue_link_audit_reviewer` —
plans/260914_agentic-venue-resolution-fallback.md §2/§4/§5.

Division of labour against the sibling BDD feature
(`tests/bdd/enrichment/agentic-venue-resolution-fallback.feature`): the BDD
scenarios pin the USER-FACING behaviour in Gherkin; this file pins the pure
core's exact edge cases and then drives the adapter end-to-end against
`tests.rds_fake.InMemoryRdsVenueStore` + fakeredis + a FAKE review client —
never a database, never Redis, never OpenAI.

Every venue name, street, neighbourhood and `location_text` below is a real,
re-verified production value from the plan's §A/§E/§F tables — the same
convention `tests/test_venue_link_audit.py` follows, for the same reason: a
case central to the design must not turn on an invented string.
"""
from __future__ import annotations

import copy
import json

import fakeredis
import pytest

from app.models.venue import Venue
from app.services.event_link_skip import (
    SKIP_CONFIRMED,
    SKIP_EXTRACTION_FAILED,
    SKIP_MANUAL_LINK,
    SKIP_OPERATOR_EDITED_VENUE,
    SKIP_REJECTED,
    SKIP_SUPERSEDED,
    skip_reason,
)
from app.services.venue_link_audit import compute_venue_link_audit
from app.services.venue_link_audit_reviewer import (
    ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY,
    ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY,
    DECISION_ACCEPTED,
    DECISION_REJECTED,
    DEFAULT_REVIEW_CONSENSUS_K,
    DEFAULT_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_ENABLED,
    DEFAULT_VENUE_LINK_AUDIT_REVIEWER_ENABLED,
    MIN_REVIEW_CONSENSUS_K,
    OUTCOME_CONFIRMED,
    OUTCOME_CONTRADICTED,
    OUTCOME_EMPTY_RESPONSE,
    OUTCOME_ERROR,
    OUTCOME_INSUFFICIENT,
    OUTCOME_NO_CONSENSUS,
    OUTCOME_SKIPPED_MULTI_MAPPED,
    OUTCOME_SKIPPED_OPERATOR_DECIDED,
    OUTCOME_VALIDATOR_REJECTED,
    VenueLinkAuditReviewerService,
    apply_reviews_to_audit,
    build_review_evidence,
    load_venue_link_audit_reviewer_auto_apply_enabled,
    load_venue_link_audit_reviewer_enabled,
    parse_venue_link_review_response,
    review_suppresses_pair,
    tally_consensus,
    validate_venue_link_audit_reviewer_auto_apply_config,
    validate_venue_link_audit_reviewer_enabled_config,
)
from app.services.venue_link_audit_reviewer_validator import (
    FIELD_NEIGHBORHOOD,
    FIELD_STREET,
    VERDICT_CONFIRM,
    VERDICT_CONTRADICT,
    VERDICT_INSUFFICIENT,
)
from tests.rds_fake import InMemoryRdsVenueStore

# ── real, re-verified production values (plan §A/§E/§F) ─────────────────────
CONCHITTAS_HANDLE = "conchittasbar"
CONCHITTAS_VENUE = "conchittas_bar"
CONCHITTAS_NAME = "Conchittas Bar"
CONCHITTAS_STREET = "R. Imperatriz Teresa Cristina 218"   # the CATALOG spelling
CONCHITTAS_NEIGHBOURHOOD = "Santo Antônio"
# The handle's own posts, ×20 live — one letter from the catalog street
# ("Tereza" vs "Teresa"), which is precisely why the deterministic audit
# flags this pair and why the address is the load-bearing signal (plan §D).
CONCHITTAS_POST_TEXT = "Rua da Imperatriz Tereza Cristina, 218"

SALA_HANDLE = "saladerebocorecife"
SALA_VENUE = "sala_de_reboco"
SALA_NAME = "Sala de Reboco"
SALA_STREET = "R. Gregório Júnior, 264"
SALA_NEIGHBOURHOOD = "Cordeiro"
SALA_CORROBORATING = "Sala de Reboco - R. Gregório Júnior, 264"
# The two genuine off-site gigs (plan §E: "mapping correct, 2 genuine off-site
# gigs") — real non-corroborating evidence that is NOT a defect.
SALA_OFFSITE_A = "Praia de Boa Viagem – Quiosque 13"
SALA_OFFSITE_B = "BONITO – PE"

BODE_HANDLE = "entreamigosobode"
BODE_ESPINHEIRO = ("entre_amigos_espinheiro", "Entre Amigos O Bode Espinheiro",
                   "R. da Hora, 695", "Espinheiro")
BODE_BOA_VIAGEM = ("entre_amigos_boa_viagem", "Entre Amigos O Bode",
                   "R. Marquês de Valença, 50", "Boa Viagem")
# The shared, mutually-exclusive pool of 12 events plan §F item 3 measured
# BOTH siblings confirming 10/10 — the structural gate's whole reason to exist.
BODE_POST_TEXT = "Amigos Park"

RECIFE = "Recife"


# ── fixtures / helpers ──────────────────────────────────────────────────────

class _FakeReviewClient:
    """Stands in for `app.api.venue_link_review_client.VenueLinkReviewClient`.

    The service reads exactly two things off the client — a `model` attribute
    (stored on the review row) and `async def review_venue_link(*, evidence)`
    returning the RAW response text — so this fake exposes exactly those two
    and nothing else. A queued `Exception` is RAISED rather than returned,
    which is how the real client reports an API failure.
    """

    def __init__(self, responses=(), *, model="gpt-5.6-luna"):
        self.model = model
        self._responses = list(responses)
        self.calls: list = []

    async def review_venue_link(self, *, evidence):
        self.calls.append(evidence)
        if not self._responses:
            raise AssertionError(
                "the service made more model calls than the test queued "
                f"(call #{len(self.calls)})"
            )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _answer(verdict, *, quote=None, field=None, reason="one short sentence") -> str:
    """One raw model reply, in the JSON shape the prompt asks for."""
    return json.dumps({
        "verdict": verdict, "evidence_quote": quote,
        "matched_field": field, "reason": reason,
    }, ensure_ascii=False)


def _confirm_conchittas() -> str:
    return _answer(VERDICT_CONFIRM, quote=CONCHITTAS_POST_TEXT, field=FIELD_STREET,
                   reason="the post gives the same street and number as the catalog venue")


def _redis(*, enabled=None, auto_apply=None):
    """A fakeredis admin-config mirror. A flag left at `None` is simply not
    stored, so the loader falls back to its shipped default — which is how
    production looks before anyone ever sets these keys."""
    client = fakeredis.FakeRedis(decode_responses=True)
    if enabled is not None:
        client.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY, json.dumps(enabled))
    if auto_apply is not None:
        client.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY, json.dumps(auto_apply))
    return client


def _seed_venue(store, *, venue_id, venue_name, street, neighborhood, handle,
                city=RECIFE):
    store.upsert_venue(Venue(
        venue_id=venue_id, venue_name=venue_name, venue_lat=-8.06, venue_lng=-34.88,
        venue_address=f"{street}, {neighborhood}, {city} - PE",
    ))
    store.update_venue_address_components(
        venue_id, source="operator", street=street, neighborhood=neighborhood, city=city,
    )
    store.enrichment.setdefault("instagram.handle", {})[venue_id] = {
        "instagram_handle": handle,
    }
    store.upsert_crawl_target(handle, {"kind": "venue", "enabled": True, "cron": "0 0 * * *"})


def _seed_event(store, *, handle, venue_id, index, location_text, **overrides):
    row = {
        "event_id": f"evt_{handle}_{index}", "venue_id": venue_id, "starts_at": None,
        "title": f"EVENT {index}", "post_type": "event", "status": "accepted",
        "location_text": location_text, "lineup": [],
        "source_kind": "venue_post", "source_handle": handle,
        "source_shortcode": f"sc_{handle}_{index}",
    }
    row.update(overrides)
    store.insert_event(row)


def _seed_conchittas(store, *, non_corroborating=2, corroborating=1):
    """The `conchittasbar` shape: a single-mapped handle whose posts give the
    venue's own street in a spelling the deterministic check cannot match."""
    _seed_venue(store, venue_id=CONCHITTAS_VENUE, venue_name=CONCHITTAS_NAME,
                street=CONCHITTAS_STREET, neighborhood=CONCHITTAS_NEIGHBOURHOOD,
                handle=CONCHITTAS_HANDLE)
    index = 0
    for _ in range(non_corroborating):
        _seed_event(store, handle=CONCHITTAS_HANDLE, venue_id=CONCHITTAS_VENUE,
                    index=index, location_text=CONCHITTAS_POST_TEXT)
        index += 1
    for _ in range(corroborating):
        _seed_event(store, handle=CONCHITTAS_HANDLE, venue_id=CONCHITTAS_VENUE,
                    index=index, location_text=CONCHITTAS_NAME)
        index += 1
    return store


def _seed_sala(store):
    """The `saladerebocorecife` shape: mapping correct, 2 genuine off-site
    gigs — the pair plan §F measured as legitimately non-unanimous."""
    _seed_venue(store, venue_id=SALA_VENUE, venue_name=SALA_NAME, street=SALA_STREET,
                neighborhood=SALA_NEIGHBOURHOOD, handle=SALA_HANDLE)
    for i, text in enumerate(
        [SALA_CORROBORATING] * 3 + [SALA_OFFSITE_A, SALA_OFFSITE_B]
    ):
        _seed_event(store, handle=SALA_HANDLE, venue_id=SALA_VENUE, index=i,
                    location_text=text)
    return store


def _seed_bode(store):
    """The `entreamigosobode` shape: ONE handle, TWO mapped venues, one shared
    pool of 12 events that cannot belong to both."""
    for venue_id, name, street, neighbourhood in (BODE_ESPINHEIRO, BODE_BOA_VIAGEM):
        _seed_venue(store, venue_id=venue_id, venue_name=name, street=street,
                    neighborhood=neighbourhood, handle=BODE_HANDLE)
    for i in range(12):
        _seed_event(store, handle=BODE_HANDLE, venue_id=None, index=i,
                    location_text=BODE_POST_TEXT)
    return store


def _service(store, client, *, redis_client, consensus_k=DEFAULT_REVIEW_CONSENSUS_K):
    return VenueLinkAuditReviewerService(
        store, client, redis_client=redis_client, consensus_k=consensus_k,
    )


def _venue_dict(venue_id, venue_name, street, neighborhood, city=RECIFE) -> dict:
    return {
        "venue_id": venue_id, "venue_name": venue_name, "street": street,
        "neighborhood": neighborhood, "city": city,
    }


# ════════════════════════════════════════════════════════════════════════════
# build_review_evidence — the pure evidence bundle
# ════════════════════════════════════════════════════════════════════════════

def test_build_review_evidence_splits_corroborating_from_non_corroborating():
    """The split is `venue_corroborates_location_text`'s own — the REAL
    function, not a re-implementation — so the model sees exactly the two
    buckets the deterministic audit produced."""
    evidence = build_review_evidence(
        handle=CONCHITTAS_HANDLE,
        venue=_venue_dict(CONCHITTAS_VENUE, CONCHITTAS_NAME, CONCHITTAS_STREET,
                          CONCHITTAS_NEIGHBOURHOOD),
        siblings=[],
        events=[
            {"location_text": CONCHITTAS_POST_TEXT},
            {"location_text": CONCHITTAS_NAME},
        ],
    )
    assert evidence.corroborating == (CONCHITTAS_NAME,), evidence
    assert evidence.non_corroborating == (CONCHITTAS_POST_TEXT,), evidence
    assert evidence.street == CONCHITTAS_STREET
    assert evidence.city == RECIFE


def test_location_texts_is_exactly_corroborating_plus_non_corroborating():
    """The ONLY valid haystack for the verbatim gate — pinned because
    widening it (to the venue record, a sibling's fields, a candidate row's
    stored evidence) is what would let a "remembered" quote through."""
    evidence = build_review_evidence(
        handle=SALA_HANDLE,
        venue=_venue_dict(SALA_VENUE, SALA_NAME, SALA_STREET, SALA_NEIGHBOURHOOD),
        siblings=[],
        events=[
            {"location_text": SALA_CORROBORATING},
            {"location_text": SALA_OFFSITE_A},
            {"location_text": SALA_OFFSITE_B},
        ],
    )
    assert evidence.location_texts == [
        SALA_CORROBORATING, SALA_OFFSITE_A, SALA_OFFSITE_B,
    ]
    assert evidence.location_texts == (
        list(evidence.corroborating) + list(evidence.non_corroborating)
    )
    # The venue's own catalogued street is NOT a haystack entry.
    assert SALA_STREET not in evidence.location_texts


def test_repeated_texts_are_de_duplicated():
    """"the model gains nothing from the same spelling twelve times" — and
    the prompt stays bounded regardless of a handle's volume (3 to 69 events
    measured live)."""
    evidence = build_review_evidence(
        handle=CONCHITTAS_HANDLE,
        venue=_venue_dict(CONCHITTAS_VENUE, CONCHITTAS_NAME, CONCHITTAS_STREET,
                          CONCHITTAS_NEIGHBOURHOOD),
        siblings=[],
        events=[{"location_text": CONCHITTAS_POST_TEXT}] * 20
        + [{"location_text": CONCHITTAS_NAME}] * 8,
    )
    assert evidence.non_corroborating == (CONCHITTAS_POST_TEXT,)
    assert evidence.corroborating == (CONCHITTAS_NAME,)


def test_every_skip_table_row_is_excluded_and_counted_as_protected():
    """Plan Desired Behavior §3: "Refuse to consider any event the skip table
    protects — confirmed, manual_link, superseded, extraction_failed,
    rejected, operator_edited_venue — reusing the repository's single
    existing expression of those rules rather than restating them."

    Each protected row below carries a DISTINCT, decisively non-corroborating
    text, so a leak would be visible in `non_corroborating` rather than hidden
    behind de-duplication."""
    protected_rows = [
        {"location_text": "Torre Malakoff", "status": "confirmed"},
        {"location_text": "Marco Zero", "location_resolution": "manual"},
        {"location_text": "Praia de Boa Viagem", "status": "superseded"},
        {"location_text": "Casa da Cultura", "status": "extraction_failed"},
        {"location_text": "Teatro do Parque", "status": "rejected"},
        {"location_text": "Paço do Frevo", "operator_edited_fields": ["venue_id"]},
    ]
    # Guard: each row really is protected, and by a DIFFERENT rule — if the
    # skip table ever stops recognising one of these shapes this test must
    # fail here, not silently pass on five of six.
    assert [skip_reason(r) for r in protected_rows] == [
        SKIP_CONFIRMED, SKIP_MANUAL_LINK, SKIP_SUPERSEDED,
        SKIP_EXTRACTION_FAILED, SKIP_REJECTED, SKIP_OPERATOR_EDITED_VENUE,
    ]

    evidence = build_review_evidence(
        handle=CONCHITTAS_HANDLE,
        venue=_venue_dict(CONCHITTAS_VENUE, CONCHITTAS_NAME, CONCHITTAS_STREET,
                          CONCHITTAS_NEIGHBOURHOOD),
        siblings=[],
        events=protected_rows + [{"location_text": CONCHITTAS_POST_TEXT}],
    )
    assert evidence.protected_event_count == len(protected_rows), evidence
    assert evidence.non_corroborating == (CONCHITTAS_POST_TEXT,), evidence
    assert evidence.corroborating == (), evidence


def test_an_event_already_resolved_to_a_different_venue_is_a_closed_case():
    """`compute_venue_link_audit`'s own filter, reused: an event another
    mechanism already reattributed is not evidence about THIS mapping either
    way — and is NOT counted as protected, because the skip table never
    reached it."""
    evidence = build_review_evidence(
        handle="beerdock_recife",
        venue=_venue_dict("beerdock_boa_viagem", "BeerDock Boa Viagem",
                          "Av. Eng. Domingos Ferreira", "Boa Viagem"),
        siblings=[],
        events=[
            {"location_text": "CASA FORTE", "venue_id": "beerdock_casa_forte"},
            {"location_text": "CASA FORTE", "venue_id": "beerdock_casa_forte"},
        ],
    )
    assert evidence.non_corroborating == (), evidence
    assert evidence.corroborating == (), evidence
    assert evidence.protected_event_count == 0, evidence


def test_an_unresolved_event_and_one_resolved_to_this_venue_both_count():
    """The regression guard the closed-case filter needs: `venue_id=None`
    and `venue_id == this venue` are both live evidence."""
    evidence = build_review_evidence(
        handle=CONCHITTAS_HANDLE,
        venue=_venue_dict(CONCHITTAS_VENUE, CONCHITTAS_NAME, CONCHITTAS_STREET,
                          CONCHITTAS_NEIGHBOURHOOD),
        siblings=[],
        events=[
            {"location_text": CONCHITTAS_POST_TEXT, "venue_id": None},
            {"location_text": "Marco Zero", "venue_id": CONCHITTAS_VENUE},
        ],
    )
    assert set(evidence.non_corroborating) == {CONCHITTAS_POST_TEXT, "Marco Zero"}


def test_a_text_too_short_to_be_checkable_lands_in_neither_bucket():
    """`venue_corroborates_location_text` returns None for a text that is not
    evidence either way; it must not be silently read as "does not
    corroborate"."""
    evidence = build_review_evidence(
        handle=CONCHITTAS_HANDLE,
        venue=_venue_dict(CONCHITTAS_VENUE, CONCHITTAS_NAME, CONCHITTAS_STREET,
                          CONCHITTAS_NEIGHBOURHOOD),
        siblings=[],
        events=[{"location_text": ""}, {"location_text": None}, {"location_text": "À."}],
    )
    assert evidence.corroborating == ()
    assert evidence.non_corroborating == ()
    assert evidence.protected_event_count == 0


def test_siblings_are_carried_as_labeled_context_only():
    """A double-mapped handle's OTHER venue reaches the prompt as context —
    name/street/neighbourhood only, and never as a haystack entry."""
    evidence = build_review_evidence(
        handle=BODE_HANDLE,
        venue=_venue_dict(*BODE_ESPINHEIRO[:2], BODE_ESPINHEIRO[2], BODE_ESPINHEIRO[3]),
        siblings=[{
            "venue_name": BODE_BOA_VIAGEM[1], "street": BODE_BOA_VIAGEM[2],
            "neighborhood": BODE_BOA_VIAGEM[3],
        }],
        events=[{"location_text": BODE_POST_TEXT}],
    )
    assert evidence.siblings == ({
        "venue_name": BODE_BOA_VIAGEM[1], "street": BODE_BOA_VIAGEM[2],
        "neighborhood": BODE_BOA_VIAGEM[3],
    },)
    assert BODE_BOA_VIAGEM[2] not in evidence.location_texts


def test_no_events_at_all_yields_an_empty_bundle():
    for events in ([], None):
        evidence = build_review_evidence(
            handle=CONCHITTAS_HANDLE,
            venue=_venue_dict(CONCHITTAS_VENUE, CONCHITTAS_NAME, CONCHITTAS_STREET,
                              CONCHITTAS_NEIGHBOURHOOD),
            siblings=None, events=events,
        )
        assert evidence.location_texts == []
        assert evidence.siblings == ()


def test_a_venue_with_no_address_row_degrades_rather_than_failing():
    """Acceptance criterion: "a pair whose venue has no `venues.address` row
    degrades to name+neighborhood rather than failing"."""
    evidence = build_review_evidence(
        handle=CONCHITTAS_HANDLE,
        venue={"venue_id": CONCHITTAS_VENUE, "venue_name": CONCHITTAS_NAME},
        siblings=[], events=[{"location_text": CONCHITTAS_POST_TEXT}],
    )
    assert evidence.street is None
    assert evidence.city is None
    assert evidence.non_corroborating == (CONCHITTAS_POST_TEXT,)


# ════════════════════════════════════════════════════════════════════════════
# tally_consensus — K-of-K unanimity
# ════════════════════════════════════════════════════════════════════════════

def test_the_shipped_consensus_k_is_ten():
    """Load-bearing constant (plan §F / Open Question 2): K=5 had a real
    chance of reading `casabacurau` (measured 9 confirm / 1 insufficient) as
    unanimous and missing a hold-back it should have caught. Lower it only
    with a fresh measurement."""
    assert DEFAULT_REVIEW_CONSENSUS_K == 10


@pytest.mark.parametrize("verdict,outcome", [
    (VERDICT_CONFIRM, OUTCOME_CONFIRMED),
    (VERDICT_CONTRADICT, OUTCOME_CONTRADICTED),
    (VERDICT_INSUFFICIENT, OUTCOME_INSUFFICIENT),
])
def test_ten_identical_answers_reach_their_own_outcome(verdict, outcome):
    assert tally_consensus([verdict] * 10, k=10) == (outcome, verdict)


def test_one_dissent_in_ten_is_no_consensus():
    """The `casabacurau` shape measured live at 9 confirm / 1 insufficient —
    exactly the mixed-evidence pair a human should look at."""
    verdicts = [VERDICT_CONFIRM] * 9 + [VERDICT_INSUFFICIENT]
    assert tally_consensus(verdicts, k=10) == (OUTCOME_NO_CONSENSUS, None)


def test_a_single_contradict_among_confirms_is_no_consensus():
    """`saladerebocorecife`, measured: 7 confirm / 2 insufficient / 1
    contradict. A majority never acts."""
    verdicts = [VERDICT_CONFIRM] * 7 + [VERDICT_INSUFFICIENT] * 2 + [VERDICT_CONTRADICT]
    assert tally_consensus(verdicts, k=10) == (OUTCOME_NO_CONSENSUS, None)


def test_fewer_than_k_accepted_answers_can_never_reach_consensus():
    """LOAD-BEARING — read `tally_consensus`'s docstring before touching it:
    unanimity is required over EXACTLY `k` accepted answers, which is what
    makes "all ten agreeing, all ten validating, all ten succeeding" a single
    check rather than three that could drift apart. Nine unanimous confirms
    out of ten calls is NOT a consensus."""
    assert tally_consensus([VERDICT_CONFIRM] * 9, k=10) == (OUTCOME_NO_CONSENSUS, None)
    assert tally_consensus([VERDICT_CONFIRM], k=10) == (OUTCOME_NO_CONSENSUS, None)


def test_more_than_k_answers_is_also_no_consensus():
    assert tally_consensus([VERDICT_CONFIRM] * 11, k=10) == (OUTCOME_NO_CONSENSUS, None)


def test_an_empty_answer_list_is_no_consensus():
    assert tally_consensus([], k=10) == (OUTCOME_NO_CONSENSUS, None)
    assert tally_consensus(None, k=10) == (OUTCOME_NO_CONSENSUS, None)
    # ...including the degenerate k=0, which must never read as "everyone
    # agreed" on an empty run.
    assert tally_consensus([], k=0) == (OUTCOME_NO_CONSENSUS, None)


# ════════════════════════════════════════════════════════════════════════════
# review_suppresses_pair — the suppression + staleness rule
# ════════════════════════════════════════════════════════════════════════════

def _review(**overrides) -> dict:
    row = {
        "verdict": VERDICT_CONFIRM, "outcome": OUTCOME_CONFIRMED,
        "operator_decision": None, "non_corroborating_count_at_review": 2,
    }
    row.update(overrides)
    return row


def test_a_consensus_confirm_within_its_recorded_evidence_suppresses():
    assert review_suppresses_pair(_review(), non_corroborating_count=2) is True
    assert review_suppresses_pair(_review(), non_corroborating_count=1) is True


def test_a_pair_whose_evidence_grew_re_opens_on_its_own():
    """Plan Desired Behavior §8: new non-corroborating evidence re-opens the
    pair with no operator action and no job."""
    assert review_suppresses_pair(_review(), non_corroborating_count=3) is False


def test_a_never_reviewed_pair_is_not_suppressed():
    assert review_suppresses_pair(None, non_corroborating_count=0) is False
    assert review_suppresses_pair({}, non_corroborating_count=0) is False


@pytest.mark.parametrize("verdict", [VERDICT_CONTRADICT, VERDICT_INSUFFICIENT, None, ""])
def test_only_confirm_closes_a_flag(verdict):
    """Asymmetric by design: a wrong `confirm` HIDES a real defect, a wrong
    `contradict` merely adds noise to a queue an operator already reads."""
    assert review_suppresses_pair(_review(verdict=verdict), non_corroborating_count=0) is False


def test_an_operator_rejection_is_permanent():
    assert review_suppresses_pair(
        _review(operator_decision=DECISION_REJECTED), non_corroborating_count=0,
    ) is False


def test_an_operator_acceptance_still_suppresses():
    assert review_suppresses_pair(
        _review(operator_decision=DECISION_ACCEPTED), non_corroborating_count=2,
    ) is True


def test_shadow_mode_suppresses_nothing():
    """`_persist` stores a NULL `non_corroborating_count_at_review` while
    `venue_link_audit_reviewer_auto_apply_enabled` is off, and that NULL is
    what makes shadow mode genuinely inert rather than merely hidden — a
    recorded `confirm` with no count suppresses nothing at all."""
    assert review_suppresses_pair(
        _review(non_corroborating_count_at_review=None), non_corroborating_count=0,
    ) is False
    assert review_suppresses_pair(
        _review(non_corroborating_count_at_review=None), non_corroborating_count=2,
    ) is False


# ════════════════════════════════════════════════════════════════════════════
# apply_reviews_to_audit — folding stored reviews into the audit's output
# ════════════════════════════════════════════════════════════════════════════

def _audit_candidates():
    """The real `real.botequim` double-mapped shape, straight out of the real
    `compute_venue_link_audit` — frozen dataclasses, exactly what the
    collector hands this function in production."""
    target = {
        "handle": "real.botequim",
        "mapped_venues": [
            {"venue_id": "bar_real_botequim", "venue_name": "Bar Real Botequim",
             "neighborhood": "Casa Forte"},
            {"venue_id": "real_bar_e_lanches", "venue_name": "Real Bar e Lanches",
             "neighborhood": "Pinheiros"},
        ],
    }
    events = [{"location_text": "Real Botequim\nAv. 17 de agosto, 1761 - Casa Forte"}] * 5
    return compute_venue_link_audit([target], {"real.botequim": events})


def test_a_suppressed_flagged_venue_drops_its_whole_handle_when_nothing_survives():
    candidates = _audit_candidates()
    reviews = {("real.botequim", "real_bar_e_lanches"): _review(
        non_corroborating_count_at_review=5,
    )}
    assert apply_reviews_to_audit(candidates, reviews) == []


def test_a_still_flagged_pair_carries_an_additive_review_sub_object():
    candidates = _audit_candidates()
    reviews = {("real.botequim", "real_bar_e_lanches"): _review(
        verdict=VERDICT_CONTRADICT, outcome=OUTCOME_CONTRADICTED,
        evidence_quote="Rua Madre Rosa, 181", matched_field=FIELD_STREET,
        reason="the posts give a different street", updated_at="2026-09-14T12:00:00+00:00",
    )}
    [out] = apply_reviews_to_audit(candidates, reviews)
    flagged = [v for v in out["mapped_venues"] if v["flagged"]]
    assert [v["venue_id"] for v in flagged] == ["real_bar_e_lanches"]
    review = flagged[0]["review"]
    assert review["verdict"] == VERDICT_CONTRADICT
    assert review["outcome"] == OUTCOME_CONTRADICTED
    assert review["evidence_quote"] == "Rua Madre Rosa, 181"
    assert review["matched_field"] == FIELD_STREET
    assert review["reviewed_at"] == "2026-09-14T12:00:00+00:00"
    # Additive ONLY: every pre-existing field is byte-identical.
    original = {v.venue_id: v.to_dict() for c in candidates for v in c.mapped_venues}
    for venue in out["mapped_venues"]:
        assert {k: v for k, v in venue.items() if k != "review"} == original[venue["venue_id"]]


def test_review_is_none_for_a_pair_that_was_never_reviewed():
    """An absent review and a review that decided nothing are different
    states and must stay distinguishable."""
    [out] = apply_reviews_to_audit(_audit_candidates(), {})
    assert all(v["review"] is None for v in out["mapped_venues"])


def test_a_non_flagged_sibling_is_never_dropped_by_a_review():
    """Only a FLAGGED venue can be suppressed — the good sibling stays
    visible in the same record, as the audit's own dataclass promises."""
    candidates = _audit_candidates()
    reviews = {
        ("real.botequim", "bar_real_botequim"): _review(non_corroborating_count_at_review=99),
        ("real.botequim", "real_bar_e_lanches"): _review(verdict=VERDICT_CONTRADICT),
    }
    [out] = apply_reviews_to_audit(candidates, reviews)
    assert {v["venue_id"] for v in out["mapped_venues"]} == {
        "bar_real_botequim", "real_bar_e_lanches",
    }


def test_a_rejected_review_never_suppresses_through_the_audit_fold():
    candidates = _audit_candidates()
    reviews = {("real.botequim", "real_bar_e_lanches"): _review(
        operator_decision=DECISION_REJECTED, non_corroborating_count_at_review=5,
    )}
    [out] = apply_reviews_to_audit(candidates, reviews)
    assert any(v["flagged"] for v in out["mapped_venues"])


def test_apply_reviews_never_mutates_its_dataclass_input():
    """The audit's dataclasses are frozen by design; this pins that the fold
    also leaves the CANDIDATE list and its nested tuples untouched, so a
    caller can render the raw audit and the folded audit side by side."""
    candidates = _audit_candidates()
    before = [c.to_dict() for c in candidates]
    apply_reviews_to_audit(candidates, {
        ("real.botequim", "real_bar_e_lanches"): _review(non_corroborating_count_at_review=5),
    })
    assert [c.to_dict() for c in candidates] == before
    assert all(not hasattr(c, "review") for c in candidates)


def test_apply_reviews_accepts_and_never_mutates_the_to_dict_form():
    dict_candidates = [c.to_dict() for c in _audit_candidates()]
    before = copy.deepcopy(dict_candidates)
    out = apply_reviews_to_audit(dict_candidates, {
        ("real.botequim", "real_bar_e_lanches"): _review(verdict=VERDICT_CONTRADICT),
    })
    assert dict_candidates == before, "the input dicts were mutated in place"
    assert all("review" in v for v in out[0]["mapped_venues"])


def test_apply_reviews_tolerates_an_empty_or_missing_candidate_list():
    assert apply_reviews_to_audit([], {}) == []
    assert apply_reviews_to_audit(None, {}) == []


# ════════════════════════════════════════════════════════════════════════════
# the two admin-config flags
# ════════════════════════════════════════════════════════════════════════════

def test_both_flags_ship_off():
    """Acceptance criterion: with both flags False a full deploy makes zero
    model calls and writes zero rows."""
    assert DEFAULT_VENUE_LINK_AUDIT_REVIEWER_ENABLED is False
    assert DEFAULT_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_ENABLED is False


def test_both_flags_default_off_with_no_redis_and_with_an_empty_mirror():
    for client in (None, fakeredis.FakeRedis(decode_responses=True)):
        assert load_venue_link_audit_reviewer_enabled(client) is False
        assert load_venue_link_audit_reviewer_auto_apply_enabled(client) is False


def test_both_flags_read_a_stored_true():
    client = _redis(enabled=True, auto_apply=True)
    assert load_venue_link_audit_reviewer_enabled(client) is True
    assert load_venue_link_audit_reviewer_auto_apply_enabled(client) is True


@pytest.mark.parametrize("stored", ['"false"', '"true"', '"0"', '1', '"on"'])
def test_a_stored_string_is_never_coerced_into_a_flag(stored):
    """The exact coercion `_load_validated_config` exists to prevent:
    `bool("false")` -> True would silently ENABLE a pass that can suppress an
    audit flag. A value that fails its validator falls back to the shipped
    default, it is never "interpreted"."""
    client = fakeredis.FakeRedis(decode_responses=True)
    client.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY, stored)
    client.set(ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY, stored)
    assert load_venue_link_audit_reviewer_enabled(client) is False
    assert load_venue_link_audit_reviewer_auto_apply_enabled(client) is False


@pytest.mark.parametrize("validator", [
    validate_venue_link_audit_reviewer_enabled_config,
    validate_venue_link_audit_reviewer_auto_apply_config,
])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}, "yes"])
def test_a_non_bool_raises_through_the_validator(validator, value):
    with pytest.raises(TypeError):
        validator(value)


@pytest.mark.parametrize("validator", [
    validate_venue_link_audit_reviewer_enabled_config,
    validate_venue_link_audit_reviewer_auto_apply_config,
])
def test_a_real_bool_passes_the_validator_unchanged(validator):
    assert validator(True) is True
    assert validator(False) is False


# ════════════════════════════════════════════════════════════════════════════
# parse_venue_link_review_response
# ════════════════════════════════════════════════════════════════════════════

def test_plain_json_parses():
    raw = _answer(VERDICT_CONFIRM, quote=CONCHITTAS_POST_TEXT, field=FIELD_STREET)
    assert parse_venue_link_review_response(raw) == {
        "verdict": VERDICT_CONFIRM, "evidence_quote": CONCHITTAS_POST_TEXT,
        "matched_field": FIELD_STREET, "reason": "one short sentence",
    }


def test_a_markdown_fenced_block_is_unwrapped():
    body = _answer(VERDICT_CONFIRM, quote=CONCHITTAS_POST_TEXT, field=FIELD_STREET)
    parsed = parse_venue_link_review_response(f"```\n{body}\n```")
    assert parsed["verdict"] == VERDICT_CONFIRM


def test_a_json_tagged_fence_is_unwrapped():
    body = _answer(VERDICT_CONTRADICT, quote="Rua Madre Rosa, 181", field=FIELD_STREET)
    parsed = parse_venue_link_review_response(f"```json\n{body}\n```")
    assert parsed["verdict"] == VERDICT_CONTRADICT


@pytest.mark.parametrize("raw", ["", None, "   ", "not json at all", "{oops",
                                 '{"verdict": "confirm"'])
def test_an_empty_or_unparseable_reply_is_none(raw):
    """An EMPTY reply returns None here too — the SERVICE distinguishes the
    two cases by checking the raw text itself, so it can count
    `empty_response` separately from `validator_rejected`."""
    assert parse_venue_link_review_response(raw) is None


@pytest.mark.parametrize("raw", ['["confirm"]', '"confirm"', "42", "true", "null"])
def test_valid_json_that_is_not_an_object_is_none(raw):
    assert parse_venue_link_review_response(raw) is None


# ════════════════════════════════════════════════════════════════════════════
# VenueLinkAuditReviewerService — the adapter, end to end
# ════════════════════════════════════════════════════════════════════════════

async def test_the_pass_is_invisible_while_its_flag_is_off():
    """The default-False guarantee: no model call, no row read, no row
    written — the flag decides whether the work runs AT ALL."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([])  # any call at all raises
    result = await _service(store, client, redis_client=_redis()).run()

    assert result == {"outcomes": [], "counts": {}, "enabled": False}
    assert client.calls == []
    assert store.venue_link_audit_reviews == {}


async def test_the_auto_apply_flag_alone_does_not_switch_the_pass_on():
    """The two flags are independent: auto-apply on / reviewer off still runs
    nothing."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([])
    result = await _service(store, client, redis_client=_redis(auto_apply=True)).run()
    assert result["enabled"] is False
    assert client.calls == []


async def test_ten_identical_confirms_confirm_the_pair_and_write_one_row():
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()] * 10)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    assert result["enabled"] is True and result["auto_apply"] is True
    assert result["consensus_k"] == 10
    assert len(client.calls) == 10, "K independent calls, one pair"
    [outcome] = result["outcomes"]
    assert outcome["outcome"] == OUTCOME_CONFIRMED
    assert outcome["verdict"] == VERDICT_CONFIRM
    assert outcome["suppressed"] is True
    assert outcome["tally"] == {VERDICT_CONFIRM: 10}
    assert result["counts"] == {OUTCOME_CONFIRMED: 1}

    assert list(store.venue_link_audit_reviews) == [(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)]
    row = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)
    assert row["outcome"] == OUTCOME_CONFIRMED
    assert row["verdict"] == VERDICT_CONFIRM
    assert row["evidence_quote"] == CONCHITTAS_POST_TEXT
    assert row["matched_field"] == FIELD_STREET
    assert row["model"] == "gpt-5.6-luna"
    assert row["consensus_k"] == 10
    assert row["checkable_count_at_review"] == 3
    # Set BECAUSE auto-apply is on — this is the field that actually closes
    # the flag, via `review_suppresses_pair`.
    assert row["non_corroborating_count_at_review"] == 2
    assert review_suppresses_pair(row, non_corroborating_count=2) is True


async def test_the_model_is_shown_the_venues_real_street(  ):
    """Plan §1/§D — the address is the load-bearing signal: arm A (name +
    neighbourhood) returned a confident, unanimous, WRONG verdict on
    `lacasarecife`; arm B (+ street + city) flipped it. The street must
    actually reach the evidence bundle."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()] * 10)
    await _service(store, client, redis_client=_redis(enabled=True, auto_apply=True)).run()

    evidence = client.calls[0]
    assert evidence.venue_name == CONCHITTAS_NAME
    assert evidence.street == CONCHITTAS_STREET
    assert evidence.neighborhood == CONCHITTAS_NEIGHBOURHOOD
    assert evidence.city == RECIFE
    assert evidence.non_corroborating == (CONCHITTAS_POST_TEXT,)
    assert evidence.siblings == ()


async def test_shadow_mode_records_the_verdict_but_suppresses_nothing():
    """auto-apply OFF: the pass still runs and still records a full,
    inspectable verdict, but stores no `non_corroborating_count_at_review` —
    so `review_suppresses_pair` is False and the pair stays flagged."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()] * 10)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=False),
    ).run()

    assert result["auto_apply"] is False
    assert result["outcomes"][0]["outcome"] == OUTCOME_CONFIRMED
    row = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)
    assert row["verdict"] == VERDICT_CONFIRM
    assert row["non_corroborating_count_at_review"] is None
    assert review_suppresses_pair(row, non_corroborating_count=2) is False
    # ...and the pair is therefore still in the audit's flagged output.
    candidates = compute_venue_link_audit(
        [{"handle": CONCHITTAS_HANDLE, "mapped_venues": [{
            "venue_id": CONCHITTAS_VENUE, "venue_name": CONCHITTAS_NAME,
            "neighborhood": CONCHITTAS_NEIGHBOURHOOD,
        }]}],
        {CONCHITTAS_HANDLE: store.list_events_by_handle(CONCHITTAS_HANDLE)},
    )
    folded = apply_reviews_to_audit(
        candidates, {(CONCHITTAS_HANDLE, CONCHITTAS_VENUE): row},
    )
    assert [c["handle"] for c in folded] == [CONCHITTAS_HANDLE]


async def test_a_dry_run_makes_every_model_call_and_writes_nothing():
    """"the first production run is a measurement an operator reads before
    anything is ever suppressed"."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()] * 10)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run(dry_run=True)

    assert result["dry_run"] is True
    assert len(client.calls) == 10, "a dry run still spends the model budget"
    assert result["outcomes"][0]["outcome"] == OUTCOME_CONFIRMED
    assert store.venue_link_audit_reviews == {}, "a dry run writes NOTHING"


async def test_mixed_verdicts_are_recorded_as_no_consensus_and_stay_flagged():
    """The real `saladerebocorecife` distribution (plan §F): 7 confirm /
    2 insufficient / 1 contradict — recorded, never acted on."""
    store = _seed_sala(InMemoryRdsVenueStore())
    responses = (
        [_answer(VERDICT_CONFIRM, quote=SALA_CORROBORATING, field=FIELD_STREET)] * 7
        + [_answer(VERDICT_INSUFFICIENT)] * 2
        + [_answer(VERDICT_CONTRADICT, quote=SALA_OFFSITE_A, field=FIELD_NEIGHBORHOOD)]
    )
    client = _FakeReviewClient(responses)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    [outcome] = result["outcomes"]
    assert outcome["outcome"] == OUTCOME_NO_CONSENSUS
    assert outcome["verdict"] is None
    assert outcome["suppressed"] is False
    assert outcome["tally"] == {VERDICT_CONFIRM: 7, VERDICT_INSUFFICIENT: 2,
                                VERDICT_CONTRADICT: 1}
    # Still WRITTEN: "Every pair the pass looked at has a review row,
    # including skipped and no-consensus ones."
    row = store.get_venue_link_audit_review(SALA_HANDLE, SALA_VENUE)
    assert row["outcome"] == OUTCOME_NO_CONSENSUS
    assert row["verdict"] is None
    assert review_suppresses_pair(row, non_corroborating_count=2) is False


async def test_a_unanimous_contradict_is_recorded_and_never_suppresses():
    """The `botecobeer.jsp` shape: the posts give a DIFFERENT street. Plan
    §E — a real defect that needs a catalogue ADD, so it must stay flagged
    and annotated, never silently closed and never force-fitted."""
    store = InMemoryRdsVenueStore()
    _seed_venue(store, venue_id="recife_beer", venue_name="Recife Beer Cervejaria",
                street="R. Abdon Batista, 300", neighborhood="Santo Amaro",
                handle="botecobeer.jsp")
    for i in range(4):
        _seed_event(store, handle="botecobeer.jsp", venue_id="recife_beer", index=i,
                    location_text="Rua Madre Rosa, 181 – Jd. São Paulo")
    client = _FakeReviewClient([
        _answer(VERDICT_CONTRADICT, quote="Rua Madre Rosa, 181", field=FIELD_STREET,
                reason="the posts give a different street"),
    ] * 10)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    [outcome] = result["outcomes"]
    assert outcome["outcome"] == OUTCOME_CONTRADICTED
    assert outcome["verdict"] == VERDICT_CONTRADICT
    assert outcome["suppressed"] is False
    row = store.get_venue_link_audit_review("botecobeer.jsp", "recife_beer")
    assert row["reason"] == "the posts give a different street"
    assert review_suppresses_pair(row, non_corroborating_count=4) is False


async def test_one_empty_response_is_its_own_outcome_not_a_validator_rejection():
    """Plan §F's confound, pinned so it cannot recur: a reasoning model whose
    `max_completion_tokens` is too small returns an EMPTY body, which parses
    to None and reads downstream as a malformed answer. Folding that into
    `validator_rejected` hid a real configuration bug in the existing advisor
    for a full cycle — "raise the token budget" and "the prompt needs work"
    are different actions and must never share an outcome."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()] * 9 + [""])
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    [outcome] = result["outcomes"]
    assert outcome["outcome"] == OUTCOME_EMPTY_RESPONSE
    assert outcome["outcome"] != OUTCOME_VALIDATOR_REJECTED
    assert outcome["verdict"] is None
    # The tally still sums to K, so a reader can tell "nine confirms and one
    # empty" from "nine confirms and one insufficient".
    assert outcome["tally"] == {VERDICT_CONFIRM: 9, OUTCOME_EMPTY_RESPONSE: 1}
    assert sum(outcome["tally"].values()) == 10
    assert result["counts"] == {OUTCOME_EMPTY_RESPONSE: 1}


async def test_one_non_verbatim_quote_is_a_validator_rejection():
    """The verbatim gate, end to end: a quote the model "remembered" (here,
    the CATALOG spelling, which was never supplied as a location text) costs
    the whole pair its consensus."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    invented = _answer(VERDICT_CONFIRM, quote=CONCHITTAS_STREET, field=FIELD_STREET)
    client = _FakeReviewClient([_confirm_conchittas()] * 9 + [invented])
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    [outcome] = result["outcomes"]
    assert outcome["outcome"] == OUTCOME_VALIDATOR_REJECTED
    assert outcome["tally"] == {VERDICT_CONFIRM: 9, OUTCOME_VALIDATOR_REJECTED: 1}
    row = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)
    assert row["outcome"] == OUTCOME_VALIDATOR_REJECTED
    assert row["verdict"] is None


async def test_a_raising_client_is_counted_as_an_error_and_never_propagates():
    """The service's degrade-gracefully contract: an OpenAI failure on one
    pair is caught, counted and logged, and leaves that pair exactly as the
    audit produced it."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient(
        [_confirm_conchittas()] * 9 + [RuntimeError("openai: 503 service unavailable")],
    )
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    [outcome] = result["outcomes"]
    assert outcome["outcome"] == OUTCOME_ERROR
    assert outcome["tally"] == {VERDICT_CONFIRM: 9, OUTCOME_ERROR: 1}
    assert result["counts"] == {OUTCOME_ERROR: 1}


async def test_every_call_failing_is_still_an_error_rather_than_a_raise():
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([RuntimeError("openai down")] * 10)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()
    assert result["outcomes"][0]["outcome"] == OUTCOME_ERROR
    assert result["outcomes"][0]["tally"] == {OUTCOME_ERROR: 10}


async def test_a_double_mapped_handle_is_skipped_for_both_venues_with_no_model_call():
    """Plan §F item 3 — the STRUCTURAL gate. Both `entreamigosobode` siblings
    returned `confirm` 10/10 on a shared pool of 12 events that cannot belong
    to both, one citing house number 30 for a venue catalogued at 50.
    Sampling cannot detect that; only this rule can — so no model call is
    made at all."""
    store = _seed_bode(InMemoryRdsVenueStore())
    client = _FakeReviewClient([])  # any call at all raises
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    assert client.calls == []
    assert result["counts"] == {OUTCOME_SKIPPED_MULTI_MAPPED: 2}
    assert {o["venue_id"] for o in result["outcomes"]} == {
        BODE_ESPINHEIRO[0], BODE_BOA_VIAGEM[0],
    }
    assert all(o["suppressed"] is False for o in result["outcomes"])
    # Both pairs still get a row — an operator sees what the machine
    # considered and declined, not only what it acted on.
    assert set(store.venue_link_audit_reviews) == {
        (BODE_HANDLE, BODE_ESPINHEIRO[0]), (BODE_HANDLE, BODE_BOA_VIAGEM[0]),
    }
    for row in store.venue_link_audit_reviews.values():
        assert row["outcome"] == OUTCOME_SKIPPED_MULTI_MAPPED
        assert row["verdict"] is None


async def test_a_pair_an_operator_already_decided_is_skipped_with_no_model_call():
    """An operator's own decision outranks everything and is permanent —
    never re-reviewed, never re-spent, never overwritten."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    store.upsert_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE, {
        "outcome": OUTCOME_CONTRADICTED, "verdict": VERDICT_CONTRADICT,
        "reason": "operator looked at this already",
    })
    store.set_venue_link_audit_review_decision(
        CONCHITTAS_HANDLE, CONCHITTAS_VENUE, DECISION_REJECTED, "not our venue",
    )
    before = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)

    client = _FakeReviewClient([])  # any call at all raises
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    assert client.calls == []
    assert result["counts"] == {OUTCOME_SKIPPED_OPERATOR_DECIDED: 1}
    after = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)
    assert after == before, "an operator's decision was overwritten by a re-run"


async def test_a_second_run_over_unchanged_data_costs_nothing():
    """Idempotency, and the whole point of persisting the count: once a pair
    is confirmed and its evidence has not grown, the second run reaches the
    same verdict from the stored row and spends ZERO model calls."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    redis_client = _redis(enabled=True, auto_apply=True)

    first = await _service(
        store, _FakeReviewClient([_confirm_conchittas()] * 10), redis_client=redis_client,
    ).run()
    assert first["outcomes"][0]["outcome"] == OUTCOME_CONFIRMED
    row_after_first = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)

    second_client = _FakeReviewClient([])  # any call at all raises
    second = await _service(store, second_client, redis_client=redis_client).run()

    assert second_client.calls == []
    [outcome] = second["outcomes"]
    assert outcome["outcome"] == OUTCOME_CONFIRMED
    assert outcome["verdict"] == VERDICT_CONFIRM
    assert outcome["suppressed"] is True
    assert outcome["evidence_quote"] == CONCHITTAS_POST_TEXT
    assert len(store.venue_link_audit_reviews) == 1
    assert store.get_venue_link_audit_review(
        CONCHITTAS_HANDLE, CONCHITTAS_VENUE,
    ) == row_after_first, "an unchanged pair was needlessly re-written"


async def test_a_confirmed_pair_re_opens_and_is_re_reviewed_when_evidence_grows():
    """The staleness rule reaching the pass itself: new non-corroborating
    evidence makes the stored review stop suppressing, so the pair is
    genuinely re-reviewed (and re-spent) rather than skipped."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    redis_client = _redis(enabled=True, auto_apply=True)
    await _service(
        store, _FakeReviewClient([_confirm_conchittas()] * 10), redis_client=redis_client,
    ).run()

    # A new, genuinely different off-site post arrives.
    _seed_event(store, handle=CONCHITTAS_HANDLE, venue_id=CONCHITTAS_VENUE, index=99,
                location_text=SALA_OFFSITE_A)

    second_client = _FakeReviewClient([_confirm_conchittas()] * 10)
    second = await _service(store, second_client, redis_client=redis_client).run()

    assert len(second_client.calls) == 10, "a re-opened pair must be re-reviewed"
    row = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)
    assert row["non_corroborating_count_at_review"] == 3
    assert second["outcomes"][0]["non_corroborating_count"] == 3


async def test_the_handle_filter_scopes_the_run_to_one_pair():
    store = _seed_sala(_seed_conchittas(InMemoryRdsVenueStore()))
    client = _FakeReviewClient([_confirm_conchittas()] * 10)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run(handles=[CONCHITTAS_HANDLE])

    assert [o["handle"] for o in result["outcomes"]] == [CONCHITTAS_HANDLE]
    assert list(store.venue_link_audit_reviews) == [(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)]


async def test_every_pair_the_pass_looked_at_gets_exactly_one_review_row():
    """Acceptance criterion, across two independent flagged handles at once.
    `insufficient` is used deliberately: it is the one verdict allowed to
    carry no quote, so this test turns on the row-per-pair accounting rather
    than on either handle's evidence."""
    store = _seed_sala(_seed_conchittas(InMemoryRdsVenueStore()))
    client = _FakeReviewClient([_answer(VERDICT_INSUFFICIENT)] * 20)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    assert len(client.calls) == 20, "K calls per pair, two pairs"
    assert result["counts"] == {OUTCOME_INSUFFICIENT: 2}
    assert len(result["outcomes"]) == 2
    assert set(store.venue_link_audit_reviews) == {
        (CONCHITTAS_HANDLE, CONCHITTAS_VENUE), (SALA_HANDLE, SALA_VENUE),
    }
    # An `insufficient` consensus is still not an action.
    for row in store.venue_link_audit_reviews.values():
        assert row["outcome"] == OUTCOME_INSUFFICIENT
        assert row["verdict"] == VERDICT_INSUFFICIENT
        assert review_suppresses_pair(row, non_corroborating_count=0) is False


async def test_a_lower_consensus_k_is_honoured_end_to_end():
    """K is a constructor argument the runner exposes as `--consensus-k`;
    the call count and the stored `consensus_k` must both follow it."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()] * 3)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True), consensus_k=3,
    ).run()

    assert len(client.calls) == 3
    assert result["consensus_k"] == 3
    assert result["outcomes"][0]["outcome"] == OUTCOME_CONFIRMED
    assert store.get_venue_link_audit_review(
        CONCHITTAS_HANDLE, CONCHITTAS_VENUE,
    )["consensus_k"] == 3


async def test_a_handle_with_no_flagged_pair_is_never_reviewed():
    """The input is the AUDIT's flagged pairs, so a correctly-mapped handle
    costs nothing at all."""
    store = InMemoryRdsVenueStore()
    _seed_venue(store, venue_id="theater", venue_name="Theater Luís Mendonça",
                street="Av. Boa Viagem, S/N", neighborhood="Boa Viagem",
                handle="teatroluizmendonca")
    for i in range(5):
        _seed_event(store, handle="teatroluizmendonca", venue_id="theater", index=i,
                    location_text="Teatro Luiz Mendonça")

    client = _FakeReviewClient([])
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
    ).run()

    assert result["outcomes"] == []
    assert client.calls == []
    assert store.venue_link_audit_reviews == {}


async def test_the_pass_never_writes_an_event_row():
    """Plan Non-goals / acceptance criterion: "No event row is modified by
    any code path in this plan"."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    before = copy.deepcopy(store.events)
    client = _FakeReviewClient([_confirm_conchittas()] * 10)
    await _service(store, client, redis_client=_redis(enabled=True, auto_apply=True)).run()
    assert store.events == before


# ── the consensus-K floor (plans/260914_agentic-venue-resolution-fallback.md) ──
# The plan's central instruction is that this feature must not ship a
# single-call auto-apply. `consensus_k` is a constructor argument the runner
# exposes as `--consensus-k`, so without a floor one CLI flag would reduce the
# whole consensus design to the single unstable call it exists to prevent.


def test_min_review_consensus_k_is_the_smallest_measured_k():
    """K=5 is the smallest K plan §F actually exercised against production;
    at K=5 all three mixed-evidence pairs were still held back."""
    assert MIN_REVIEW_CONSENSUS_K == 5
    assert DEFAULT_REVIEW_CONSENSUS_K >= MIN_REVIEW_CONSENSUS_K


async def test_a_consensus_k_below_the_floor_records_but_never_suppresses():
    """A unanimous confirm at K=1 must still leave the pair flagged: the row
    is written, but with no `non_corroborating_count_at_review`, so
    `review_suppresses_pair` refuses to suppress on it."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()])
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
        consensus_k=1,
    ).run()

    # The flag said auto-apply; the floor overrode it.
    assert result["auto_apply"] is False, result
    row = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)
    assert row is not None, "the verdict must still be RECORDED"
    assert row["verdict"] == VERDICT_CONFIRM, row
    assert row["non_corroborating_count_at_review"] is None, row
    assert review_suppresses_pair(row, non_corroborating_count=0) is False


async def test_at_or_above_the_floor_a_unanimous_confirm_does_suppress():
    """The same path at K=5 suppresses — proving the floor is what blocked it
    above, not something else about a small run."""
    store = _seed_conchittas(InMemoryRdsVenueStore())
    client = _FakeReviewClient([_confirm_conchittas()] * 5)
    result = await _service(
        store, client, redis_client=_redis(enabled=True, auto_apply=True),
        consensus_k=5,
    ).run()

    assert result["auto_apply"] is True, result
    row = store.get_venue_link_audit_review(CONCHITTAS_HANDLE, CONCHITTAS_VENUE)
    assert row["non_corroborating_count_at_review"] is not None, row
    assert review_suppresses_pair(
        row, non_corroborating_count=row["non_corroborating_count_at_review"],
    ) is True
