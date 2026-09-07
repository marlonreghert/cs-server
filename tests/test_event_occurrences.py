"""Unit coverage for app.services.event_occurrences (plans/260905_events-
serving-projection.md, Phase 3 pytest list): the expansion matrix, the
horizon boundary, clock-time preservation, and an exhaustive RFC 3986
safety check on generated occurrence ids — "no `#` anywhere" is precisely
the claim a handful of passing examples cannot establish, so that one is
checked over the whole character set, not a few samples.
"""
from __future__ import annotations

import string
from datetime import datetime, timedelta, timezone

import pytest

from app.services.event_date_resolver import RECIFE_TZ
from app.services.event_occurrences import (
    NIGHTLIFE_CUTOFF_HOUR,
    expand_occurrences,
    nightlife_date,
)

EVENT_ID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"  # 32 lowercase hex, matching event_identity
REFERENCE_TIME = datetime(2026, 9, 5, 18, 0, tzinfo=RECIFE_TZ)  # a Saturday


def _row(**overrides) -> dict:
    base = {
        "event_id": EVENT_ID,
        "starts_at": datetime(2026, 9, 6, 20, 0, tzinfo=RECIFE_TZ),
        "is_recurring": False,
        "recurrence_text": None,
    }
    base.update(overrides)
    return base


# ── one-off ──────────────────────────────────────────────────────────────
def test_one_off_event_produces_exactly_one_occurrence_with_the_bare_id():
    row = _row()
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert len(occ) == 1
    assert occ[0].occurrence_id == EVENT_ID
    assert occ[0].event_id == EVENT_ID
    assert occ[0].occurrence_date == "2026-09-06"


def test_one_off_starts_at_is_converted_to_utc():
    row = _row(starts_at=datetime(2026, 9, 6, 20, 0, tzinfo=RECIFE_TZ))
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert occ[0].starts_at.tzinfo is timezone.utc
    # Recife is UTC-3, no DST — 20:00 local is 23:00 UTC the same day.
    assert occ[0].starts_at == datetime(2026, 9, 6, 23, 0, tzinfo=timezone.utc)


def test_one_off_naive_starts_at_is_treated_as_utc():
    row = _row(starts_at=datetime(2026, 9, 6, 23, 0))  # naive
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert occ[0].starts_at == datetime(2026, 9, 6, 23, 0, tzinfo=timezone.utc)


# ── null starts_at ───────────────────────────────────────────────────────
def test_null_starts_at_produces_no_occurrences_even_when_recurring():
    row = _row(starts_at=None, is_recurring=True, recurrence_text="toda quinta")
    assert expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME) == []

    row2 = _row(starts_at=None, is_recurring=False)
    assert expand_occurrences(row2, horizon_days=21, reference_time=REFERENCE_TIME) == []


# ── weekly recurrence ────────────────────────────────────────────────────
def test_weekly_recurrence_expands_to_one_occurrence_per_matching_weekday():
    # "toda quinta" = every Thursday (weekday() == 3). The stored starts_at's
    # OWN DATE is deliberately stale (weeks in the past) — proving expansion
    # ignores it entirely and only keeps its clock time (21:00).
    row = _row(
        starts_at=datetime(2026, 8, 1, 21, 0, tzinfo=RECIFE_TZ),  # a long-past Saturday
        is_recurring=True,
        recurrence_text="toda quinta",
    )
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert occ  # non-empty: staleness of the stored date must not suppress expansion
    for o in occ:
        d = datetime.fromisoformat(o.occurrence_date)
        assert d.weekday() == 3, o.occurrence_date
        # clock time preserved on every generated day
        local = o.starts_at.astimezone(RECIFE_TZ)
        assert (local.hour, local.minute) == (21, 0)
        assert o.event_id == EVENT_ID
        assert o.occurrence_id == f"{EVENT_ID}_{o.occurrence_date}"


def test_weekly_recurrence_ids_are_unique_and_dated():
    row = _row(is_recurring=True, recurrence_text="toda quinta")
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    ids = [o.occurrence_id for o in occ]
    assert len(ids) == len(set(ids))
    dates = [o.occurrence_date for o in occ]
    assert dates == sorted(dates)


# ── daily / weekend cadence ──────────────────────────────────────────────
def test_daily_recurrence_expands_to_one_occurrence_per_day_in_the_horizon():
    row = _row(is_recurring=True, recurrence_text="todo dia")
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert len(occ) == 22  # inclusive both ends: today .. today+21


def test_weekend_recurrence_expands_to_saturdays_and_sundays_only():
    row = _row(is_recurring=True, recurrence_text="todo fim de semana")
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert occ
    for o in occ:
        d = datetime.fromisoformat(o.occurrence_date)
        assert d.weekday() in (5, 6)


# ── no computable day ────────────────────────────────────────────────────
@pytest.mark.parametrize("phrase", ["toda semana", "sempre"])
def test_no_computable_day_recurrence_yields_a_single_occurrence(phrase):
    row = _row(is_recurring=True, recurrence_text=phrase)
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert len(occ) == 1
    assert occ[0].occurrence_id == EVENT_ID  # the bare id — same shape as one-off


def test_unrecognised_recurrence_prose_also_yields_a_single_occurrence():
    """Recurring=True with prose this module simply does not parse is NOT
    the same state as "toda semana"/"sempre" internally, but it produces the
    identical externally-observable result: invent no days."""
    row = _row(is_recurring=True, recurrence_text="alguma coisa não reconhecida")
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert len(occ) == 1
    assert occ[0].occurrence_id == EVENT_ID


# ── horizon boundary ─────────────────────────────────────────────────────
def test_horizon_is_inclusive_of_today_and_the_final_day():
    # A daily cadence makes every day in [today, today+horizon] concrete and
    # counts trivially, so the boundary is checked directly on the produced
    # dates rather than inferred from a count.
    row = _row(is_recurring=True, recurrence_text="todo dia")
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    today = REFERENCE_TIME.date()
    dates = {datetime.fromisoformat(o.occurrence_date).date() for o in occ}
    assert today in dates
    assert today + timedelta(days=21) in dates
    assert today + timedelta(days=22) not in dates
    assert today - timedelta(days=1) not in dates


def test_zero_horizon_yields_at_most_todays_occurrence():
    row = _row(is_recurring=True, recurrence_text="todo dia")
    occ = expand_occurrences(row, horizon_days=0, reference_time=REFERENCE_TIME)
    assert len(occ) == 1
    assert occ[0].occurrence_date == REFERENCE_TIME.date().isoformat()


# ── occurrence id: exhaustive RFC 3986 safety ────────────────────────────
# RFC 3986 unreserved: ALPHA / DIGIT / "-" / "." / "_" / "~". Everything else
# (incl. every gen-delims/sub-delims character, "#" foremost) is reserved or
# otherwise not guaranteed to survive naive concatenation into a URL path.
_UNRESERVED = set(string.ascii_letters + string.digits + "-._~")


def test_recurring_occurrence_id_contains_no_uri_reserved_character():
    row = _row(is_recurring=True, recurrence_text="todo dia")
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert occ
    for o in occ:
        bad = set(o.occurrence_id) - _UNRESERVED
        assert not bad, f"{o.occurrence_id!r} contains reserved char(s): {bad}"
        assert "#" not in o.occurrence_id


def test_one_off_and_no_computable_day_ids_also_stay_unreserved():
    for row in (
        _row(),
        _row(is_recurring=True, recurrence_text="toda semana"),
    ):
        occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
        bad = set(occ[0].occurrence_id) - _UNRESERVED
        assert not bad


def test_occurrence_id_round_trips_through_a_real_url_unchanged():
    """The concrete failure mode the plan's Data section names at length: a
    spec-compliant client building `.../events/<occurrence_id>` by
    concatenation must recover the EXACT same id it started with — proven
    here by actually building a URL and parsing it back, not by reasoning
    about the character set alone."""
    from urllib.parse import urlparse

    row = _row(is_recurring=True, recurrence_text="toda quinta")
    occ = expand_occurrences(row, horizon_days=21, reference_time=REFERENCE_TIME)
    assert occ
    for o in occ:
        url = f"https://api.example.com/events/{o.occurrence_id}"
        parsed = urlparse(url)
        rebuilt_id = parsed.path.rsplit("/", 1)[-1]
        assert rebuilt_id == o.occurrence_id
        assert parsed.fragment == ""  # nothing was ever dropped after a "#"


# ══════════════════════════════════════════════════════════════════════════
# the nightlife day (plans/260906_events-venue-bairro-and-ticket-url.md §4)
# ══════════════════════════════════════════════════════════════════════════
class TestNightlifeDate:
    """`nightlife_date` alone, at the strict-`<` boundary vibes_bot uses.
    The two repos must agree at 05:59:59 and at 06:00:00 or the projected
    window and the served window disagree for one second every night."""

    @pytest.mark.parametrize("hour,minute,expected", [
        (0, 0, "2026-09-09"),
        (3, 30, "2026-09-09"),
        (5, 59, "2026-09-09"),
        (6, 0, "2026-09-10"),
        (6, 1, "2026-09-10"),
        (12, 0, "2026-09-10"),
        (23, 59, "2026-09-10"),
    ])
    def test_the_cutoff_boundary(self, hour, minute, expected):
        reference = datetime(2026, 9, 10, hour, minute, tzinfo=RECIFE_TZ)
        assert nightlife_date(reference).isoformat() == expected

    def test_the_cutoff_is_six(self):
        """Frozen cross-repo: vibes_bot's EVENTS_NIGHTLIFE_CUTOFF_HOUR and
        mobile's RECIFE_NIGHTLIFE_CUTOFF_HOUR must equal this. Changing it
        is a coordinated three-repo release, so it gets an explicit test
        rather than only being asserted through behaviour."""
        assert NIGHTLIFE_CUTOFF_HOUR == 6

    def test_it_reads_the_recife_local_hour_not_utc(self):
        """03:00 UTC is 00:00 in Recife (UTC-3), which is BEFORE the cutoff
        — reading the UTC hour instead would roll back a day too late."""
        reference = datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)
        assert nightlife_date(reference).isoformat() == "2026-09-09"

    def test_a_naive_reference_time_is_treated_as_utc(self):
        reference = datetime(2026, 9, 10, 3, 0)
        assert nightlife_date(reference).isoformat() == "2026-09-09"


class TestNightlifeExpansion:
    """The two bounds move INDEPENDENTLY: the near edge follows the
    nightlife day, the forward edge stays anchored to the calendar date."""

    def _daily(self, reference):
        row = _row(is_recurring=True, recurrence_text="todo dia",
                   starts_at=datetime(2026, 7, 1, 22, 0, tzinfo=RECIFE_TZ))
        return [o.occurrence_date for o in expand_occurrences(
            row, horizon_days=21, reference_time=reference,
        )]

    def test_before_the_cutoff_the_expansion_starts_on_yesterday(self):
        dates = self._daily(datetime(2026, 9, 10, 0, 30, tzinfo=RECIFE_TZ))
        assert dates[0] == "2026-09-09"

    def test_at_the_cutoff_the_expansion_starts_on_today(self):
        dates = self._daily(datetime(2026, 9, 10, 6, 0, tzinfo=RECIFE_TZ))
        assert dates[0] == "2026-09-10"

    def test_the_last_second_before_the_cutoff_still_starts_on_yesterday(self):
        dates = self._daily(datetime(2026, 9, 10, 5, 59, tzinfo=RECIFE_TZ))
        assert dates[0] == "2026-09-09"

    @pytest.mark.parametrize("hour,minute", [(0, 30), (5, 59), (6, 0), (18, 0)])
    def test_the_forward_edge_is_the_calendar_date_plus_the_horizon(self, hour, minute):
        """The deliberate deviation from F01's literal prescription: rolling
        the FORWARD edge back too would delete and re-create every
        horizon-edge occurrence once a night. Same last day at every hour."""
        dates = self._daily(datetime(2026, 9, 10, hour, minute, tzinfo=RECIFE_TZ))
        assert dates[-1] == "2026-10-01"  # calendar 2026-09-10 + 21 days

    def test_the_rolled_back_window_is_exactly_one_day_wider(self):
        before = self._daily(datetime(2026, 9, 10, 0, 30, tzinfo=RECIFE_TZ))
        after = self._daily(datetime(2026, 9, 10, 6, 30, tzinfo=RECIFE_TZ))
        assert len(before) == len(after) + 1
        assert before[1:] == after

    def test_a_weekday_recurrence_only_gains_the_extra_day_when_it_matches(self):
        """2026-09-09 is a Wednesday. A "toda quarta" event gains it at
        00:30; a "toda quinta" event does not, because the near edge only
        WIDENS the interval — it never forces a non-matching day in."""
        reference = datetime(2026, 9, 10, 0, 30, tzinfo=RECIFE_TZ)
        wednesday = _row(is_recurring=True, recurrence_text="toda quarta",
                         starts_at=datetime(2026, 7, 1, 22, 0, tzinfo=RECIFE_TZ))
        thursday = _row(is_recurring=True, recurrence_text="toda quinta",
                        starts_at=datetime(2026, 7, 1, 22, 0, tzinfo=RECIFE_TZ))
        wed_dates = [o.occurrence_date for o in expand_occurrences(
            wednesday, horizon_days=21, reference_time=reference)]
        thu_dates = [o.occurrence_date for o in expand_occurrences(
            thursday, horizon_days=21, reference_time=reference)]
        assert wed_dates[0] == "2026-09-09"
        assert thu_dates[0] == "2026-09-10"

    def test_the_rolled_back_occurrence_keeps_its_own_start_time(self):
        """Last night's 22:00 party is scored by 2026-09-09 22:00 Recife,
        not by anything derived from `reference_time` — that score is what
        vibes_bot's widened ZRANGEBYSCORE window matches on."""
        reference = datetime(2026, 9, 10, 0, 30, tzinfo=RECIFE_TZ)
        row = _row(is_recurring=True, recurrence_text="toda quarta",
                   starts_at=datetime(2026, 7, 1, 22, 0, tzinfo=RECIFE_TZ))
        first = expand_occurrences(row, horizon_days=21, reference_time=reference)[0]
        assert first.occurrence_date == "2026-09-09"
        assert first.starts_at == datetime(
            2026, 9, 9, 22, 0, tzinfo=RECIFE_TZ
        ).astimezone(timezone.utc)
        assert first.occurrence_id == f"{EVENT_ID}_2026-09-09"

    def test_a_non_recurring_row_is_untouched_by_the_nightlife_day(self):
        """The one-off path never consults `reference_time` at all — this
        change is additive to the shipped PAST_GRACE behaviour, not a
        replacement for it."""
        row = _row(starts_at=datetime(2026, 9, 9, 22, 0, tzinfo=RECIFE_TZ))
        for hour in (0, 6, 18):
            reference = datetime(2026, 9, 10, hour, 30, tzinfo=RECIFE_TZ)
            occ = expand_occurrences(row, horizon_days=21, reference_time=reference)
            assert len(occ) == 1
            assert occ[0].occurrence_date == "2026-09-09"
            assert occ[0].occurrence_id == EVENT_ID
