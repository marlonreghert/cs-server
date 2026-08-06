"""Unit tests for app/services/event_identity.py.

See plans/260806_multi-event-posts.md §A: the row key for a multi-event post
must be content-derived, not positional — the model does not guarantee list
order between runs, so an ordinal key would silently migrate an operator's
confirmation onto a different event. These tests are the proof that the key
is genuinely stable across the things that legitimately vary between two
extractions of the SAME event (list position, case, accents, whitespace),
while still changing for a genuinely different event.
"""
from datetime import datetime, timezone

from app.services.event_identity import compute_source_event_key


def _dt(day: int) -> datetime:
    return datetime(2026, 8, day, 20, 0, tzinfo=timezone.utc)


class TestStability:
    def test_same_title_and_date_produce_the_same_key(self):
        a = compute_source_event_key("O Homem do Fraque Verde", _dt(5))
        b = compute_source_event_key("O Homem do Fraque Verde", _dt(5))
        assert a == b

    def test_key_is_unaffected_by_list_position(self):
        """The whole point of the plan: position is NEVER an input to the
        key. Calling it twice with identical (title, date) — as would happen
        whether the event was first or third in the model's list — must
        yield the identical key."""
        first_call = compute_source_event_key("Adilson Ramos", _dt(5))
        third_call = compute_source_event_key("Adilson Ramos", _dt(5))
        assert first_call == third_call

    def test_key_is_unaffected_by_case(self):
        a = compute_source_event_key("Khrystal", _dt(5))
        b = compute_source_event_key("khrystal", _dt(5))
        c = compute_source_event_key("KHRYSTAL", _dt(5))
        assert a == b == c

    def test_key_is_unaffected_by_accents(self):
        a = compute_source_event_key("Quarta Sertaneja", _dt(5))
        b = compute_source_event_key("Quárta Sertáneja", _dt(5))
        assert a == b

    def test_key_is_unaffected_by_surrounding_or_repeated_whitespace(self):
        a = compute_source_event_key("O Homem do Fraque Verde", _dt(5))
        b = compute_source_event_key("  O Homem do Fraque Verde  ", _dt(5))
        c = compute_source_event_key("O Homem  do   Fraque Verde", _dt(5))
        assert a == b == c

    def test_key_ignores_the_clock_time_component(self):
        """The same event re-extracted with an unstated time (defaulted to
        midnight) one run and a stated time read on a later run must still
        resolve to the same key, or a later, more precise reading would
        orphan the operator's confirmation of the original row."""
        midnight = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)
        evening = datetime(2026, 8, 5, 22, 0, tzinfo=timezone.utc)
        assert compute_source_event_key("Festa", midnight) == compute_source_event_key("Festa", evening)


class TestDistinctness:
    def test_different_titles_on_the_same_date_produce_different_keys(self):
        a = compute_source_event_key("O Homem do Fraque Verde", _dt(5))
        b = compute_source_event_key("Adilson Ramos", _dt(5))
        c = compute_source_event_key("Khrystal", _dt(5))
        assert len({a, b, c}) == 3

    def test_same_title_on_different_dates_produce_different_keys(self):
        a = compute_source_event_key("Festa Semanal", _dt(5))
        b = compute_source_event_key("Festa Semanal", _dt(12))
        assert a != b


class TestMissingData:
    def test_no_date_still_produces_a_deterministic_key(self):
        a = compute_source_event_key("Festa Sem Data", None)
        b = compute_source_event_key("Festa Sem Data", None)
        assert a == b

    def test_no_title_still_produces_a_deterministic_key(self):
        a = compute_source_event_key(None, _dt(5))
        b = compute_source_event_key(None, _dt(5))
        assert a == b

    def test_key_is_a_non_empty_string(self):
        key = compute_source_event_key("Festa", _dt(5))
        assert isinstance(key, str) and key
