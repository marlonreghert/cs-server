"""Unit coverage for app.services.event_projection_selection.is_selectable
(plans/260905_events-serving-projection.md, Phase 4 pytest list: "Projection
selection predicate, as a pure function over rows").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.event_projection_selection import (
    DEFAULT_RECURRING_MAX_SOURCE_AGE,
    is_selectable,
)

NOW = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)


def _row(**overrides) -> dict:
    base = {
        "event_id": "e1", "post_type": "event", "status": "accepted",
        "venue_id": "v1", "superseded_by": None,
        "starts_at": NOW + timedelta(hours=1), "is_recurring": False,
        # A recently-seen source by default: every PRE-EXISTING test in this
        # file predates the source-freshness bound and must keep passing
        # unchanged (none of them is about last_seen_at) — only the tests
        # below that explicitly override it exercise that dimension.
        "last_seen_at": NOW,
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


# ── recurring-event source-freshness bound (the phantom-event fix) ─────────
# Every fixture below uses is_recurring=True with a long-stale starts_at (as
# test_a_recurring_row_is_selectable_even_with_a_long_stale_starts_at already
# proves that alone can never exclude a recurring row) so ONLY last_seen_at
# is ever the thing under test.
_STALE_STARTS_AT = NOW - timedelta(days=200)


def test_a_recurring_row_whose_sources_have_gone_stale_is_excluded():
    """The defect this bound exists to fix: a "toda quinta" post whose venue
    stopped running that night long ago, and that has not been re-crawled
    since, must eventually stop being projected."""
    stale_seen = NOW - timedelta(days=90)
    row = _row(starts_at=_STALE_STARTS_AT, is_recurring=True, last_seen_at=stale_seen)
    assert is_selectable(row, now=NOW) is False


def test_a_recurring_row_with_a_recently_seen_source_is_selectable():
    fresh_seen = NOW - timedelta(days=10)
    row = _row(starts_at=_STALE_STARTS_AT, is_recurring=True, last_seen_at=fresh_seen)
    assert is_selectable(row, now=NOW) is True


def test_recurring_source_age_exactly_at_the_boundary_is_still_included():
    seen = NOW - DEFAULT_RECURRING_MAX_SOURCE_AGE
    row = _row(starts_at=_STALE_STARTS_AT, is_recurring=True, last_seen_at=seen)
    assert is_selectable(row, now=NOW) is True


def test_recurring_source_age_one_second_past_the_boundary_is_excluded():
    seen = NOW - DEFAULT_RECURRING_MAX_SOURCE_AGE - timedelta(seconds=1)
    row = _row(starts_at=_STALE_STARTS_AT, is_recurring=True, last_seen_at=seen)
    assert is_selectable(row, now=NOW) is False


def test_a_recurring_row_with_no_source_rows_at_all_is_excluded():
    """`last_seen_at is None` means NO `post_item_source` row was ever
    attached (should not occur in practice — every event is created FROM a
    source — but never assumed, per is_selectable's own docstring). Fails
    toward honest: no evidence this announcement was ever confirmed at all
    is treated as maximally stale, not as a free pass — mirrors
    app.models.menu_lifecycle.is_menu_item_current's identical NULL
    handling for the same shape of question."""
    row = _row(starts_at=_STALE_STARTS_AT, is_recurring=True, last_seen_at=None)
    assert is_selectable(row, now=NOW) is False


def test_recurring_bound_honours_a_caller_supplied_window():
    """The window is a parameter, not a hardcoded constant — a caller (both
    stores' list_events_for_projection) threads the LIVE
    `settings.events_recurring_max_source_age_days` through explicitly
    rather than relying on this module's own default."""
    seen = NOW - timedelta(days=10)
    row = _row(starts_at=_STALE_STARTS_AT, is_recurring=True, last_seen_at=seen)
    assert is_selectable(row, now=NOW, max_recurring_source_age=timedelta(days=5)) is False
    assert is_selectable(row, now=NOW, max_recurring_source_age=timedelta(days=30)) is True


def test_non_recurring_row_behaviour_is_unchanged_by_the_source_freshness_bound():
    """The bound applies ONLY to recurring rows: a non-recurring row with a
    future starts_at is selectable regardless of how stale (or absent) its
    last_seen_at is — the exact same outcome as before this change, proving
    last_seen_at is never even consulted on this branch."""
    row = _row(is_recurring=False, last_seen_at=None)  # starts_at defaults to NOW+1h
    assert is_selectable(row, now=NOW) is True
    very_stale_seen = NOW - timedelta(days=400)
    row = _row(is_recurring=False, last_seen_at=very_stale_seen)
    assert is_selectable(row, now=NOW) is True
