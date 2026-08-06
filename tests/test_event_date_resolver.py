"""Unit tests for app/services/event_date_resolver.py.

This is the highest-risk logic in plans/260804_instagram-event-extraction.md:
relative expressions must resolve against the POST's timestamp, never the run
clock, dates are parsed day-first, a missing year rolls forward to the next
occurrence at or after the post date, and an unparseable/ambiguous date must
NEVER be guessed — it resolves to None plus a review reason.

Written before app/services/event_date_resolver.py exists (true red: this
whole module fails to import until the resolver is implemented).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.event_date_resolver import (
    REASON_MISSING_DATE,
    RECIFE_TZ,
    resolve_event_datetime,
)

RECIFE = ZoneInfo("America/Recife")


def _post_at(year, month, day, hour=20, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=RECIFE)


# ── relative expressions resolve against the POST timestamp ─────────────────
class TestRelativeAgainstPostTimestamp:
    def test_amanha_resolves_to_the_day_after_the_post_not_the_run(self):
        # Post published Thursday 2026-07-16. Run happens three weeks later
        # (run clock deliberately irrelevant to this call: resolve_event_datetime
        # never receives "now", only the post timestamp).
        post_ts = _post_at(2026, 7, 16)  # Thursday
        resolved = resolve_event_datetime(
            date_text="amanhã", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None
        assert resolved.starts_at.date().isoformat() == "2026-07-17"  # Friday

    def test_hoje_resolves_to_the_post_date(self):
        post_ts = _post_at(2026, 7, 16, hour=9)
        resolved = resolve_event_datetime(
            date_text="hoje", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2026-07-16"

    def test_weekday_alone_resolves_to_next_occurrence_on_or_after_post_date(self):
        # Post on a Wednesday (2026-07-15), "sábado" -> the following Saturday.
        post_ts = _post_at(2026, 7, 15)
        resolved = resolve_event_datetime(
            date_text="este sábado", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2026-07-18"


# ── day-first parsing ─────────────────────────────────────────────────────
class TestDayFirstParsing:
    def test_numeric_date_is_day_first_not_month_first(self):
        # "05/08" must be 5 August, never 5 May.
        post_ts = _post_at(2026, 1, 1)
        resolved = resolve_event_datetime(
            date_text="05/08", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.month == 8
        assert resolved.starts_at.day == 5

    def test_every_day_at_or_below_twelve_is_still_day_first(self):
        # The dangerous band: an American reader would read "03/07" as March 7.
        post_ts = _post_at(2026, 1, 1)
        resolved = resolve_event_datetime(
            date_text="03/07", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.day == 3
        assert resolved.starts_at.month == 7


# ── missing year rolls forward to the next occurrence at/after the post ─────
class TestMissingYearForwardResolution:
    def test_no_year_within_the_same_year_stays_in_that_year(self):
        post_ts = _post_at(2026, 7, 1)  # July 2026
        resolved = resolve_event_datetime(
            date_text="15/08", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2026-08-15"

    def test_no_year_across_a_year_boundary_rolls_to_next_year(self):
        post_ts = _post_at(2026, 12, 1)  # December 2026
        resolved = resolve_event_datetime(
            date_text="15/08", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2027-08-15"

    def test_explicit_year_is_never_bumped(self):
        post_ts = _post_at(2026, 12, 1)
        resolved = resolve_event_datetime(
            date_text="15/08/2026", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2026-08-15"


# ── times: 22h == 22:00 == 10pm, plus ranges ─────────────────────────────────
class TestTimeParsing:
    @pytest.mark.parametrize("time_text", ["22h", "22:00", "10pm", "10 pm"])
    def test_time_variants_agree(self, time_text):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="hoje", time_text=time_text, post_timestamp=post_ts,
        )
        assert resolved.starts_at.hour == 22
        assert resolved.starts_at.minute == 0

    def test_range_sets_start_and_end_across_midnight(self):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="hoje", time_text="22h às 04h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.hour == 22
        assert resolved.ends_at is not None
        assert resolved.ends_at.hour == 4
        assert resolved.ends_at.date() > resolved.starts_at.date()


# ── timezone: America/Recife, stored as an aware instant ────────────────────
class TestTimezone:
    def test_start_is_2200_america_recife(self):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="hoje", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.tzinfo is not None
        assert resolved.starts_at.utcoffset() == RECIFE_TZ.utcoffset(resolved.starts_at)
        # Recife is UTC-3, no DST since 2019.
        assert resolved.starts_at.astimezone(ZoneInfo("UTC")).hour == 1  # 22h -03:00 -> 01h UTC next day


# ── recurrence ────────────────────────────────────────────────────────────
class TestRecurrence:
    def test_toda_quinta_is_recurring_with_next_thursday_as_start(self):
        # Post published on a Monday (2026-07-13).
        post_ts = _post_at(2026, 7, 13)
        resolved = resolve_event_datetime(
            date_text="toda quinta", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.is_recurring is True
        assert resolved.recurrence_text == "toda quinta"
        assert resolved.starts_at.date().isoformat() == "2026-07-16"  # Thursday

    def test_recurring_post_made_on_the_recurring_weekday_itself(self):
        # Post published ON a Thursday, announcing "toda quinta" — the next
        # occurrence is that same day, not a week later.
        post_ts = _post_at(2026, 7, 16)  # Thursday
        resolved = resolve_event_datetime(
            date_text="toda quinta", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.is_recurring is True
        assert resolved.starts_at.date().isoformat() == "2026-07-16"


# ── never invent a date ──────────────────────────────────────────────────────
class TestNeverInventADate:
    @pytest.mark.parametrize(
        "date_text",
        [None, "", "em breve", "data a definir", "não sei quando"],
    )
    def test_unparseable_or_absent_date_resolves_to_none_and_flags_review(self, date_text):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text=date_text, time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at is None
        assert resolved.needs_review is True
        assert resolved.review_reason == REASON_MISSING_DATE

    def test_time_with_no_date_stores_no_start_time(self):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at is None
        assert resolved.needs_review is True

    def test_review_reason_names_the_missing_date(self):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="em breve", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.review_reason is not None
        assert "date" in resolved.review_reason.lower()


# ── time_known: a stated midnight is not the same fact as a defaulted one ────
class TestTimeKnown:
    """plans/260806_instagram-post-recency-and-unknown-time.md: an event whose
    time could not be read must not be indistinguishable from one that
    genuinely never stated a time. `time_known` is the signal; the trap is
    that a stated "00h" and a defaulted midnight land on the exact same
    `starts_at` instant, so only checking the PARSE RESULT (not the hour
    value, which is falsy at midnight regardless) tells them apart.
    """

    def test_a_parsed_time_is_known(self):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="hoje", time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.time_known is True

    def test_an_absent_time_defaults_to_midnight_and_is_not_known(self):
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="hoje", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at.hour == 0
        assert resolved.starts_at.minute == 0
        assert resolved.time_known is False

    def test_a_stated_00h_is_known_even_though_it_is_also_midnight(self):
        # THE TRAP: "00h" is a real stated midnight. It must report
        # time_known=True even though it resolves to the identical instant a
        # defaulted midnight would.
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="hoje", time_text="00h", post_timestamp=post_ts,
        )
        assert resolved.starts_at.hour == 0
        assert resolved.starts_at.minute == 0
        assert resolved.time_known is True

    def test_a_stated_00h_and_a_defaulted_midnight_are_the_same_instant_but_different_facts(self):
        post_ts = _post_at(2026, 7, 1)
        stated = resolve_event_datetime(
            date_text="hoje", time_text="00h", post_timestamp=post_ts,
        )
        defaulted = resolve_event_datetime(
            date_text="hoje", time_text=None, post_timestamp=post_ts,
        )
        assert stated.starts_at == defaulted.starts_at
        assert stated.time_known is True
        assert defaulted.time_known is False

    def test_a_missing_date_reports_time_unknown(self):
        # No date resolved at all -> starts_at is None; time_known must not
        # be left uninitialized/true by accident.
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=post_ts,
        )
        assert resolved.starts_at is None
        assert resolved.time_known is False
