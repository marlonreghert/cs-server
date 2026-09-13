"""Unit tests for `event_dedup.in_candidate_window_for_rows`.

See plans/260912_events-venue-night-duplication.md §D (Defect 3).
`expand_occurrences` re-derives a recurring row's served days from its
weekday pattern and discards its stored date entirely; both merge gates key
on that same stored date. So two posts about one weekly night stored three
weeks apart are never compared, yet expand onto the same served dates —
confirmed on two live rows titled `Aula de FORRÓ na Sala de Reboco`, both
`recurrence_text = 'Toda QUARTA'`, stored 21 days apart.

The three assertions this file exists for, because each is a choice a future
reader will want to undo:
  - the flag OFF reduces the new function to `in_candidate_window` EXACTLY;
  - weekday sets INTERSECT rather than being equal;
  - the weekday set comes from `weekdays_from_recurrence_text` and not from a
    local re-parse (pinned by patching that ONE function and observing this
    one change its answer).

Division of labour against the BDD sibling
(`tests/bdd/enrichment/events-venue-night-duplication.feature`): the
scenarios there prove two weekly announcements actually merge and keep
serving the same nights; this file pins the window predicate itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services import event_dedup

RECIFE = ZoneInfo("America/Recife")


def _local(date_str: str, time_str: str = "20:00") -> datetime:
    hour, minute = (int(p) for p in time_str.split(":"))
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=RECIFE)


def _row(*, starts_at, is_recurring=False, recurrence_text=None):
    return {
        "starts_at": starts_at, "is_recurring": is_recurring,
        "recurrence_text": recurrence_text,
    }


def _weekly(date_str, recurrence_text="Toda QUARTA", time_str="20:00"):
    return _row(
        starts_at=_local(date_str, time_str), is_recurring=True,
        recurrence_text=recurrence_text,
    )


def _window(a, b, *, enabled=True, hours=8):
    return event_dedup.in_candidate_window_for_rows(
        a, b, window_hours=hours, recurring_window_enabled=enabled,
    )


# The two live rows the RCA measured, 21 days apart.
_FORRO_A = _weekly("2026-08-05")
_FORRO_B = _weekly("2026-08-26")


class TestTheWidenedWindow:
    def test_two_weekly_rows_on_the_same_weekday_stored_three_weeks_apart_are_candidates(self):
        assert _window(_FORRO_A, _FORRO_B) is True

    def test_the_plain_window_alone_would_never_have_paired_them(self):
        assert event_dedup.in_candidate_window(
            _FORRO_A["starts_at"], _FORRO_B["starts_at"], window_hours=8,
        ) is False

    def test_two_weekly_rows_on_different_weekdays_are_not_candidates(self):
        wednesday = _weekly("2026-08-05", "Toda QUARTA")
        friday = _weekly("2026-08-28", "Toda SEXTA")
        assert _window(wednesday, friday) is False

    def test_intersecting_weekday_sets_are_enough_equality_is_not_required(self):
        # `Toda QUARTA` and `Quartas e sextas` collide on every Wednesday
        # they both serve; requiring EQUAL sets would miss exactly the
        # collision this rule exists to catch.
        one_day = _weekly("2026-08-05", "Toda QUARTA")
        two_days = _weekly("2026-08-26", "Quartas e sextas")
        assert _window(one_day, two_days) is True

    def test_a_recurring_row_never_pairs_with_a_one_off_row(self):
        one_off = _row(starts_at=_local("2026-08-26"))
        assert _window(_FORRO_A, one_off) is False
        assert _window(one_off, _FORRO_A) is False

    def test_unparseable_recurrence_prose_on_either_side_excludes(self):
        vague = _weekly("2026-08-26", "toda semana")
        assert _window(_FORRO_A, vague) is False
        assert _window(vague, _FORRO_A) is False

    def test_a_missing_recurrence_text_excludes(self):
        no_text = _row(starts_at=_local("2026-08-26"), is_recurring=True, recurrence_text=None)
        assert _window(_FORRO_A, no_text) is False

    def test_the_rule_is_symmetric(self):
        assert _window(_FORRO_A, _FORRO_B) == _window(_FORRO_B, _FORRO_A)
        wednesday = _weekly("2026-08-05", "Toda QUARTA")
        friday = _weekly("2026-08-28", "Toda SEXTA")
        assert _window(wednesday, friday) == _window(friday, wednesday)

    def test_two_weekly_rows_with_no_stored_date_at_all_are_still_candidates_by_weekday(self):
        # `in_candidate_window` refuses a pair where either side has no
        # `starts_at`; the weekday half is independent of the stored date by
        # design, which is the entire point of Defect 3.
        a = _row(starts_at=None, is_recurring=True, recurrence_text="Toda QUARTA")
        b = _row(starts_at=None, is_recurring=True, recurrence_text="Toda QUARTA")
        assert _window(a, b) is True


class TestTheFlagOff:
    def test_the_flag_off_reduces_the_function_to_the_plain_window_exactly(self):
        cases = [
            (_FORRO_A, _FORRO_B),
            (_weekly("2026-08-05"), _weekly("2026-08-05", time_str="23:00")),
            (_weekly("2026-08-05"), _weekly("2026-08-06", "Quartas e sextas", "02:00")),
            (_row(starts_at=_local("2026-08-05")), _row(starts_at=_local("2026-08-05"))),
            (_row(starts_at=None), _weekly("2026-08-05")),
        ]
        for a, b in cases:
            assert _window(a, b, enabled=False) == event_dedup.in_candidate_window(
                a.get("starts_at"), b.get("starts_at"), window_hours=8,
            ), (a, b)

    def test_the_flag_off_refuses_the_three_week_pair(self):
        assert _window(_FORRO_A, _FORRO_B, enabled=False) is False

    def test_the_flag_on_never_NARROWS_the_window(self):
        # Strictly a widening: anything the plain window accepted must still
        # be accepted.
        same_night = (_weekly("2026-08-05"), _weekly("2026-08-05", "Toda SEXTA", "23:00"))
        assert _window(*same_night, enabled=False) is True
        assert _window(*same_night, enabled=True) is True


class TestTheWeekdaySetComesFromTheSharedParser:
    def test_it_calls_weekdays_from_recurrence_text_and_does_not_re_parse(self, monkeypatch):
        """If this window and `expand_occurrences` could ever disagree about
        which nights a row serves, the window would stop being evidence. The
        guard: patch the ONE shared function and require this one to follow
        it. A local re-parse would ignore the patch and keep answering
        True."""
        calls = []

        def _fake(recurrence_text):
            calls.append(recurrence_text)
            return frozenset()

        monkeypatch.setattr(event_dedup, "weekdays_from_recurrence_text", _fake)
        assert _window(_FORRO_A, _FORRO_B) is False
        assert calls == ["Toda QUARTA", "Toda QUARTA"], calls

    def test_it_is_the_same_function_event_occurrences_uses(self):
        from app.services import event_occurrences

        assert (
            event_dedup.weekdays_from_recurrence_text
            is event_occurrences.weekdays_from_recurrence_text
        )


class TestConfigPlumbing:
    def test_the_recurring_window_ships_disabled(self):
        assert event_dedup.DEFAULT_RECURRING_WINDOW_ENABLED is False
        assert event_dedup.load_dedup_config(None).recurring_window_enabled is False

    def test_a_stored_string_never_coerces_the_flag_on(self):
        class _FakeRedis:
            def get(self, key):
                if key == event_dedup.ADMIN_CONFIG_RECURRING_WINDOW_ENABLED_KEY:
                    return '"true"'
                return None

        assert event_dedup.load_dedup_config(_FakeRedis()).recurring_window_enabled is False

    def test_a_real_stored_true_enables_it(self):
        import json

        class _FakeRedis:
            def get(self, key):
                if key == event_dedup.ADMIN_CONFIG_RECURRING_WINDOW_ENABLED_KEY:
                    return json.dumps(True)
                return None

        assert event_dedup.load_dedup_config(_FakeRedis()).recurring_window_enabled is True


def test_the_original_in_candidate_window_is_untouched():
    """`in_candidate_window` keeps its signature and behaviour so its own
    existing tests keep meaning what they mean — this is a SIBLING, never a
    replacement."""
    a = datetime(2026, 8, 7, 21, 0, tzinfo=RECIFE)
    b = datetime(2026, 8, 8, 0, 0, tzinfo=RECIFE)
    assert event_dedup.in_candidate_window(a, b, window_hours=8) is True
    assert event_dedup.in_candidate_window(a, None, window_hours=8) is False
    far = datetime(2026, 8, 9, 21, 0, tzinfo=timezone.utc)
    assert event_dedup.in_candidate_window(a, far, window_hours=8) is False
