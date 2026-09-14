"""Unit tests for the `events.venue_link_audit_review` DAO surface —
plans/260914_agentic-venue-resolution-fallback.md §5, migration 0048.

Driven against `tests.rds_fake.InMemoryRdsVenueStore` (no database), plus a
pass through the real `app.dao.venue_repository.VenueRepository` boundary,
which is what production actually calls.

## Why the parity assertions below are not ceremony

`app/dao/rds_venue_store.py`'s own comments record the trap this file exists
to catch: the fake has three times modelled the HAPPY PATH instead of the
CONSTRAINT, and let a write the real store rejects (or silently no-ops) pass
every offline scenario — `upsert_crawl_target`'s 2026-08-09 production
incident and `update_event`'s `_EVENT_COLUMNS` allowlist are the two named
precedents. So the five behaviours the real store's docstrings single out are
pinned here explicitly:

1. the returned dict's KEY SET (it is the contract, hence no `SELECT *`),
2. `list_...`'s TOTAL ordering (`updated_at DESC, handle, venue_id`),
3. the tri-state `decided` filter on the OPERATOR column,
4. the `operator_*` columns surviving a machine re-review,
5. `set_..._decision` returning None — and writing nothing — for a row that
   does not exist.

Two of them are additionally checked against the REAL store's own module
constants, which needs no database at all and fails the moment a migration
widens one side without the other.
"""
from __future__ import annotations

import pytest

from app.dao.rds_venue_store import (
    _VENUE_LINK_AUDIT_REVIEW_COLUMNS,
    _VENUE_LINK_AUDIT_REVIEW_JSONB_COLUMNS,
    _VENUE_LINK_AUDIT_REVIEW_OPERATOR_COLUMNS,
    _VENUE_LINK_AUDIT_REVIEW_SELECT,
)
from app.dao.venue_repository import VenueRepository
from app.services.venue_link_audit_reviewer import (
    DECISION_ACCEPTED,
    DECISION_REJECTED,
    OUTCOME_CONFIRMED,
    OUTCOME_CONTRADICTED,
    OUTCOME_NO_CONSENSUS,
)
from app.services.venue_link_audit_reviewer_validator import (
    FIELD_STREET,
    VERDICT_CONFIRM,
    VERDICT_CONTRADICT,
)
from tests.rds_fake import InMemoryRdsVenueStore

# Real, re-verified production pairs (plan §A/§E).
CONCHITTAS = ("conchittasbar", "conchittas_bar")
BOTECO = ("botecobeer.jsp", "recife_beer_cervejaria")
LACASA = ("lacasarecife", "la_casa_recife")

# The column list the real store SELECTs, parsed out of its own constant —
# this IS the returned dict's contract (see that constant's comment: "a
# future migration adding a column must change this line (and the fake)
# deliberately rather than widen the shape silently").
_REAL_SELECT_COLUMNS = tuple(
    c.strip()
    for c in _VENUE_LINK_AUDIT_REVIEW_SELECT.split("SELECT ", 1)[1]
    .split(" FROM ")[0]
    .split(",")
)


def _store() -> InMemoryRdsVenueStore:
    return InMemoryRdsVenueStore()


def _machine_fields(**overrides) -> dict:
    fields = {
        "outcome": OUTCOME_CONFIRMED,
        "verdict": VERDICT_CONFIRM,
        "evidence_quote": "Rua da Imperatriz Tereza Cristina, 218",
        "matched_field": FIELD_STREET,
        "reason": "the post gives the same street and number",
        "model": "gpt-5.6-luna",
        "consensus_k": 10,
        "tally": {VERDICT_CONFIRM: 10},
        "checkable_count_at_review": 28,
        "non_corroborating_count_at_review": 20,
    }
    fields.update(overrides)
    return fields


# ── upsert → get ────────────────────────────────────────────────────────────

def test_an_upserted_review_reads_back_field_for_field():
    store = _store()
    written = store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    read = store.get_venue_link_audit_review(*CONCHITTAS)

    assert read == written
    assert (read["handle"], read["venue_id"]) == CONCHITTAS
    for key, value in _machine_fields().items():
        assert read[key] == value, key
    # NULL until a human actually decides — never invented.
    assert read["operator_decision"] is None
    assert read["operator_decided_at"] is None
    assert read["operator_note"] is None
    assert read["created_at"] and read["updated_at"]


def test_a_review_for_a_pair_that_was_never_reviewed_is_none():
    """`None` simply means "still flagged" — it is the read the collector's
    suppression check makes, one pair at a time."""
    assert _store().get_venue_link_audit_review(*CONCHITTAS) is None


def test_outcome_is_required_on_every_upsert_including_a_re_review():
    """Postgres validates NOT NULL against the FULLY CONSTRUCTED insert tuple
    before it ever evaluates `ON CONFLICT`, so an `outcome`-less partial
    upsert raises even against a row that certainly exists — the identical
    trap `upsert_crawl_target` documents from a 2026-08-09 production
    incident. Enforced in Python so it fails the same way offline."""
    store = _store()
    with pytest.raises(ValueError):
        store.upsert_venue_link_audit_review(*CONCHITTAS, {"verdict": VERDICT_CONFIRM})

    store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    with pytest.raises(ValueError):
        store.upsert_venue_link_audit_review(*CONCHITTAS, {"verdict": VERDICT_CONFIRM})
    with pytest.raises(ValueError):
        store.upsert_venue_link_audit_review(*CONCHITTAS, {"outcome": None})


def test_a_re_review_overwrites_the_machine_half_in_place():
    store = _store()
    first = store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    updated = store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields(
        outcome=OUTCOME_NO_CONSENSUS, verdict=None,
        tally={VERDICT_CONFIRM: 7, VERDICT_CONTRADICT: 3},
        non_corroborating_count_at_review=None,
    ))
    assert len(store.venue_link_audit_reviews) == 1, "the PK is the (handle, venue) PAIR"
    assert updated["outcome"] == OUTCOME_NO_CONSENSUS
    assert updated["verdict"] is None
    assert updated["tally"] == {VERDICT_CONFIRM: 7, VERDICT_CONTRADICT: 3}
    assert updated["non_corroborating_count_at_review"] is None
    # `created_at` survives the re-review; `updated_at` is re-stamped.
    assert updated["created_at"] == first["created_at"]
    assert updated["updated_at"] != first["updated_at"]


def test_only_the_keys_present_in_fields_are_written():
    store = _store()
    store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    store.upsert_venue_link_audit_review(*CONCHITTAS, {"outcome": OUTCOME_CONTRADICTED})
    row = store.get_venue_link_audit_review(*CONCHITTAS)
    assert row["outcome"] == OUTCOME_CONTRADICTED
    # Untouched by the partial re-upsert, mirroring the real DO UPDATE SET
    # list, which only names the columns actually supplied.
    assert row["model"] == "gpt-5.6-luna"
    assert row["consensus_k"] == 10


def test_an_unknown_field_never_reaches_the_row():
    """The allowlist, not a bare dict-update: a `fields` key the table has no
    column for is dropped, exactly like the real store's
    `[c for c in _VENUE_LINK_AUDIT_REVIEW_COLUMNS if c in fields]`."""
    store = _store()
    row = store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields(
        venue_name="Conchittas Bar", suppressed=True,
    ))
    assert "venue_name" not in row
    assert "suppressed" not in row


def test_the_returned_row_is_a_copy_the_caller_cannot_write_through():
    store = _store()
    row = store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    row["tally"]["confirm"] = 999
    row["outcome"] = "tampered"
    assert store.get_venue_link_audit_review(*CONCHITTAS)["tally"] == {VERDICT_CONFIRM: 10}
    assert store.get_venue_link_audit_review(*CONCHITTAS)["outcome"] == OUTCOME_CONFIRMED


# ── list ────────────────────────────────────────────────────────────────────

def _seed_three(store):
    store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    store.upsert_venue_link_audit_review(*BOTECO, _machine_fields(
        outcome=OUTCOME_CONTRADICTED, verdict=VERDICT_CONTRADICT,
        evidence_quote="Rua Madre Rosa, 181",
    ))
    store.upsert_venue_link_audit_review(*LACASA, _machine_fields())
    return store


def test_list_returns_every_review_with_no_filter():
    store = _seed_three(_store())
    rows = store.list_venue_link_audit_reviews()
    assert {(r["handle"], r["venue_id"]) for r in rows} == {CONCHITTAS, BOTECO, LACASA}


def test_list_filters_by_verdict():
    store = _seed_three(_store())
    contradicted = store.list_venue_link_audit_reviews(verdict=VERDICT_CONTRADICT)
    assert [(r["handle"], r["venue_id"]) for r in contradicted] == [BOTECO]
    confirmed = store.list_venue_link_audit_reviews(verdict=VERDICT_CONFIRM)
    assert {(r["handle"], r["venue_id"]) for r in confirmed} == {CONCHITTAS, LACASA}
    assert store.list_venue_link_audit_reviews(verdict="insufficient") == []


def test_the_decided_filter_is_tri_state_on_the_operator_column():
    """`False` is the useful one — it is the operator's actual queue."""
    store = _seed_three(_store())
    store.set_venue_link_audit_review_decision(*BOTECO, DECISION_ACCEPTED, "real defect")

    decided = store.list_venue_link_audit_reviews(decided=True)
    assert [(r["handle"], r["venue_id"]) for r in decided] == [BOTECO]

    awaiting = store.list_venue_link_audit_reviews(decided=False)
    assert {(r["handle"], r["venue_id"]) for r in awaiting} == {CONCHITTAS, LACASA}

    assert len(store.list_venue_link_audit_reviews(decided=None)) == 3


def test_the_verdict_and_decided_filters_combine():
    store = _seed_three(_store())
    store.set_venue_link_audit_review_decision(*CONCHITTAS, DECISION_ACCEPTED, None)
    rows = store.list_venue_link_audit_reviews(verdict=VERDICT_CONFIRM, decided=True)
    assert [(r["handle"], r["venue_id"]) for r in rows] == [CONCHITTAS]


def test_list_is_newest_reviewed_first():
    store = _store()
    store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    store.upsert_venue_link_audit_review(*BOTECO, _machine_fields())
    store.upsert_venue_link_audit_review(*LACASA, _machine_fields())
    rows = store.list_venue_link_audit_reviews()
    assert [(r["handle"], r["venue_id"]) for r in rows] == [LACASA, BOTECO, CONCHITTAS]


def test_reviews_sharing_a_timestamp_are_ordered_by_their_primary_key():
    """`ORDER BY updated_at DESC, handle, venue_id` is a TOTAL order. Without
    the tiebreak, two rows written in the same instant would come back in
    whatever order Postgres felt like and in insertion order from the fake —
    exactly the fake/real drift this file's parity contract exists to
    prevent. The stored timestamps are flattened by hand here because that
    collision is the only way to observe the tiebreak at all."""
    store = _seed_three(_store())
    for row in store.venue_link_audit_reviews.values():
        row["updated_at"] = "2026-09-14T12:00:00+00:00"
    rows = store.list_venue_link_audit_reviews()
    assert [(r["handle"], r["venue_id"]) for r in rows] == sorted([CONCHITTAS, BOTECO, LACASA])


def test_an_empty_table_lists_empty():
    assert _store().list_venue_link_audit_reviews() == []
    assert _store().list_venue_link_audit_reviews(verdict=VERDICT_CONFIRM, decided=False) == []


# ── the operator decision ───────────────────────────────────────────────────

def test_setting_a_decision_records_the_operators_half():
    store = _seed_three(_store())
    before = store.get_venue_link_audit_review(*BOTECO)
    decided = store.set_venue_link_audit_review_decision(
        *BOTECO, DECISION_REJECTED, "the real venue is not in the catalog yet",
    )
    assert decided["operator_decision"] == DECISION_REJECTED
    assert decided["operator_note"] == "the real venue is not in the catalog yet"
    assert decided["operator_decided_at"]
    # The MACHINE's own timestamp is deliberately untouched: bumping it would
    # make an operator merely reading through the queue reshuffle it under
    # themselves.
    assert decided["updated_at"] == before["updated_at"]
    assert decided["outcome"] == before["outcome"]


def test_a_decision_on_a_pair_with_no_review_row_writes_nothing():
    """There is nothing to decide about a pair no review row exists for, so
    a missing row returns None rather than inventing a decision with no
    verdict attached to it."""
    store = _store()
    assert store.set_venue_link_audit_review_decision(
        *CONCHITTAS, DECISION_ACCEPTED, "note",
    ) is None
    assert store.venue_link_audit_reviews == {}


def test_a_decision_note_is_optional():
    store = _seed_three(_store())
    decided = store.set_venue_link_audit_review_decision(*LACASA, DECISION_ACCEPTED)
    assert decided["operator_decision"] == DECISION_ACCEPTED
    assert decided["operator_note"] is None


def test_a_machine_re_review_never_clobbers_an_operator_decision():
    """Plan §6 makes a rejection PERMANENT for a pair ("never re-reviewed and
    never suppresses anything"), which a re-review that silently reset it
    would destroy. The `operator_*` columns are absent from the write
    allowlist entirely, so no `fields` dict can reach them either."""
    store = _seed_three(_store())
    store.set_venue_link_audit_review_decision(*CONCHITTAS, DECISION_REJECTED, "wrong")
    decided = store.get_venue_link_audit_review(*CONCHITTAS)

    store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields(
        outcome=OUTCOME_CONFIRMED, verdict=VERDICT_CONFIRM,
        # A caller trying to write them through the machine path anyway.
        operator_decision=DECISION_ACCEPTED,
        operator_decided_at="2030-01-01T00:00:00+00:00",
        operator_note="overwritten by the machine",
    ))
    after = store.get_venue_link_audit_review(*CONCHITTAS)
    assert after["operator_decision"] == DECISION_REJECTED
    assert after["operator_decided_at"] == decided["operator_decided_at"]
    assert after["operator_note"] == "wrong"
    # ...while the machine half genuinely did change.
    assert after["outcome"] == OUTCOME_CONFIRMED


# ── the shape contract, and fake/real parity ────────────────────────────────

def test_upsert_get_and_list_all_return_the_same_key_set():
    store = _seed_three(_store())
    written = store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    read = store.get_venue_link_audit_review(*CONCHITTAS)
    [listed] = [
        r for r in store.list_venue_link_audit_reviews()
        if (r["handle"], r["venue_id"]) == CONCHITTAS
    ]
    assert set(written) == set(read) == set(listed)


def test_the_fakes_key_set_is_exactly_the_real_stores_select_list():
    """No database needed: the real store names every column explicitly
    rather than using `SELECT *` precisely so this comparison is possible."""
    store = _store()
    row = store.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    assert set(row) == set(_REAL_SELECT_COLUMNS)
    assert set(store.list_venue_link_audit_reviews()[0]) == set(_REAL_SELECT_COLUMNS)


def test_the_fakes_write_allowlists_match_the_real_stores():
    """The drift guard that matters most: if a migration adds a machine
    column to one side only, the fake would accept a write the real store
    silently drops (or vice versa)."""
    assert (
        InMemoryRdsVenueStore._VENUE_LINK_AUDIT_REVIEW_MACHINE_COLUMNS
        == _VENUE_LINK_AUDIT_REVIEW_COLUMNS
    )
    assert (
        InMemoryRdsVenueStore._VENUE_LINK_AUDIT_REVIEW_OPERATOR_COLUMNS
        == _VENUE_LINK_AUDIT_REVIEW_OPERATOR_COLUMNS
    )


def test_the_write_allowlists_are_disjoint_and_cover_the_select_list():
    """`handle`/`venue_id` are the primary key (arguments, never `fields`) and
    `created_at`/`updated_at` are database-managed — the three deliberate
    absences the real store's comment names."""
    machine = set(_VENUE_LINK_AUDIT_REVIEW_COLUMNS)
    operator = set(_VENUE_LINK_AUDIT_REVIEW_OPERATOR_COLUMNS)
    assert machine & operator == set()
    assert machine | operator | {
        "handle", "venue_id", "created_at", "updated_at",
    } == set(_REAL_SELECT_COLUMNS)
    assert set(_VENUE_LINK_AUDIT_REVIEW_JSONB_COLUMNS) <= machine


# ── through the real VenueRepository boundary ───────────────────────────────

def _repo():
    store = _store()
    return VenueRepository(client=None, rds_store=store), store


@pytest.mark.parametrize("method", [
    "upsert_venue_link_audit_review",
    "get_venue_link_audit_review",
    "list_venue_link_audit_reviews",
    "set_venue_link_audit_review_decision",
])
def test_the_repository_exposes_every_review_method(method):
    """`collect_venue_link_audit` and the reviewer service are both called in
    production with a `VenueRepository`, never the raw store — a missing
    forwarder is invisible to every test that drives the store directly."""
    repo, _ = _repo()
    assert callable(getattr(repo, method))


def test_the_repository_forwards_the_upsert_and_the_read():
    repo, store = _repo()
    written = repo.upsert_venue_link_audit_review(*CONCHITTAS, _machine_fields())
    assert written["outcome"] == OUTCOME_CONFIRMED
    assert repo.get_venue_link_audit_review(*CONCHITTAS) == written
    assert store.get_venue_link_audit_review(*CONCHITTAS) == written
    assert repo.get_venue_link_audit_review(*BOTECO) is None


def test_the_repository_forwards_both_list_filters_as_keywords():
    repo, store = _repo()
    _seed_three(store)
    store.set_venue_link_audit_review_decision(*BOTECO, DECISION_ACCEPTED, None)

    assert len(repo.list_venue_link_audit_reviews()) == 3
    assert [
        (r["handle"], r["venue_id"])
        for r in repo.list_venue_link_audit_reviews(verdict=VERDICT_CONTRADICT)
    ] == [BOTECO]
    assert {
        (r["handle"], r["venue_id"])
        for r in repo.list_venue_link_audit_reviews(decided=False)
    } == {CONCHITTAS, LACASA}


def test_the_repository_forwards_the_decision_including_the_missing_row_case():
    repo, store = _repo()
    repo.upsert_venue_link_audit_review(*LACASA, _machine_fields())
    decided = repo.set_venue_link_audit_review_decision(
        *LACASA, DECISION_ACCEPTED, "neighborhood data is wrong, street matches",
    )
    assert decided["operator_decision"] == DECISION_ACCEPTED
    assert decided["operator_note"] == "neighborhood data is wrong, street matches"
    assert store.get_venue_link_audit_review(*LACASA)["operator_decision"] == DECISION_ACCEPTED
    assert repo.set_venue_link_audit_review_decision(
        *BOTECO, DECISION_REJECTED, None,
    ) is None
