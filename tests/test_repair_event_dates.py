"""Unit tests for scripts/repair_event_dates.py.

See plans/260812_history-repair-dates.md's Test Plan.

`tests/bdd/enrichment/history-repair-dates.feature` covers the same behavior
end-to-end through the real `run_repair` against the in-memory RDS fake; this
file adds the lower-level layers the plan's Test Plan calls out by name:
  - each `date_text` shape from the Evidence table, resolved against the
    EXACT production strings;
  - key rewriting asserted directly against `compute_source_event_key`;
  - collision detection asserted against the real uniqueness constraint
    `insert_event` itself enforces (`uq_post_item_source_post`);
  - reason-set arithmetic (`_fold_date_reasons`) in isolation;
  - multi-source folding asserted directly against `merge_event_fields`,
    never a local reimplementation of "which source wins";
  - idempotency over a small mixed corpus;
  - the two Data/Config/API Impact guards (date-vocabulary landed, anchor
    coverage);
  - the protection table (`_protection_reason`) — plan's own note: "production
    currently has ZERO confirmed/operator-edited rows, so tests are the only
    place this is exercised."
"""
from __future__ import annotations

import inspect

from datetime import datetime, timezone

import pytest

from app.dao.venue_repository import VenueRepository
from app.models.event_kind import KIND_EVENT, KIND_OTHER
from app.models.venue import Venue
from app.services.event_date_resolver import RECIFE_TZ
from app.services.event_identity import compute_source_event_key
from app.services.event_merge import merge_event_fields
from scripts.repair_event_dates import (
    AnchorCoverageTooLow,
    DependencyNotLanded,
    Report,
    ArithmeticImbalance,
    _DATE_REASON_TOKENS,
    _RETIRED_REASON_TOKENS,
    _candidate_fields,
    _check_and_register_key,
    _date_reasons_for,
    _fold_candidates,
    _fold_date_reasons,
    _protection_reason,
    _resolve_source,
    _seed_key_index,
    check_balance,
    date_text_shape,
    run_repair,
)
from tests.rds_fake import InMemoryRdsVenueStore

_ANCHOR = datetime(2026, 8, 7, 12, 0, tzinfo=RECIFE_TZ)
_VENUE_ID = "v_repair_test"


# ── harness ──────────────────────────────────────────────────────────────────
def _dao() -> VenueRepository:
    store = InMemoryRdsVenueStore()
    dao = VenueRepository(client=None, rds_store=store)
    dao.upsert_venue(Venue(
        venue_id=_VENUE_ID, venue_name="Repair Test Venue",
        venue_lat=-8.05, venue_lng=-34.88, venue_address="",
    ))
    return dao


def _insert(
    dao: VenueRepository, n: int, *, date_text=None, time_text=None, starts_at=None,
    status="pending_review", review_reason=None, operator_edited_fields=None,
    uploaded_at=_ANCHOR, title=None, venue_id=_VENUE_ID, confidence=0.9,
    post_type=KIND_EVENT, is_recurring=None, recurrence_text=None,
    date_interpretation=None, source_handle=None, source_shortcode=None,
    last_seen_at=None,
) -> dict:
    title = title or f"Evento {n}"
    handle = source_handle or f"handle_{n}"
    shortcode = source_shortcode or f"post_{n}"
    event_id = f"evt_ut_{n:04d}"
    key = compute_source_event_key(title, starts_at)
    seen_at = last_seen_at or uploaded_at or datetime(2020, 1, 1, tzinfo=timezone.utc)
    fields = {
        "event_id": event_id, "venue_id": venue_id, "starts_at": starts_at,
        "ends_at": None, "is_recurring": bool(is_recurring), "recurrence_text": recurrence_text,
        "title": title, "status": status, "review_reason": review_reason,
        "operator_edited_fields": operator_edited_fields, "confidence": confidence,
        "post_type": post_type, "time_known": False,
        "source_kind": "venue_post", "source_handle": handle, "source_shortcode": shortcode,
        "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": key, "source_event_index": 1,
        "raw_extraction": {
            "date_text": date_text, "time_text": time_text,
            "is_recurring": is_recurring, "recurrence_text": recurrence_text,
        },
        "date_interpretation": date_interpretation, "source_uploaded_at": uploaded_at,
        "first_seen_at": seen_at, "last_seen_at": seen_at,
    }
    dao.insert_event(fields)
    source_id = dao.list_event_sources(event_id)[0]["id"]
    return {"event_id": event_id, "source_id": source_id, "handle": handle, "shortcode": shortcode}


def _row(dao: VenueRepository, entry: dict) -> dict:
    row = dao.get_event(entry["event_id"])
    assert row is not None
    return row


# ── date_text shape ────────────────────────────────────────────────────────────
class TestDateTextShape:
    def test_empty(self):
        assert date_text_shape(None) == "empty"
        assert date_text_shape("   ") == "empty"

    def test_relative(self):
        assert date_text_shape("É HOJE") == "relative_hoje_amanha"
        assert date_text_shape("Hoje!") == "relative_hoje_amanha"
        assert date_text_shape("amanhã tem show") == "relative_hoje_amanha"

    def test_numeric_dmy(self):
        assert date_text_shape("08/08") == "numeric_dmy"

    def test_range_preposition(self):
        assert date_text_shape("De 06 a 09 de fevereiro") == "range_de_x_a_y_de_mes"

    def test_textual_month(self):
        assert date_text_shape("05/SET") == "textual_month_name"

    def test_bare_weekday(self):
        assert date_text_shape("Quinta (02)") == "bare_weekday"

    def test_daily_cadence(self):
        assert date_text_shape("todo dia") == "cadence_daily_or_weekend"

    def test_no_computable_cadence(self):
        assert date_text_shape("toda semana") == "cadence_toda_semana_or_sempre"

    def test_different_shapes_are_actually_different(self):
        # An enumerated-assertion guard: the report groups by this value, so
        # two genuinely different texts must never collapse onto one shape.
        shapes = {
            date_text_shape(t) for t in (
                "É HOJE", "08/08", "De 06 a 09 de fevereiro", "05/SET",
                "Quinta (02)", "todo dia", "toda semana", None,
            )
        }
        assert len(shapes) == 8, shapes


# ── the evidence-table strings, through THIS script's own row-to-resolver wiring ──
class TestResolveSourceProductionStrings:
    """`_resolve_source` — never the bare `resolve_event_datetime` directly —
    so these tests prove the SCRIPT's own row -> resolver-call wiring
    (pulling date_text/time_text/is_recurring/recurrence_text/
    date_interpretation/source_uploaded_at off the stored row shape) is
    correct, not merely that the resolver itself works (already covered by
    tests/test_event_date_resolver.py)."""

    def test_e_hoje(self):
        row = {"raw_extraction": {"date_text": "É HOJE"}, "source_uploaded_at": _ANCHOR}
        resolved = _resolve_source(row)
        assert resolved.starts_at == datetime(2026, 8, 7, 0, 0, tzinfo=RECIFE_TZ)
        assert resolved.review_reason is None
        assert resolved.year_inferred is False

    def test_numeric_rolled_within_grace_keeps_anchor_year(self):
        anchor = datetime(2026, 8, 12, 12, 0, tzinfo=RECIFE_TZ)
        row = {"raw_extraction": {"date_text": "08/08"}, "source_uploaded_at": anchor}
        resolved = _resolve_source(row)
        assert resolved.starts_at == datetime(2026, 8, 8, 0, 0, tzinfo=RECIFE_TZ)
        assert resolved.year_inferred is True

    def test_preposition_range_resolves_to_first_day(self):
        row = {
            "raw_extraction": {"date_text": "De 06 a 09 de fevereiro"},
            "source_uploaded_at": _ANCHOR,
        }
        resolved = _resolve_source(row)
        assert resolved.starts_at == datetime(2027, 2, 6, 0, 0, tzinfo=RECIFE_TZ)
        assert resolved.date_range is True

    def test_abbreviated_month_reversed_form(self):
        row = {"raw_extraction": {"date_text": "05/SET"}, "source_uploaded_at": _ANCHOR}
        resolved = _resolve_source(row)
        assert resolved.starts_at == datetime(2026, 9, 5, 0, 0, tzinfo=RECIFE_TZ)
        assert resolved.review_reason is None
        assert resolved.year_inferred is False

    def test_weekday_plus_day_needs_the_stored_interpretation(self):
        # Deterministically alone, "Quinta (02)" carries a day numeral with
        # no month it can pair with -- unresolvable without the model's own
        # stored structured reading (plan Evidence: "resolvable" only via
        # the interpretation fallback, never a fresh model call).
        row = {"raw_extraction": {"date_text": "Quinta (02)"}, "source_uploaded_at": _ANCHOR}
        assert _resolve_source(row).starts_at is None

        row["date_interpretation"] = {"kind": "weekday_day", "weekday": "quinta", "day": 2}
        resolved = _resolve_source(row)
        assert resolved.starts_at == datetime(2026, 9, 2, 0, 0, tzinfo=RECIFE_TZ)
        assert resolved.date_source == "structured_fallback"

    def test_no_anchor_is_never_guessed(self):
        row = {"raw_extraction": {"date_text": "É HOJE"}, "source_uploaded_at": None}
        assert _resolve_source(row) is None


# ── key rewriting ──────────────────────────────────────────────────────────────
class TestKeyRewriting:
    def test_recomputed_key_matches_a_fresh_extraction(self):
        dao = _dao()
        entry = _insert(dao, 1, date_text="É HOJE", starts_at=None, title="Aniversário do Rodolpho")
        report = run_repair(dao, apply=True)
        assert report.by_disposition.get("repaired", 0) == 1

        row = _row(dao, entry)
        assert row["starts_at"] == datetime(2026, 8, 7, 0, 0, tzinfo=RECIFE_TZ)
        # The plan's own words: "assert against compute_source_event_key
        # directly, so the script cannot drift from the pipeline."
        assert row["source_event_key"] == compute_source_event_key(row["title"], row["starts_at"])

    def test_key_and_date_land_in_the_same_write(self):
        dao = _dao()
        entry = _insert(dao, 1, date_text="É HOJE", starts_at=None)
        report = run_repair(dao, apply=True)
        decision = next(d for d in report.rows if d.source_id == entry["source_id"])
        assert "source_event_key" in decision.write_fields
        assert "starts_at" in decision.write_fields


# ── collision detection ─────────────────────────────────────────────────────────
class TestCollisionDetection:
    def test_seed_and_register_agree_with_the_real_unique_constraint(self):
        dao = _dao()
        shared_title = "Show de Sexta"
        a = _insert(
            dao, 1, date_text="É HOJE", starts_at=None, title=shared_title,
            source_handle="h", source_shortcode="s",
        )
        b = _insert(
            dao, 2, date_text="07/08", starts_at=datetime(2027, 1, 1, tzinfo=RECIFE_TZ),
            title=shared_title, source_handle="h", source_shortcode="s",
        )

        all_rows = dao.list_all_event_sources_with_context()
        index = _seed_key_index(all_rows)
        row_a = next(r for r in all_rows if r["id"] == a["source_id"])
        row_b = next(r for r in all_rows if r["id"] == b["source_id"])

        new_key_a = compute_source_event_key(shared_title, datetime(2026, 8, 7, 0, 0, tzinfo=RECIFE_TZ))
        new_key_b = compute_source_event_key(shared_title, datetime(2026, 8, 7, 0, 0, tzinfo=RECIFE_TZ))
        assert new_key_a == new_key_b  # both sources now describe the same real event

        assert _check_and_register_key(index, row_a, new_key_a) is None
        owner = _check_and_register_key(index, row_b, new_key_b)
        assert owner == a["source_id"]

        # The collision is not a false positive of this script's own
        # bookkeeping: let the repair ACTUALLY write row A onto new_key_a,
        # then prove a third row genuinely inserted at (h, s, new_key_a)
        # WOULD violate uq_post_item_source_post for real.
        run_repair(dao, apply=True)
        assert _row(dao, a)["source_event_key"] == new_key_a
        with pytest.raises(ValueError, match="duplicate"):
            dao.insert_event({
                "event_id": "evt_ut_third", "venue_id": _VENUE_ID,
                "starts_at": datetime(2026, 8, 7, 0, 0, tzinfo=RECIFE_TZ), "title": shared_title,
                "status": "pending_review", "post_type": KIND_EVENT, "confidence": 0.9,
                "source_kind": "venue_post", "source_handle": "h", "source_shortcode": "s",
                "source_permalink": "https://instagram.com/p/s", "source_event_key": new_key_a,
                "source_event_index": 1, "raw_extraction": {},
                "first_seen_at": _ANCHOR, "last_seen_at": _ANCHOR,
            })

    def test_run_repair_reports_the_collision_and_never_forces_the_write(self):
        dao = _dao()
        shared_title = "Show de Sexta"
        a = _insert(
            dao, 1, date_text="É HOJE", starts_at=None, title=shared_title,
            source_handle="h", source_shortcode="s",
        )
        b = _insert(
            dao, 2, date_text="07/08", starts_at=datetime(2027, 1, 1, tzinfo=RECIFE_TZ),
            title=shared_title, source_handle="h", source_shortcode="s",
        )
        original_key_a = _row(dao, a)["source_event_key"]
        original_key_b = _row(dao, b)["source_event_key"]
        assert original_key_a != original_key_b  # true at fixture-creation time

        report = run_repair(dao, apply=True)

        ids = {a["source_id"], b["source_id"]}
        assert any({sid, owner} <= ids for sid, owner, _key in report.collisions)
        assert report.by_disposition.get("collided", 0) == 1
        # Exactly one of the pair collided; the other repaired normally --
        # never BOTH refused.
        assert report.by_disposition.get("repaired", 0) == 1

        collided_id = report.collisions[0][0]
        collided_entry, repaired_entry = (a, b) if collided_id == a["source_id"] else (b, a)
        original_collided_key = original_key_a if collided_entry is a else original_key_b

        # The collided row's own key is left EXACTLY as it was stored --
        # never forced onto the new, colliding value.
        assert _row(dao, collided_entry)["source_event_key"] == original_collided_key
        # The repaired sibling's key DID move, onto the value the pair now
        # collides on.
        colliding_key = report.collisions[0][2]
        assert _row(dao, repaired_entry)["source_event_key"] == colliding_key


# ── reason-set arithmetic ────────────────────────────────────────────────────────
class TestFoldDateReasons:
    def test_removal(self):
        assert _fold_date_reasons("missing_date", []) is None

    def test_addition(self):
        assert _fold_date_reasons(None, ["year_inferred"]) == "year_inferred"

    def test_composite_removes_one_keeps_another(self):
        assert _fold_date_reasons("missing_date; unresolved_venue", []) == "unresolved_venue"

    def test_composite_preserves_non_date_reason_order(self):
        result = _fold_date_reasons("unresolved_venue; missing_date", ["year_inferred"])
        assert result == "unresolved_venue; year_inferred"

    def test_replaces_one_date_reason_with_another(self):
        assert _fold_date_reasons("weekday_mismatch", ["year_inferred", "date_range"]) == (
            "year_inferred; date_range"
        )

    def test_already_correct_reason_is_a_no_op(self):
        assert _fold_date_reasons("unresolved_venue; year_inferred", ["year_inferred"]) == (
            "unresolved_venue; year_inferred"
        )


class TestDateReasonsFor:
    def test_missing_date_suppressed_for_exempt_post_type(self):
        from app.services.event_date_resolver import resolve_event_datetime
        resolved = resolve_event_datetime(date_text=None, time_text=None, post_timestamp=_ANCHOR)
        assert resolved.review_reason == "missing_date"
        assert _date_reasons_for(resolved, post_type=KIND_OTHER) == []
        assert _date_reasons_for(resolved, post_type=KIND_EVENT) == ["missing_date"]


# ── multi-source folding, asserted against merge_event_fields directly ───────────
class TestFoldCandidates:
    def test_single_contributor(self):
        row = {"id": "s1", "last_seen_at": _ANCHOR}
        resolved = _resolve_source({"raw_extraction": {"date_text": "É HOJE"}, "source_uploaded_at": _ANCHOR})
        folded, winner_id, winner_resolved = _fold_candidates([(row, resolved)])
        assert folded["starts_at"] == resolved.starts_at
        assert winner_id == "s1"
        assert winner_resolved is resolved

    def test_disagreement_is_broken_by_merge_event_fields_recency(self):
        # Both candidates state a real CLOCK TIME (time_known=True on both
        # sides) -- required for the disagreement to even be VISIBLE to
        # merge_event_fields: see test_two_time_unknown_disagreeing_dates_
        # keep_the_first_seen below for what happens when neither does.
        older = {"id": "s_older", "last_seen_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        newer = {"id": "s_newer", "last_seen_at": datetime(2026, 6, 1, tzinfo=timezone.utc)}
        resolved_older = _resolve_source({
            "raw_extraction": {"date_text": "05/SET", "time_text": "20h"}, "source_uploaded_at": _ANCHOR,
        })
        resolved_newer = _resolve_source({
            "raw_extraction": {"date_text": "É HOJE", "time_text": "22h"}, "source_uploaded_at": _ANCHOR,
        })
        assert resolved_older.time_known and resolved_newer.time_known
        assert resolved_older.starts_at != resolved_newer.starts_at  # a genuine disagreement

        folded, winner_id, _winner_resolved = _fold_candidates(
            [(older, resolved_older), (newer, resolved_newer)],
        )

        # The independently-computed expectation: the SAME two candidate
        # dicts, folded by calling merge_event_fields directly (never this
        # script's own _fold_candidates) -- the plan's own requirement that
        # this behavior is "asserted against merge_event_fields rather than
        # a local reimplementation."
        canonical = _candidate_fields(resolved_older)
        canonical["last_seen_at"] = older["last_seen_at"]
        duplicate = _candidate_fields(resolved_newer)
        duplicate["last_seen_at"] = newer["last_seen_at"]
        changed, _reason = merge_event_fields(canonical, duplicate)

        assert folded["starts_at"] == changed["starts_at"] == resolved_newer.starts_at
        assert winner_id == "s_newer"

    def test_two_time_unknown_disagreeing_dates_keep_the_first_seen(self):
        # A surprising but faithfully-inherited corner of merge_event_fields
        # (app.services.event_reconciliation.event_field_is_absent): a
        # DATE-only value (time NOT stated, the common case for a flyer that
        # names no clock time) counts as "empty" for merge purposes, exactly
        # like an unresolved None -- so when NEITHER contributor states a
        # time, a disagreement is invisible to the empty-check and the fold
        # simply never overwrites the running value. This is the SAME
        # production function every cross-post merge already uses; this
        # script inherits it deliberately rather than special-casing around
        # it (plan §A: never a local reimplementation of "which source
        # wins"). Documented explicitly here so it is a known, asserted
        # behavior rather than an accidental one.
        first_seen = {"id": "s_first", "last_seen_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        most_recent = {"id": "s_recent", "last_seen_at": datetime(2026, 12, 1, tzinfo=timezone.utc)}
        resolved_first = _resolve_source({
            "raw_extraction": {"date_text": "05/SET"}, "source_uploaded_at": _ANCHOR,
        })
        resolved_recent = _resolve_source({
            "raw_extraction": {"date_text": "É HOJE"}, "source_uploaded_at": _ANCHOR,
        })
        assert not resolved_first.time_known and not resolved_recent.time_known
        assert resolved_first.starts_at != resolved_recent.starts_at

        folded, winner_id, _ = _fold_candidates(
            [(first_seen, resolved_first), (most_recent, resolved_recent)],
        )
        assert folded["starts_at"] == resolved_first.starts_at
        assert winner_id == "s_first"

    def test_empty_contributor_never_beats_a_known_time_answer(self):
        # A known-CLOCK-TIME real value is unambiguously non-empty on either
        # side of the comparison, so (unlike the time-unknown case above)
        # this holds regardless of fold order.
        recent_but_empty = {"id": "s_empty", "last_seen_at": datetime(2026, 12, 1, tzinfo=timezone.utc)}
        older_but_real = {"id": "s_real", "last_seen_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        resolved_empty = _resolve_source({
            "raw_extraction": {"date_text": None}, "source_uploaded_at": _ANCHOR,
        })
        resolved_real = _resolve_source({
            "raw_extraction": {"date_text": "É HOJE", "time_text": "21h"}, "source_uploaded_at": _ANCHOR,
        })
        assert resolved_empty.starts_at is None
        assert resolved_real.time_known and resolved_real.starts_at is not None

        folded, winner_id, _ = _fold_candidates(
            [(older_but_real, resolved_real), (recent_but_empty, resolved_empty)],
        )
        assert folded["starts_at"] == resolved_real.starts_at
        assert winner_id == "s_real"

        # And the SAME result holds with the empty one seeded FIRST --
        # proving this is not an accident of seeding order.
        folded2, winner_id2, _ = _fold_candidates(
            [(recent_but_empty, resolved_empty), (older_but_real, resolved_real)],
        )
        assert folded2["starts_at"] == resolved_real.starts_at
        assert winner_id2 == "s_real"


# ── idempotency ──────────────────────────────────────────────────────────────────
class TestIdempotency:
    def test_second_apply_over_a_mixed_corpus_changes_nothing(self):
        dao = _dao()
        _insert(dao, 1, date_text="É HOJE", starts_at=None, title="Repairable")
        _insert(
            dao, 2, date_text="08/08", starts_at=datetime(2027, 8, 9, tzinfo=RECIFE_TZ),
            title="Wrong Year", uploaded_at=datetime(2026, 8, 12, 12, 0, tzinfo=RECIFE_TZ),
        )
        _insert(dao, 3, date_text=None, starts_at=None, review_reason="missing_date")
        _insert(dao, 4, date_text="É HOJE", starts_at=None, uploaded_at=None)  # no anchor
        _insert(
            dao, 5, date_text="É HOJE", starts_at=datetime(2026, 9, 1, 20, 0, tzinfo=RECIFE_TZ),
            operator_edited_fields=["starts_at"],
        )
        _insert(
            dao, 6, date_text="É HOJE", starts_at=datetime(2026, 9, 1, 20, 0, tzinfo=RECIFE_TZ),
            status="confirmed",
        )
        _insert(dao, 7, date_text="É HOJE", starts_at=None, status="superseded")

        first = run_repair(dao, apply=True)
        assert first.by_disposition.get("repaired", 0) >= 1

        snapshot = {
            row["id"]: dict(row)
            for row in dao.list_all_event_sources_with_context()
        }

        second = run_repair(dao, apply=True)
        assert second.by_disposition.get("repaired", 0) == 0
        assert second.by_disposition.get("collided", 0) == 0
        assert second.by_disposition.get("error", 0) == 0

        for row in dao.list_all_event_sources_with_context():
            before = snapshot[row["id"]]
            assert row["starts_at"] == before["starts_at"], row["id"]
            assert row["review_reason"] == before["review_reason"], row["id"]
            assert row["status"] == before["status"], row["id"]
            assert row["source_event_key"] == before["source_event_key"], row["id"]


# ── protection table ───────────────────────────────────────────────────────────
class TestProtectionReason:
    """plan's own note: production currently has ZERO confirmed/operator-
    edited rows, so this table is exercised only here and in the feature
    file — covered properly rather than left implicit."""

    def test_confirmed(self):
        assert _protection_reason({"status": "confirmed"}) == "confirmed"

    def test_operator_edited_starts_at(self):
        assert _protection_reason({
            "status": "accepted", "operator_edited_fields": ["starts_at"],
        }) == "operator"

    def test_operator_edited_other_field_is_not_protected(self):
        assert _protection_reason({
            "status": "accepted", "operator_edited_fields": ["title"],
        }) is None

    def test_superseded(self):
        assert _protection_reason({"status": "superseded"}) == "superseded"

    def test_extraction_failed_and_rejected(self):
        assert _protection_reason({"status": "extraction_failed"}) == "status"
        assert _protection_reason({"status": "rejected"}) == "status"

    def test_confirmed_outranks_operator_edit(self):
        # The strongest signal in the system gets the fuller (disagreement-
        # reporting) treatment even when the row ALSO carries an unrelated
        # operator edit.
        assert _protection_reason({
            "status": "confirmed", "operator_edited_fields": ["starts_at"],
        }) == "confirmed"

    def test_ordinary_accepted_row_is_unprotected(self):
        assert _protection_reason({"status": "accepted", "operator_edited_fields": None}) is None


# ── the never-overwrite-an-operator scenarios, at the run_repair level ───────────
class TestNeverOverwriteOperator:
    def test_confirmed_row_is_byte_identical_after_a_disagreeing_repair(self):
        dao = _dao()
        stored_at = datetime(2026, 9, 1, 20, 0, tzinfo=RECIFE_TZ)
        entry = _insert(dao, 1, date_text="É HOJE", starts_at=stored_at, status="confirmed")
        before = _row(dao, entry)

        report = run_repair(dao, apply=True)

        after = _row(dao, entry)
        assert after == before
        decision = next(d for d in report.rows if d.source_id == entry["source_id"])
        assert decision.action == "confirmed_conflict"

    def test_operator_edited_row_is_byte_identical_after_a_disagreeing_repair(self):
        dao = _dao()
        stored_at = datetime(2026, 9, 1, 20, 0, tzinfo=RECIFE_TZ)
        entry = _insert(
            dao, 1, date_text="É HOJE", starts_at=stored_at, status="accepted",
            operator_edited_fields=["starts_at"],
        )
        before = _row(dao, entry)

        report = run_repair(dao, apply=True)

        after = _row(dao, entry)
        assert after == before
        decision = next(d for d in report.rows if d.source_id == entry["source_id"])
        assert decision.action == "skipped_operator"


# ── the two Data/Config/API Impact guards ────────────────────────────────────────
class TestDependencyGuard:
    def test_refuses_to_run_before_reading_any_row_when_not_landed(self):
        import app.services.event_date_resolver as edr

        dao = _dao()
        _insert(dao, 1, date_text="É HOJE", starts_at=None)

        original = edr.select_date_interpretation_for_reuse
        delattr(edr, "select_date_interpretation_for_reuse")

        def _spy(*_a, **_kw):
            raise AssertionError("run_repair read events before checking the dependency guard")

        original_list = dao.list_all_event_sources_with_context
        dao.list_all_event_sources_with_context = _spy
        try:
            with pytest.raises(DependencyNotLanded):
                run_repair(dao, apply=False)
        finally:
            edr.select_date_interpretation_for_reuse = original
            dao.list_all_event_sources_with_context = original_list


class TestAnchorCoverageGuard:
    def test_small_corpus_is_never_gated_by_anchor_share(self):
        dao = _dao()
        _insert(dao, 1, date_text="É HOJE", starts_at=None, uploaded_at=None)
        # One row, zero anchors -- 100% missing, but far below the sample
        # floor, so this must run rather than refuse.
        report = run_repair(dao, apply=False)
        assert report.by_disposition.get("skipped_no_anchor", 0) == 1

    def test_large_corpus_with_too_many_missing_anchors_refuses(self):
        dao = _dao()
        for i in range(1, 25):
            _insert(dao, i, date_text="É HOJE", starts_at=None, uploaded_at=None)
        with pytest.raises(AnchorCoverageTooLow):
            run_repair(dao, apply=False, max_null_anchor_share=0.2)

    def test_large_corpus_within_the_configured_ceiling_runs(self):
        dao = _dao()
        for i in range(1, 21):
            _insert(dao, i, date_text="É HOJE", starts_at=None)
        for i in range(21, 24):
            _insert(dao, i, date_text="É HOJE", starts_at=None, uploaded_at=None)
        report = run_repair(dao, apply=False, max_null_anchor_share=0.5)
        assert report.selected == 23


# ── balance ──────────────────────────────────────────────────────────────────────
class TestCheckBalance:
    def test_balanced_report_does_not_raise(self):
        report = Report(selected=3)
        report.by_disposition["repaired"] = 2
        report.by_disposition["unchanged"] = 1
        check_balance(report)
        assert report.balanced is True

    def test_imbalanced_report_raises(self):
        report = Report(selected=5)
        report.by_disposition["repaired"] = 2
        with pytest.raises(ArithmeticImbalance):
            check_balance(report)
        assert report.balanced is False


# ── dry-run writes nothing ───────────────────────────────────────────────────────
class TestDryRun:
    def test_dry_run_writes_nothing_but_reports_the_would_be_change(self):
        dao = _dao()
        entry = _insert(dao, 1, date_text="É HOJE", starts_at=None, title="Dry Run Item")
        before = _row(dao, entry)

        report = run_repair(dao, apply=False)

        after = _row(dao, entry)
        assert after == before
        decision = next(d for d in report.rows if d.source_id == entry["source_id"])
        assert decision.action == "repaired"
        assert decision.new_starts_at == datetime(2026, 8, 7, 0, 0, tzinfo=RECIFE_TZ)
        assert decision.old_starts_at is None


class TestRetiredReviewReasons:
    """plans/260813_review-gate-and-date-vocabulary.md §D retired
    `unread_time`: the pipeline no longer writes it, and an event whose date
    resolved is no longer held up by a clock time alone.

    A row queued under the OLD rule keeps the stale token forever unless this
    repair clears it — nothing re-extracts a post whose crawl cursor has
    already moved past it. Production has exactly such a row (NOITE DA PATROA
    at Club Metrópole: a correct 2026-08-08 date, queued on `unread_time`
    alone), which is why this is the repair's job and not a re-extraction's.

    Distinct from `_DATE_REASON_TOKENS`, which are dropped only to be
    RECOMPUTED — a retired token is dropped and never re-added.
    """

    def test_a_retired_token_is_dropped_outright(self):
        assert _fold_date_reasons("unread_time", []) is None

    def test_a_retired_token_never_comes_back(self):
        assert _fold_date_reasons("unread_time", ["year_inferred"]) == "year_inferred"

    def test_a_reason_held_for_something_else_survives(self):
        assert _fold_date_reasons("unread_time; unresolved_venue", []) == "unresolved_venue"

    def test_a_retired_token_is_dropped_from_any_position(self):
        assert _fold_date_reasons("unresolved_venue; unread_time", []) == "unresolved_venue"
        assert _fold_date_reasons("unread_time; missing_date", []) is None

    def test_the_retired_set_and_the_date_set_do_not_overlap(self):
        """A token in both would be dropped and then recomputed back in,
        which is the opposite of retiring it."""
        assert not (_RETIRED_REASON_TOKENS & _DATE_REASON_TOKENS)

    def test_every_retired_token_is_one_the_pipeline_stopped_writing(self):
        """Guards against retiring a reason that is still live: if this
        fails, either the token was retired by mistake or the extraction path
        started writing it again."""
        import app.services.event_extraction_service as ees
        source = inspect.getsource(ees)
        for token in _RETIRED_REASON_TOKENS:
            assert f'reasons.append(REVIEW_REASON_UNREAD_TIME)' not in source, token
