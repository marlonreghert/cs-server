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
    REASON_DATE_RANGE,
    REASON_MISSING_DATE,
    REASON_WEEKDAY_MISMATCH,
    REASON_YEAR_INFERRED,
    RECIFE_TZ,
    resolve_event_datetime,
    vote_on_sibling_years,
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


# plans/260810_post-kind-and-post-extraction-attribution.md §D: recurrence
# read from the MODEL's own is_recurring/recurrence_text, extended beyond
# toda/todo to a weekday range, a list, and a bare plural — all gated on
# is_recurring=True so the narrowed one-off weekday path
# (260807_date-resolution-correctness.md) is never re-widened.
class TestRecurrenceReadFromTheModel:
    _MONDAY = _post_at(2026, 7, 13)  # 2026-07-13 IS a Monday

    def test_weekday_range_recurs_from_the_anchor_itself(self):
        # The post's own Monday anchor already falls inside "segunda a
        # sexta" -> the next occurrence is TODAY, not a week out.
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=True, recurrence_text="de segunda a sexta",
        )
        assert resolved.is_recurring is True
        assert resolved.needs_review is False
        assert resolved.starts_at.date().isoformat() == "2026-07-13"
        assert resolved.recurrence_text == "de segunda a sexta"

    def test_weekday_range_from_a_weekend_anchor_rolls_to_monday(self):
        saturday = _post_at(2026, 7, 18)
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=saturday,
            is_recurring=True, recurrence_text="de sexta a sábado",
        )
        # "de sexta a sábado" -> {Friday, Saturday}; anchor IS Saturday, so
        # the next matching day is the anchor itself.
        assert resolved.starts_at.date().isoformat() == "2026-07-18"
        assert resolved.needs_review is False

    def test_weekday_list_resolves_to_the_next_named_day(self):
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=True, recurrence_text="sextas e sábados",
        )
        assert resolved.is_recurring is True
        assert resolved.needs_review is False
        assert resolved.starts_at.date().isoformat() == "2026-07-17"  # next Friday

    def test_todas_as_sextas_resolves_like_the_classic_marker_form(self):
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=True, recurrence_text="todas as sextas",
        )
        assert resolved.starts_at.date().isoformat() == "2026-07-17"
        assert resolved.needs_review is False

    def test_bare_plural_sextas_resolves_when_is_recurring_true(self):
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=True, recurrence_text="sextas",
        )
        assert resolved.starts_at.date().isoformat() == "2026-07-17"
        assert resolved.needs_review is False

    def test_unparseable_recurrence_phrase_keeps_todays_behaviour(self):
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=True, recurrence_text="semanalmente",
        )
        assert resolved.starts_at is None
        assert resolved.needs_review is True
        assert resolved.review_reason == REASON_MISSING_DATE

    def test_prose_with_no_recurrence_is_not_detected(self):
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=True, recurrence_text="Ingressos abertos, venha conferir!",
        )
        assert resolved.is_recurring is False
        assert resolved.starts_at is None
        assert resolved.needs_review is True

    def test_a_one_off_saturday_with_is_recurring_false_is_never_treated_as_recurring(self):
        """The load-bearing guard: a bare weekday with is_recurring=False
        must resolve through the OLD one-off path unchanged, never through
        any new recurrence form — 260807_date-resolution-correctness.md
        narrowed this on purpose and this must not re-open it."""
        resolved = resolve_event_datetime(
            date_text="sábado", time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=False, recurrence_text=None,
        )
        assert resolved.is_recurring is False
        assert resolved.starts_at is not None
        assert resolved.starts_at.date().weekday() == 5  # Saturday
        assert resolved.needs_review is False

    def test_new_forms_are_never_applied_when_is_recurring_is_not_supplied(self):
        """Every pre-existing caller/test in this file calls with only
        date_text (is_recurring defaulting to None) — a range/list phrase
        sitting in date_text itself must NOT be picked up unless is_recurring
        is explicitly True, or the byte-for-byte-unchanged guarantee for
        legacy callers would be broken."""
        resolved = resolve_event_datetime(
            date_text="de segunda a sexta", time_text="22h", post_timestamp=self._MONDAY,
        )
        assert resolved.is_recurring is False

    def test_a_recurring_event_with_a_resolvable_schedule_is_never_flagged_missing_date(self):
        resolved = resolve_event_datetime(
            date_text=None, time_text="22h", post_timestamp=self._MONDAY,
            is_recurring=True, recurrence_text="de segunda a sexta",
        )
        assert resolved.review_reason is None
        assert resolved.needs_review is False


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


# ── plans/260807_date-resolution-correctness.md ──────────────────────────────
# Defect 1: pt-BR month abbreviations, verbatim from the plan's Evidence
# section — a future edit that "simplifies" _MONTHS/_TEXTUAL_DATE_RE has to
# argue with a specific row, not just a diff.
class TestMonthAbbreviationsResolveVerbatim:
    _ANCHOR = _post_at(2026, 8, 3)  # a Monday; "5 SET"/etc all roll to 5 Sep

    @pytest.mark.parametrize(
        "date_text",
        ["05/SET", "05/set", "5 SET", "SET 05"],
        ids=["05_SET", "05_set_lower", "5_space_SET", "SET_space_05"],
    )
    def test_day_and_abbreviated_month_resolve_to_5_september(self, date_text):
        resolved = resolve_event_datetime(
            date_text=date_text, time_text=None, post_timestamp=self._ANCHOR,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-09-05", resolved.starts_at

    def test_08_ago_resolves_to_8_august(self):
        # Distinct row from the table above: a different day AND a different
        # month abbreviation, so this cannot pass by coincidentally reusing
        # the same hardcoded expectation.
        resolved = resolve_event_datetime(
            date_text="08/Ago", time_text=None, post_timestamp=self._ANCHOR,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-08-08", resolved.starts_at

    @pytest.mark.parametrize("date_text", ["05/09", "05.09", "05-09", "05/09/26"])
    def test_pre_existing_numeric_forms_are_unaffected(self, date_text):
        resolved = resolve_event_datetime(
            date_text=date_text, time_text=None, post_timestamp=self._ANCHOR,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-09-05", resolved.starts_at

    def test_full_month_name_form_is_unaffected(self):
        resolved = resolve_event_datetime(
            date_text="5 de setembro", time_text=None, post_timestamp=self._ANCHOR,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-09-05", resolved.starts_at

    @pytest.mark.parametrize("date_text", ["HOJE", "AMANHÃ", "toda quinta"])
    def test_relative_and_recurring_forms_are_unaffected(self, date_text):
        resolved = resolve_event_datetime(
            date_text=date_text, time_text=None, post_timestamp=self._ANCHOR,
        )
        assert resolved.starts_at is not None, resolved


class TestMonthAbbreviationNeverMatchesInsideProse:
    """The trap named in the plan: 'set', 'mai' and 'out' are ordinary
    Portuguese words. A bare 3-letter match with no adjacent day numeral must
    never invent a date out of sentence prose."""

    @pytest.mark.parametrize(
        "date_text",
        [
            "Em breve soltamos os detalhes completos, aguardem set chegar por aqui",
            "Não vou dar mai detalhes agora, aguardem a confirmação completa",
            "Isso já tava meio out, mano, mas o line-up completo ainda sai",
        ],
        ids=["set_in_prose", "mai_in_prose", "out_in_prose"],
    )
    def test_bare_abbreviation_word_in_prose_resolves_to_no_date(self, date_text):
        post_ts = _post_at(2026, 8, 1)
        resolved = resolve_event_datetime(
            date_text=date_text, time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is None, resolved
        assert resolved.needs_review is True, resolved


# Defect 2 (the load-bearing one): a weekday corroborates an explicit date,
# it never replaces one.
class TestWeekdayCorroboratesNeverReplaces:
    def test_explicit_date_and_agreeing_weekday_resolve_silently(self):
        # The operator's real case: 5 September 2026 IS a Saturday, so this
        # must resolve WITHOUT any flag — the weekday corroborates, it is
        # not merely tolerated.
        post_ts = _post_at(2026, 8, 3)  # Monday
        resolved = resolve_event_datetime(
            date_text="Sábado • 05/SET", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-09-05", resolved.starts_at
        assert resolved.needs_review is False, resolved
        assert resolved.review_reason is None, resolved

    def test_weekday_with_a_competing_numeral_never_wins(self):
        # THE dangerous case fixed here: pre-fix, this silently returned the
        # next Friday and dropped the "15" with no flag at all.
        post_ts = _post_at(2026, 8, 1)
        resolved = resolve_event_datetime(
            date_text="sexta 15", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is None, resolved
        assert resolved.needs_review is True, resolved
        assert resolved.review_reason == REASON_MISSING_DATE, resolved

    def test_the_339_day_case_resolves_to_no_date_and_flagged(self):
        """The worst observed instance (plan's Evidence section): an anchor
        in January, a flyer stating a December date the resolver cannot read
        (the month text is unparseable) beside a weekday. Pre-fix, the
        weekday fallback silently returned the next Sunday from the January
        anchor — 6 days out, not 339 — with no flag at all. Post-fix this
        must resolve to no date, not a plausible-looking nearby Sunday: the
        visible blank is the safe failure, not the confident wrong answer."""
        post_ts = _post_at(2026, 1, 5)
        resolved = resolve_event_datetime(
            date_text="Domingo • 20/DEZEMBRÃO", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is None, resolved
        assert resolved.needs_review is True, resolved
        assert resolved.review_reason == REASON_MISSING_DATE, resolved

    def test_guard_does_not_fire_for_a_bare_weekday_with_no_numeral(self):
        # Trap A: the guard is "numeral present AND unresolved", never
        # merely "weekday present" — a bare/qualified weekday with nothing
        # competing must keep resolving exactly as before this fix.
        post_ts = _post_at(2026, 8, 3)  # Monday
        resolved = resolve_event_datetime(
            date_text="este sábado", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().weekday() == 5, resolved.starts_at
        assert resolved.needs_review is False, resolved

    def test_recurrence_is_unaffected_even_with_a_competing_numeral(self):
        # Trap B: the recurrence branch runs BEFORE the new guard and must
        # never be dragged into it, even when a bare numeral sits right next
        # to the recurring weekday — adversarial input chosen specifically
        # to prove the guard cannot leak into this path.
        post_ts = _post_at(2026, 7, 13)  # Monday
        resolved = resolve_event_datetime(
            date_text="toda quinta dia 15", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.is_recurring is True, resolved
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-07-16", resolved.starts_at
        assert resolved.needs_review is False, resolved


# Defect 2b: disagreement is a third outcome — never silently trusted either
# way.
class TestWeekdayMismatchIsFlaggedNotTrusted:
    def test_disagreeing_weekday_is_flagged_and_explicit_date_wins(self):
        # 5 September 2026 is a Saturday, not a Sunday: a genuine flyer typo.
        post_ts = _post_at(2026, 8, 3)
        resolved = resolve_event_datetime(
            date_text="Domingo • 05/SET", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-09-05", resolved.starts_at
        assert resolved.needs_review is True, resolved
        assert resolved.review_reason == REASON_WEEKDAY_MISMATCH, resolved

    def test_defect_1_fix_also_closes_the_original_339_day_evidence(self):
        """Once "dez" is a parseable abbreviation, the plan's original
        measured bug case ("Domingo • 20/DEZ" from a 2026-01-05 anchor) is no
        longer a blank OR a mismatch: 20 December 2026 genuinely IS a Sunday,
        so the explicit date and the stated weekday agree and this resolves
        silently, correctly, to the true date — the 339-day miss it used to
        produce is simply gone."""
        post_ts = _post_at(2026, 1, 5)
        resolved = resolve_event_datetime(
            date_text="Domingo • 20/DEZ", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-12-20", resolved.starts_at
        assert resolved.needs_review is False, resolved
        assert resolved.review_reason is None, resolved


# plans/260810_date-correctness-review-reasons-and-path-parity.md §A: a
# rolled-forward year is a guess and must be flagged, never presented with
# the resolver's usual silent confidence.
class TestYearInferredFlag:
    def test_a_roll_across_the_year_boundary_is_flagged(self):
        post_ts = _post_at(2026, 12, 1)
        resolved = resolve_event_datetime(
            date_text="15/08", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2027-08-15", resolved
        assert resolved.year_inferred is True, resolved
        assert resolved.needs_review is True, resolved

    def test_a_date_within_the_same_year_is_not_flagged(self):
        # No roll happens here -- the year was never inferred by GUESSING,
        # only defaulted to the anchor's own year, which is not a guess.
        post_ts = _post_at(2026, 7, 1)
        resolved = resolve_event_datetime(
            date_text="15/08", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2026-08-15", resolved
        assert resolved.year_inferred is False, resolved
        assert resolved.needs_review is False, resolved

    def test_an_explicit_year_is_never_flagged_even_if_it_looks_like_a_typo(self):
        # An explicit year is trusted outright -- _roll_forward never even
        # runs for it (app.services.event_date_resolver._numeric_date).
        post_ts = _post_at(2026, 12, 1)
        resolved = resolve_event_datetime(
            date_text="15/08/2025", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2025-08-15", resolved
        assert resolved.year_inferred is False, resolved

    def test_recurrence_never_sets_year_inferred(self):
        # Trap: the recurrence path never reaches _resolve_explicit_date, so
        # it can never trip this flag either -- there is no year to guess,
        # only a weekday to find the next occurrence of.
        post_ts = _post_at(2026, 7, 13)  # Monday
        resolved = resolve_event_datetime(
            date_text="toda quinta", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.is_recurring is True, resolved
        assert resolved.year_inferred is False, resolved

    def test_a_bare_weekday_fallback_never_sets_year_inferred(self):
        # Trap: the one-off weekday path (plans/260807_date-resolution-
        # correctness.md) must survive this change untouched -- it resolves
        # via _next_weekday_on_or_after, never _roll_forward, so it can
        # never be flagged as a year guess.
        post_ts = _post_at(2026, 8, 3)  # Monday
        resolved = resolve_event_datetime(
            date_text="este sábado", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.year_inferred is False, resolved
        assert resolved.needs_review is False, resolved

    def test_weekday_mismatch_and_year_inference_are_independent_flags(self):
        # A rolled date whose stated weekday also disagrees: both signals
        # must survive side by side -- review_reason keeps carrying the
        # resolver's OWN weekday_mismatch reason (never overwritten by the
        # roll), while year_inferred is reported separately.
        # 15 August 2027 is a Sunday, not a Saturday -- a genuine mismatch.
        post_ts = _post_at(2026, 12, 1)
        resolved = resolve_event_datetime(
            date_text="Sábado • 15/08", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2027-08-15", resolved
        assert resolved.year_inferred is True, resolved
        assert resolved.review_reason == REASON_WEEKDAY_MISMATCH, resolved
        assert resolved.needs_review is True, resolved


# §B: a range keeps only its first day, and says a range existed.
class TestDateRangeYieldsFirstDay:
    def test_a_three_day_range_resolves_to_the_first_day(self):
        post_ts = _post_at(2026, 6, 1)
        resolved = resolve_event_datetime(
            date_text="01, 02 e 03 de julho", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-07-01", resolved
        assert resolved.date_range is True, resolved

    def test_the_theoretical_05_e_06_09_gap_now_resolves_to_the_first_day(self):
        # plans/260807_date-resolution-correctness.md §D named this gap and
        # deliberately deferred it; this plan closes it.
        post_ts = _post_at(2026, 1, 1)
        resolved = resolve_event_datetime(
            date_text="05 e 06/09", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at is not None, resolved
        assert resolved.starts_at.date().isoformat() == "2026-09-05", resolved
        assert resolved.date_range is True, resolved

    def test_a_single_date_is_never_flagged_as_a_range(self):
        post_ts = _post_at(2026, 1, 1)
        resolved = resolve_event_datetime(
            date_text="15/08", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.date_range is False, resolved

    def test_a_range_that_also_needs_to_roll_flags_both(self):
        # The real production case (plan Evidence §A/§B combined): a range
        # whose first day is still before the anchor rolls AND is a range.
        post_ts = _post_at(2026, 12, 1)
        resolved = resolve_event_datetime(
            date_text="01, 02 e 03 de julho", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.starts_at.date().isoformat() == "2027-07-01", resolved
        assert resolved.year_inferred is True, resolved
        assert resolved.date_range is True, resolved

    def test_a_bare_number_list_with_nothing_dated_after_it_is_not_a_range(self):
        # "3, 4 e 5 amigos chegam" -- a list of numbers with no month
        # attached at all must never be swept into range detection; it falls
        # through to the ordinary unparseable-date path instead.
        post_ts = _post_at(2026, 1, 1)
        resolved = resolve_event_datetime(
            date_text="3, 4 e 5 amigos chegam pra festa", time_text=None, post_timestamp=post_ts,
        )
        assert resolved.date_range is False, resolved


# §A: siblings from the SAME post vote on a rolled year.
class TestSiblingYearVoting:
    def test_ferias_amigos_park_the_real_production_case(self):
        """The real bug, with the anchor that actually matches the plan's
        Evidence section: "weeks 1 and 2 had already happened" (both, not
        just one) means the anchor sits strictly between week 2's last day
        (10 July) and week 3's first day (15 July) -- 13 July, say. That is
        a genuine 2-2 split by year (2027, 2027, 2026, 2026), a TIE by raw
        count, not a 3-1 majority. The fix must resolve it anyway: within
        July, the tie prefers the year the two NATIVE (never-rolled)
        siblings already hold."""
        post_ts = _post_at(2026, 7, 13)
        texts = [
            "01, 02 e 03 de julho",
            "08, 09 e 10 de julho",
            "15, 16 e 17 de julho",
            "22, 23 e 24 de julho",
        ]
        resolved = [
            resolve_event_datetime(date_text=t, time_text=None, post_timestamp=post_ts)
            for t in texts
        ]
        # Pre-vote: weeks 1-2 individually roll to 2027 (both before the 13
        # July anchor); weeks 3-4 resolve natively within 2026. A tie, 2-2.
        assert [r.starts_at.year for r in resolved] == [2027, 2027, 2026, 2026], resolved
        assert [r.year_inferred for r in resolved] == [True, True, False, False], resolved

        voted = vote_on_sibling_years(resolved)
        assert [r.starts_at.date().isoformat() for r in voted] == [
            "2026-07-01", "2026-07-08", "2026-07-15", "2026-07-22",
        ], voted
        # The two corrected weeks are still an inference, even corrected --
        # never auto-acceptable.
        assert voted[0].year_inferred is True and voted[0].needs_review is True, voted[0]
        assert voted[1].year_inferred is True and voted[1].needs_review is True, voted[1]
        # The two that were already correct are untouched, and never
        # flagged -- they were never a guess.
        assert voted[2].year_inferred is False, voted[2]
        assert voted[3].year_inferred is False, voted[3]

    def test_a_unique_majority_pulls_a_rolled_outlier_back(self):
        """A synthetic (non-evidence) fixture isolating the OTHER branch:
        a real, non-tied plurality -- three siblings already agree, one
        rolled outlier does not."""
        post_ts = _post_at(2026, 7, 5)
        texts = ["01/07", "08/07", "15/07", "22/07"]
        resolved = [
            resolve_event_datetime(date_text=t, time_text=None, post_timestamp=post_ts)
            for t in texts
        ]
        assert [r.starts_at.year for r in resolved] == [2027, 2026, 2026, 2026], resolved

        voted = vote_on_sibling_years(resolved)
        assert [r.starts_at.year for r in voted] == [2026, 2026, 2026, 2026], voted
        assert voted[0].year_inferred is True, voted[0]

    def test_a_genuine_single_date_post_is_completely_unaffected(self):
        post_ts = _post_at(2026, 12, 1)
        resolved = [resolve_event_datetime(
            date_text="15/08", time_text=None, post_timestamp=post_ts,
        )]
        voted = vote_on_sibling_years(resolved)
        assert voted == resolved, (voted, resolved)
        assert voted[0].starts_at.date().isoformat() == "2027-08-15", voted

    def test_different_months_within_one_post_never_vote_on_each_other(self):
        """The month gate's own boundary case, isolated from the New Year's
        scenario below: two dates in the SAME post but different months
        must never influence each other, even though (pre-fix) they would
        have looked like a 1-1 "tie" by raw count."""
        post_ts = _post_at(2026, 12, 1)
        rolled = resolve_event_datetime(
            date_text="15/08", time_text=None, post_timestamp=post_ts,
        )
        native = resolve_event_datetime(
            date_text="20/12", time_text=None, post_timestamp=post_ts,
        )
        assert rolled.starts_at.year == 2027 and native.starts_at.year == 2026
        voted = vote_on_sibling_years([rolled, native])
        assert voted[0].starts_at.year == 2027, voted
        assert voted[1].starts_at.year == 2026, voted
        assert voted[0].year_inferred is True, voted[0]

    def test_new_years_programme_december_and_january_never_vote_on_each_other(self):
        """The counter-example that keeps the month gate honest (raised
        directly against an earlier, ungated version of this rule): a
        programme spanning "27, 28, 29 de dezembro e 02, 03 de janeiro".
        The December dates resolve natively within the post's own year; the
        January dates CORRECTLY roll into the next one. A rule that let
        December's non-inferred majority "win" across the whole post would
        wrongly drag January back a year and break a case that already
        works today."""
        post_ts = _post_at(2026, 12, 20)
        december = ["27/12", "28/12", "29/12"]
        january = ["02/01", "03/01"]
        resolved = [
            resolve_event_datetime(date_text=t, time_text=None, post_timestamp=post_ts)
            for t in december + january
        ]
        assert [r.starts_at.year for r in resolved] == [2026, 2026, 2026, 2027, 2027], resolved
        assert [r.year_inferred for r in resolved] == [False, False, False, True, True], resolved

        voted = vote_on_sibling_years(resolved)
        # December: untouched (already agrees, nothing inferred there).
        assert [r.starts_at.year for r in voted[:3]] == [2026, 2026, 2026], voted
        # January: STILL rolls into 2027 -- December's majority never
        # reaches across the month boundary to correct it.
        assert [r.starts_at.year for r in voted[3:]] == [2027, 2027], voted
        assert voted[3].year_inferred is True and voted[4].year_inferred is True, voted

    def test_a_same_month_tie_between_two_trusted_answers_refuses_to_guess(self):
        """The genuinely ambiguous case within ONE month: two explicitly
        stated (non-inferred) years disagree with each other, and a third,
        bare date rolls to a THIRD year -- a 1-1-1 tie with no unique
        trusted answer to defer to. `_roll_forward` always rolls every
        inferred sibling in a post to the SAME target year, so the only way
        a same-month tie is genuinely unresolvable is exactly this: two (or
        more) DIFFERENT non-inferred years both present at once."""
        post_ts = _post_at(2026, 12, 1)
        explicit_2025 = resolve_event_datetime(
            date_text="20/08/2025", time_text=None, post_timestamp=post_ts,
        )
        explicit_2028 = resolve_event_datetime(
            date_text="22/08/2028", time_text=None, post_timestamp=post_ts,
        )
        bare_rolls_to_2027 = resolve_event_datetime(
            date_text="25/08", time_text=None, post_timestamp=post_ts,
        )
        assert [
            explicit_2025.starts_at.year, explicit_2028.starts_at.year,
            bare_rolls_to_2027.starts_at.year,
        ] == [2025, 2028, 2027]

        voted = vote_on_sibling_years([explicit_2025, explicit_2028, bare_rolls_to_2027])
        # Refused to guess: all three keep their own individually-resolved
        # year, including the one genuine guess among them.
        assert voted[0].starts_at.year == 2025, voted
        assert voted[1].starts_at.year == 2028, voted
        assert voted[2].starts_at.year == 2027, voted
        assert voted[2].year_inferred is True, voted[2]

    def test_voting_never_touches_a_non_inferred_outlier(self):
        # An explicitly-stated year that happens to disagree with its
        # siblings must NEVER be pulled into line -- only a GUESS is ever
        # eligible for correction. This is the boundary that keeps voting
        # from turning into "cross-post" behaviour smuggled into one post.
        post_ts = _post_at(2026, 12, 1)
        explicit_outlier = resolve_event_datetime(
            date_text="15/08/2030", time_text=None, post_timestamp=post_ts,
        )
        rolled_a = resolve_event_datetime(
            date_text="16/08", time_text=None, post_timestamp=post_ts,
        )
        rolled_b = resolve_event_datetime(
            date_text="17/08", time_text=None, post_timestamp=post_ts,
        )
        voted = vote_on_sibling_years([explicit_outlier, rolled_a, rolled_b])
        # The explicit 2030 date is untouched regardless of what its
        # siblings (both 2027) agree on.
        assert voted[0].starts_at.year == 2030, voted
        assert voted[0].year_inferred is False, voted[0]
        # The two genuine guesses agree with each other already -- nothing
        # to correct, both stay flagged.
        assert voted[1].starts_at.year == 2027, voted
        assert voted[2].starts_at.year == 2027, voted

    def test_voting_never_crosses_posts(self):
        # The hard boundary the plan calls out explicitly: an unrelated
        # event from a DIFFERENT post must never drag its year onto this
        # one, even if this function were (incorrectly) called across two
        # posts' lists concatenated together. This test proves the function
        # itself has no notion of "post" at all -- scoping to one post's own
        # events is the CALLER's responsibility (event_extraction_service.py
        # / promoter_crawl_service.py each call this once per post, never
        # once for the whole run) -- so the real regression this guards is a
        # future caller accidentally pooling multiple posts' lists together.
        post_a_ts = _post_at(2026, 12, 1)
        post_b_ts = _post_at(2026, 3, 1)
        rolled_alone = resolve_event_datetime(
            date_text="15/08", time_text=None, post_timestamp=post_a_ts,
        )
        unrelated_native = resolve_event_datetime(
            date_text="20/06", time_text=None, post_timestamp=post_b_ts,
        )
        # Called SEPARATELY, per post (the correct usage): neither list has
        # more than one dated event, so voting is a no-op for both.
        voted_a = vote_on_sibling_years([rolled_alone])
        voted_b = vote_on_sibling_years([unrelated_native])
        assert voted_a[0].starts_at.year == 2027, voted_a
        assert voted_b[0].starts_at.year == 2026, voted_b
