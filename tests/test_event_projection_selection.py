"""Unit coverage for app.services.event_projection_selection.is_selectable
(plans/260905_events-serving-projection.md, Phase 4 pytest list: "Projection
selection predicate, as a pure function over rows").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.event_projection_selection import is_selectable

NOW = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)


def _row(**overrides) -> dict:
    base = {
        "event_id": "e1", "post_type": "event", "status": "accepted",
        "venue_id": "v1", "superseded_by": None,
        "starts_at": NOW + timedelta(hours=1), "is_recurring": False,
    }
    base.update(overrides)
    return base


def test_an_ordinary_accepted_future_event_is_selectable():
    assert is_selectable(_row(), now=NOW) is True


def test_confirmed_status_is_also_selectable():
    assert is_selectable(_row(status="confirmed"), now=NOW) is True


def test_pending_review_is_excluded():
    assert is_selectable(_row(status="pending_review"), now=NOW) is False


def test_rejected_is_excluded():
    assert is_selectable(_row(status="rejected"), now=NOW) is False


def test_extraction_failed_is_excluded():
    assert is_selectable(_row(status="extraction_failed"), now=NOW) is False


def test_superseded_row_is_excluded_even_if_otherwise_accepted():
    assert is_selectable(_row(superseded_by="other_event_id"), now=NOW) is False


def test_non_event_post_type_is_excluded():
    for post_type in ("menu", "promotion", "food", "other"):
        assert is_selectable(_row(post_type=post_type), now=NOW) is False


def test_no_venue_id_is_excluded():
    assert is_selectable(_row(venue_id=None), now=NOW) is False


def test_null_starts_at_is_selectable_regardless_of_recurring():
    """Expansion (app.services.event_occurrences) is what turns a null
    starts_at into zero occurrences; selection itself must not exclude it,
    or the "no occurrence at all" scenario could never even be reached."""
    assert is_selectable(_row(starts_at=None, is_recurring=False), now=NOW) is True
    assert is_selectable(_row(starts_at=None, is_recurring=True), now=NOW) is True


def test_a_night_that_started_yesterday_evening_is_still_selectable_at_01_00():
    started = NOW - timedelta(hours=20)  # "yesterday 22:00", now is "01:00"
    assert is_selectable(_row(starts_at=started, is_recurring=False), now=NOW) is True


def test_a_genuinely_past_one_off_event_is_excluded():
    started = NOW - timedelta(days=4)
    assert is_selectable(_row(starts_at=started, is_recurring=False), now=NOW) is False


def test_exactly_at_the_grace_boundary_is_still_included():
    started = NOW - timedelta(days=1)
    assert is_selectable(_row(starts_at=started, is_recurring=False), now=NOW) is True


def test_one_second_past_the_grace_boundary_is_excluded():
    started = NOW - timedelta(days=1, seconds=1)
    assert is_selectable(_row(starts_at=started, is_recurring=False), now=NOW) is False


def test_a_recurring_row_is_selectable_even_with_a_long_stale_starts_at():
    """The whole point of Phase 3's expansion: a recurring row's OWN
    starts_at can be months old and it must still be selected, since its
    concrete occurrences are re-derived from today forward."""
    long_stale = NOW - timedelta(days=200)
    assert is_selectable(_row(starts_at=long_stale, is_recurring=True), now=NOW) is True


def test_naive_starts_at_is_treated_as_utc():
    started = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    assert is_selectable(_row(starts_at=started, is_recurring=False), now=NOW) is True
