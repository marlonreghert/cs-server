"""Unit coverage for `app.services.event_occurrence_end`.

plans/260907_events-non-event-and-recurrence-normalisation.md §3a. A
pure-input matrix over all SIX outcomes on BOTH branches, because the
decision this module makes — DERIVATION, never `is_recurring` — is the one
revision 1 of that plan got wrong, and getting it wrong deletes the genuine
end of a multi-day festival rather than merely serving a stale one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.event_occurrence_end import (
    MAX_DERIVED_DURATION,
    OUTCOME_ABSENT,
    OUTCOME_CARRIED,
    OUTCOME_DERIVED,
    OUTCOME_DROPPED_IMPLAUSIBLE,
    OUTCOME_DROPPED_INVERTED,
    OUTCOME_DROPPED_ZERO,
    resolve_occurrence_end,
)

_STORED_START = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc)
_OCCURRENCE_START = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)


def _end(hours):
    return _STORED_START + timedelta(hours=hours)


class TestTheCarriedBranch:
    """`occurrence_id == event_id` — every non-recurring row, and every
    recurring row whose recurrence prose this repo cannot parse into
    weekdays. The occurrence IS the stored announcement."""

    def test_a_stored_end_is_carried_verbatim(self):
        value, outcome = resolve_occurrence_end(
            _STORED_START, _end(5), _STORED_START, derived=False
        )
        assert value == _end(5)
        assert outcome == OUTCOME_CARRIED

    def test_a_three_day_interval_survives_the_24h_bound(self):
        """The multi-day festival vibes_bot declines to truncate at serve
        time. The bound must not reach this branch — here the stored end is
        the end of this exact interval, not a stale instant."""
        value, outcome = resolve_occurrence_end(
            _STORED_START, _end(72), _STORED_START, derived=False
        )
        assert value == _end(72)
        assert outcome == OUTCOME_CARRIED

    def test_a_zero_length_interval_is_carried_not_dropped(self):
        """Equality is permitted by the upstream invariant and vibes_bot
        serves it, so cs-server must not delete it one layer earlier."""
        value, outcome = resolve_occurrence_end(
            _STORED_START, _STORED_START, _STORED_START, derived=False
        )
        assert value == _STORED_START
        assert outcome == OUTCOME_CARRIED

    def test_an_inverted_pair_is_dropped(self):
        """cs-server never emits an occurrence that ends before it starts,
        on either branch."""
        value, outcome = resolve_occurrence_end(
            _STORED_START, _end(-3), _STORED_START, derived=False
        )
        assert value is None
        assert outcome == OUTCOME_DROPPED_INVERTED

    def test_no_stored_end_reads_absent(self):
        assert resolve_occurrence_end(
            _STORED_START, None, _STORED_START, derived=False
        ) == (None, OUTCOME_ABSENT)

    def test_the_carried_value_is_the_stored_object_itself(self):
        """Byte-for-byte: this branch's behaviour is exactly what 1.4.0
        already served, so the fix cannot leak into it."""
        stored_end = _end(5)
        value, _outcome = resolve_occurrence_end(
            _STORED_START, stored_end, _STORED_START, derived=False
        )
        assert value is stored_end


class TestTheDerivedBranch:
    """`occurrence_id != event_id` — the stored end's DATE was thrown away
    when `expand_occurrences` re-derived this occurrence's day from the
    weekday pattern, so only the stored DURATION is still meaningful."""

    def test_a_usable_duration_lands_on_the_occurrences_own_date(self):
        value, outcome = resolve_occurrence_end(
            _STORED_START, _end(1), _OCCURRENCE_START, derived=True
        )
        assert value == _OCCURRENCE_START + timedelta(hours=1)
        assert outcome == OUTCOME_DERIVED

    def test_the_bound_is_inclusive_at_exactly_24_hours(self):
        value, outcome = resolve_occurrence_end(
            _STORED_START, _STORED_START + MAX_DERIVED_DURATION,
            _OCCURRENCE_START, derived=True,
        )
        assert value == _OCCURRENCE_START + MAX_DERIVED_DURATION
        assert outcome == OUTCOME_DERIVED

    def test_a_30_hour_duration_is_dropped_as_implausible(self):
        """A >24h "duration" on a weekly night is indistinguishable from the
        announcement's own stale absolute end — the exact defect C3."""
        value, outcome = resolve_occurrence_end(
            _STORED_START, _end(30), _OCCURRENCE_START, derived=True
        )
        assert value is None
        assert outcome == OUTCOME_DROPPED_IMPLAUSIBLE

    def test_a_zero_duration_is_dropped_zero_not_dropped_inverted(self):
        """A zero-length interval is not an inversion. Folding the two makes
        the counter unreadable as the data-defect signal it exists to be."""
        value, outcome = resolve_occurrence_end(
            _STORED_START, _STORED_START, _OCCURRENCE_START, derived=True
        )
        assert value is None
        assert outcome == OUTCOME_DROPPED_ZERO

    def test_an_inverted_pair_is_dropped(self):
        value, outcome = resolve_occurrence_end(
            _STORED_START, _end(-1296), _OCCURRENCE_START, derived=True
        )
        assert value is None
        assert outcome == OUTCOME_DROPPED_INVERTED

    def test_no_stored_end_reads_absent(self):
        assert resolve_occurrence_end(
            _STORED_START, None, _OCCURRENCE_START, derived=True
        ) == (None, OUTCOME_ABSENT)

    def test_the_live_defect_shape_now_answers_dropped_or_derived(self):
        """The four live source rows measured on 2026-09-07: every stored
        `ends_at` sat weeks before its own occurrence's `starts_at`. None of
        them may still project a wrong instant."""
        for offset_hours in (-1296, -1008, -504):
            value, outcome = resolve_occurrence_end(
                _STORED_START, _end(offset_hours), _OCCURRENCE_START, derived=True
            )
            assert value is None
            assert outcome == OUTCOME_DROPPED_INVERTED


class TestTotality:
    def test_a_null_stored_start_answers_absent_rather_than_raising(self):
        """Unreachable from the projector — `expand_occurrences` yields no
        occurrences at all for a row with no `starts_at` — but a pure
        function must be total."""
        for derived in (True, False):
            assert resolve_occurrence_end(
                None, _end(2), _OCCURRENCE_START, derived=derived
            ) == (None, OUTCOME_ABSENT)

    def test_naive_stored_values_are_read_as_utc(self):
        """Mirrors `event_occurrences._as_aware_utc`: a naive value only
        ever arrives from a fixture or the in-memory fake, and comparing it
        against a tz-aware instant would otherwise raise."""
        naive_start = datetime(2026, 8, 12, 22, 0)
        naive_end = datetime(2026, 8, 12, 23, 0)
        value, outcome = resolve_occurrence_end(
            naive_start, naive_end, _OCCURRENCE_START, derived=True
        )
        assert outcome == OUTCOME_DERIVED
        assert value == _OCCURRENCE_START + timedelta(hours=1)

    @pytest.mark.parametrize("derived", [True, False])
    def test_no_input_combination_ever_returns_an_inverted_end(self, derived):
        starts = [_STORED_START, _STORED_START + timedelta(days=30)]
        ends = [None, _end(-1296), _end(-1), _STORED_START, _end(1), _end(30), _end(72)]
        for stored_start in starts:
            for stored_end in ends:
                value, _outcome = resolve_occurrence_end(
                    stored_start, stored_end, _OCCURRENCE_START, derived=derived
                )
                if value is None:
                    continue
                own_start = _OCCURRENCE_START if derived else stored_start
                assert value >= own_start, (stored_start, stored_end, value)
