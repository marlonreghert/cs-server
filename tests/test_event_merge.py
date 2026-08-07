"""Unit tests for app/services/event_merge.py.

See plans/260807_one-event-many-posts.md. Covers the identity function
(venue/date/title, including the null-venue and null-date refusals), canonical
selection (oldest event_id, confirmed/manually-linked precedence, the
more-than-one-protected refusal), and the field-merge matrix: exactly one
source has a value, several agree, several disagree (recency-broken, flagged),
and lineup union.

`merge_touched_events` (the runtime orchestration against a live venue_dao) is
covered end-to-end by tests/bdd/enrichment/one-event-many-posts.feature and by
tests/test_event_reconciliation.py-style DAO plumbing; this file protects the
pure decision functions both that runtime path AND migration 0026 import
directly, so a bug here would be wrong in both places at once.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.event_merge import (
    REVIEW_REASON_SOURCES_DISAGREE,
    choose_canonical,
    compute_event_identity,
    merge_event_fields,
)
from app.services.event_reconciliation import REVIEW_REASON_DIVERGES_FROM_CONFIRMED

_D1 = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
_D1_NOON = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
_D2 = datetime(2026, 8, 9, 22, 0, tzinfo=timezone.utc)


def _event(event_id, **overrides) -> dict:
    base = {
        "event_id": event_id, "venue_id": "v1", "starts_at": _D1,
        "title": "Noite da Patroa", "status": "pending_review",
        "location_resolution": None, "review_reason": None,
        "ends_at": None, "is_recurring": False, "recurrence_text": None,
        "description": None, "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": None, "last_seen_at": _D1,
    }
    base.update(overrides)
    return base


class TestComputeEventIdentity:
    def test_same_venue_date_and_title_are_the_same_identity(self):
        a = compute_event_identity("v1", _D1, "Noite da Patroa")
        b = compute_event_identity("v1", _D1_NOON, "Noite da Patroa")
        assert a == b  # only the DATE participates, not the clock time

    def test_case_and_accent_differences_do_not_change_identity(self):
        a = compute_event_identity("v1", _D1, "NOITE DA PATROA")
        b = compute_event_identity("v1", _D1, "Noite da Patroa")
        assert a == b

    def test_different_venue_is_a_different_identity(self):
        a = compute_event_identity("v1", _D1, "Noite da Patroa")
        b = compute_event_identity("v2", _D1, "Noite da Patroa")
        assert a != b

    def test_different_date_is_a_different_identity(self):
        a = compute_event_identity("v1", _D1, "Noite da Patroa")
        b = compute_event_identity("v1", _D2, "Noite da Patroa")
        assert a != b

    def test_materially_different_title_is_a_different_identity(self):
        a = compute_event_identity("v1", _D1, "Noite da Patroa")
        b = compute_event_identity("v1", _D1, "Equilibrium na Metropole")
        assert a != b

    def test_null_venue_never_computes_an_identity(self):
        assert compute_event_identity(None, _D1, "Noite da Patroa") is None

    def test_null_date_never_computes_an_identity(self):
        assert compute_event_identity("v1", None, "Noite da Patroa") is None

    def test_null_venue_and_null_date_together_never_compute_an_identity(self):
        assert compute_event_identity(None, None, "Noite da Patroa") is None


class TestChooseCanonical:
    def test_the_oldest_event_id_wins_when_nothing_is_protected(self):
        events = [_event("evt_c"), _event("evt_a"), _event("evt_b")]
        canonical = choose_canonical(events)
        assert canonical["event_id"] == "evt_a"

    def test_a_sole_confirmed_event_wins_even_when_not_oldest(self):
        events = [
            _event("evt_a"),
            _event("evt_b", status="confirmed"),
            _event("evt_c"),
        ]
        canonical = choose_canonical(events)
        assert canonical["event_id"] == "evt_b"

    def test_a_sole_manually_linked_event_wins_even_when_not_oldest(self):
        events = [
            _event("evt_a"),
            _event("evt_b", location_resolution="manual"),
        ]
        canonical = choose_canonical(events)
        assert canonical["event_id"] == "evt_b"

    def test_two_confirmed_events_refuse_to_choose_a_canonical(self):
        events = [
            _event("evt_a", status="confirmed"),
            _event("evt_b", status="confirmed"),
        ]
        assert choose_canonical(events) is None

    def test_a_confirmed_and_a_manually_linked_event_together_also_refuse(self):
        events = [
            _event("evt_a", status="confirmed"),
            _event("evt_b", location_resolution="manual"),
        ]
        assert choose_canonical(events) is None


class TestFieldMergeMatrix:
    """Per scalar: exactly one has a value -> use it; several agree -> use
    it; several disagree -> the more recently SEEN event's value wins and
    the fold is flagged sources_disagree. lineup unions rather than choosing."""

    def test_exactly_one_source_has_a_value_the_other_is_absent(self):
        canonical = _event("evt_a", description=None)
        duplicate = _event("evt_b", description="Uma noite especial")
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed["description"] == "Uma noite especial"
        assert reason is None

    def test_the_canonicals_own_value_stands_when_the_duplicate_is_absent(self):
        canonical = _event("evt_a", description="Uma noite especial")
        duplicate = _event("evt_b", description=None)
        changed, reason = merge_event_fields(canonical, duplicate)
        assert "description" not in changed
        assert reason is None

    def test_several_sources_agreeing_is_not_a_disagreement(self):
        canonical = _event("evt_a", price_text="R$30")
        duplicate = _event("evt_b", price_text="R$30")
        changed, reason = merge_event_fields(canonical, duplicate)
        assert "price_text" not in changed
        assert reason is None

    def test_title_agreeing_only_after_normalization_is_not_a_disagreement(self):
        """Two events reach a merge only because their normalized titles
        already matched (that IS compute_event_identity) — a raw-string
        comparison would wrongly flag "NOITE DA PATROA" vs "Noite da
        Patroa" as sources disagreeing over casing that was never really
        conflicting information."""
        canonical = _event("evt_a", title="NOITE DA PATROA")
        duplicate = _event("evt_b", title="Noite da Patroa")
        changed, reason = merge_event_fields(canonical, duplicate)
        assert "title" not in changed
        assert reason is None

    def test_a_teasers_defaulted_midnight_never_contradicts_a_later_real_time(self):
        """A teaser naming only a date resolves `starts_at` to a DEFAULTED
        midnight (event_date_resolver: "a stated '00h' and a defaulted
        midnight land on the exact same starts_at instant"), never `None` —
        a plain equality/emptiness check would misread the later post's real
        22:00 as a contradicting time rather than the teaser simply never
        having stated one."""
        teaser_starts_at = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)
        detailed_starts_at = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
        canonical = _event(
            "evt_a", starts_at=teaser_starts_at, last_seen_at=_D1,
            raw_extraction={"time_known": False},
        )
        duplicate = _event(
            "evt_b", starts_at=detailed_starts_at,
            last_seen_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            raw_extraction={"time_known": True},
        )
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed["starts_at"] == detailed_starts_at
        assert reason is None  # completed, not disputed

    def test_a_real_time_is_never_overwritten_by_a_later_teasers_defaulted_midnight(self):
        detailed_starts_at = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
        teaser_starts_at = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)
        canonical = _event(
            "evt_a", starts_at=detailed_starts_at, last_seen_at=_D1,
            raw_extraction={"time_known": True},
        )
        duplicate = _event(
            "evt_b", starts_at=teaser_starts_at,
            last_seen_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            raw_extraction={"time_known": False},
        )
        changed, reason = merge_event_fields(canonical, duplicate)
        assert "starts_at" not in changed
        assert reason is None

    def test_two_real_but_different_times_are_a_genuine_disagreement(self):
        canonical = _event(
            "evt_a", starts_at=datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc),
            last_seen_at=_D1, raw_extraction={"time_known": True},
        )
        duplicate = _event(
            "evt_b", starts_at=datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
            raw_extraction={"time_known": True},
        )
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed["starts_at"] == duplicate["starts_at"]
        assert reason == REVIEW_REASON_SOURCES_DISAGREE

    def test_disagreeing_sources_take_the_more_recently_seen_value_and_flag_it(self):
        canonical = _event(
            "evt_a", ticket_url="https://old.example", last_seen_at=_D1,
        )
        duplicate = _event(
            "evt_b", ticket_url="https://new.example",
            last_seen_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        )
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed["ticket_url"] == "https://new.example"
        assert reason == REVIEW_REASON_SOURCES_DISAGREE

    def test_a_less_recent_duplicate_never_overwrites_a_more_recent_canonical(self):
        canonical = _event(
            "evt_a", ticket_url="https://new.example",
            last_seen_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        )
        duplicate = _event("evt_b", ticket_url="https://old.example", last_seen_at=_D1)
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed["ticket_url"] == "https://new.example"
        assert reason == REVIEW_REASON_SOURCES_DISAGREE

    def test_lineup_unions_preserving_order_and_dropping_duplicates(self):
        canonical = _event("evt_a", lineup=["DJ A", "DJ B"])
        duplicate = _event("evt_b", lineup=["DJ B", "DJ C"])
        changed, _reason = merge_event_fields(canonical, duplicate)
        assert changed["lineup"] == ["DJ A", "DJ B", "DJ C"]

    def test_lineup_unchanged_is_not_included_in_the_update(self):
        canonical = _event("evt_a", lineup=["DJ A"])
        duplicate = _event("evt_b", lineup=[])
        changed, _reason = merge_event_fields(canonical, duplicate)
        assert "lineup" not in changed

    def test_no_disagreement_anywhere_leaves_review_reason_untouched(self):
        canonical = _event("evt_a", review_reason="low_confidence")
        duplicate = _event("evt_b")
        _changed, reason = merge_event_fields(canonical, duplicate)
        assert reason == "low_confidence"


class TestConfirmedCanonicalIsFrozen:
    def test_a_confirmed_canonicals_fields_are_never_recomputed(self):
        canonical = _event("evt_a", status="confirmed", title="Corrected by Operator")
        duplicate = _event("evt_b", title="Noite da Patroa", description="New info")
        changed, _reason = merge_event_fields(canonical, duplicate)
        assert changed == {}

    def test_a_confirmed_canonical_still_flags_a_genuine_divergence(self):
        canonical = _event("evt_a", status="confirmed", description="Original")
        duplicate = _event("evt_b", description="Something else entirely")
        _changed, reason = merge_event_fields(canonical, duplicate)
        assert reason == REVIEW_REASON_DIVERGES_FROM_CONFIRMED

    def test_a_confirmed_canonical_is_not_flagged_when_the_duplicate_agrees(self):
        canonical = _event("evt_a", status="confirmed", description="Same info")
        duplicate = _event("evt_b", description="Same info")
        _changed, reason = merge_event_fields(canonical, duplicate)
        assert reason is None

    def test_a_confirmed_canonical_is_not_flagged_by_an_absent_duplicate_field(self):
        canonical = _event("evt_a", status="confirmed", description="Original")
        duplicate = _event("evt_b", description=None)
        _changed, reason = merge_event_fields(canonical, duplicate)
        assert reason is None

    def test_a_confirmed_canonical_is_not_flagged_by_title_casing_alone(self):
        canonical = _event("evt_a", status="confirmed", title="NOITE DA PATROA")
        duplicate = _event("evt_b", title="Noite da Patroa")
        _changed, reason = merge_event_fields(canonical, duplicate)
        assert reason is None

    def test_a_missing_operator_edited_fields_key_behaves_like_null(self):
        """Migration 0026's raw-SQL-built dicts never carry this key at all
        (it postdates 0026 — migration 0027) — `.get(...)` must read that
        exactly like an explicit `None`, so the migration's replay always
        lands in the legacy whole-row-freeze branch, unchanged from what it
        shipped with."""
        canonical = _event("evt_a", status="confirmed", price_text="R$30")
        assert "operator_edited_fields" not in canonical
        duplicate = _event("evt_b", price_text="R$99")
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed == {}
        assert reason == REVIEW_REASON_DIVERGES_FROM_CONFIRMED


class TestFieldLevelProtectionOnTheMergePath:
    """plans/260807_auto-accept-and-field-level-protection.md's coordination
    note: a CONFIRMED canonical whose `operator_edited_fields` is a real
    list applies the SAME per-field table a same-post re-extraction does
    (app.services.event_reconciliation.apply_operator_field_protection) —
    proven here at the same level TestConfirmedCanonicalIsFrozen already
    uses (a direct `merge_event_fields` call), since two events reaching
    this function are, by construction, already identity-matched (see
    compute_event_identity) and title specifically can therefore never
    genuinely disagree here post-normalization — exactly why these tests
    exercise a non-title edited field (`price_text`) for the "edited field
    genuinely differs" case, while a separate test still proves an edited
    TITLE's exact casing is never silently re-cased by an unedited fold."""

    def test_a_null_new_value_never_overwrites_a_known_one(self):
        canonical = _event(
            "evt_a", status="confirmed", operator_edited_fields=["title"],
            price_text="R$30",
        )
        duplicate = _event("evt_b", price_text=None)
        changed, _reason = merge_event_fields(canonical, duplicate)
        assert "price_text" not in changed

    def test_an_unedited_field_folds_in_from_the_more_recently_seen_source(self):
        canonical = _event(
            "evt_a", status="confirmed", operator_edited_fields=["title"],
            description="Old info", last_seen_at=_D1,
        )
        duplicate = _event("evt_b", description="New info", last_seen_at=_D2)
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed["description"] == "New info"
        assert reason is None  # unedited fold-in never flags

    def test_a_less_recently_seen_duplicates_unedited_field_never_overwrites(self):
        canonical = _event(
            "evt_a", status="confirmed", operator_edited_fields=["title"],
            description="Current info", last_seen_at=_D2,
        )
        duplicate = _event("evt_b", description="Stale info", last_seen_at=_D1)
        changed, _reason = merge_event_fields(canonical, duplicate)
        assert "description" not in changed

    def test_an_edited_field_that_genuinely_differs_is_kept_and_flagged(self):
        """The exact shape the coordination note asks for: an
        OPERATOR-EDITED field (here `price_text`, standing in for `title`,
        which can never genuinely disagree at this point — see the class
        docstring) survives a contradicting duplicate and the divergence is
        flagged, while an unedited field in the SAME call is absorbed."""
        canonical = _event(
            "evt_a", status="confirmed", operator_edited_fields=["price_text"],
            price_text="R$30 corrigido", description="Old info", last_seen_at=_D1,
        )
        duplicate = _event(
            "evt_b", price_text="R$99", description="New info", last_seen_at=_D2,
        )
        changed, reason = merge_event_fields(canonical, duplicate)
        assert "price_text" not in changed  # kept — never silently overwritten
        assert changed["description"] == "New info"  # absorbed — unedited
        assert reason == REVIEW_REASON_DIVERGES_FROM_CONFIRMED  # flagged

    def test_an_edited_titles_exact_casing_survives_an_unedited_fold(self):
        """Title can never genuinely DISAGREE post-normalization once two
        events reach this function (compute_event_identity groups on it) —
        but that must not be confused with "title is free to be re-cased".
        An operator who edited title keeps their EXACT casing even when a
        duplicate states the model's own (differently-cased) form."""
        canonical = _event(
            "evt_a", status="confirmed", operator_edited_fields=["title"],
            title="Noite da Patroa - Edição Especial",
        )
        duplicate = _event("evt_b", title="NOITE DA PATROA - EDIÇÃO ESPECIAL")
        changed, _reason = merge_event_fields(canonical, duplicate)
        assert "title" not in changed
        assert canonical["title"] == "Noite da Patroa - Edição Especial"

    def test_lineup_unions_even_when_the_canonical_is_confirmed_and_edited(self):
        canonical = _event(
            "evt_a", status="confirmed", operator_edited_fields=["title"],
            lineup=["DJ A"],
        )
        duplicate = _event("evt_b", lineup=["DJ B"])
        changed, _reason = merge_event_fields(canonical, duplicate)
        assert changed["lineup"] == ["DJ A", "DJ B"]

    def test_a_legacy_null_operator_edited_fields_keeps_whole_row_protection(self):
        canonical = _event(
            "evt_a", status="confirmed", operator_edited_fields=None,
            price_text="R$30", description="Original",
        )
        duplicate = _event("evt_b", price_text="R$999", description="Something else")
        changed, reason = merge_event_fields(canonical, duplicate)
        assert changed == {}
        assert reason == REVIEW_REASON_DIVERGES_FROM_CONFIRMED
