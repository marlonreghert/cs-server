"""Unit coverage for `app.services.event_recurrence_text`.

plans/260907_events-non-event-and-recurrence-normalisation.md §2. The
load-bearing cases are the NINE distinct raw values the 2026-09-07
production census actually measured, verbatim: the rule's whole claim is
that it takes those nine to seven by folding casing and nothing else, and a
test built from invented strings would not check that claim at all.
"""
from __future__ import annotations

import pytest

from app.services.event_recurrence_text import (
    OUTCOME_ABSENT,
    OUTCOME_NORMALIZED,
    OUTCOME_UNCHANGED,
    normalize_recurrence_text,
)

# The nine distinct `recurrence_text` values across the 33 live occurrences
# that carry the field, with the occurrence count each accounted for.
_PRODUCTION_VALUES = [
    # (stored, projected, outcome, live occurrences)
    ("Toda QUARTA", "Toda quarta", OUTCOME_NORMALIZED, 6),
    ("toda quarta", "Toda quarta", OUTCOME_NORMALIZED, 3),
    ("Terça a Domingo", "Terça a domingo", OUTCOME_NORMALIZED, 6),
    ("TODOS OS DOMINGOS", "Todos os domingos", OUTCOME_NORMALIZED, 3),
    ("Aos domingos", "Aos domingos", OUTCOME_UNCHANGED, 3),
    ("Toda quinta", "Toda quinta", OUTCOME_UNCHANGED, 3),
    ("Toda sexta, às 21 horas", "Toda sexta, às 21 horas", OUTCOME_UNCHANGED, 3),
    ("todos os sábados", "Todos os sábados", OUTCOME_NORMALIZED, 3),
    ("TODOS OS SÁBADOS", "Todos os sábados", OUTCOME_NORMALIZED, 3),
]


class TestTheLiveCorpus:
    @pytest.mark.parametrize(
        "stored,projected,outcome",
        [(s, p, o) for s, p, o, _n in _PRODUCTION_VALUES],
    )
    def test_each_production_value_projects_its_sentence_case_form(
        self, stored, projected, outcome
    ):
        assert normalize_recurrence_text(stored) == (projected, outcome)

    def test_the_nine_distinct_values_collapse_to_seven(self):
        """The measurable point of the change: `'Toda QUARTA'`/`'toda
        quarta'` (9 occurrences) and `'TODOS OS SÁBADOS'`/`'todos os
        sábados'` (6) stop being two chips each."""
        stored = {s for s, _p, _o, _n in _PRODUCTION_VALUES}
        projected = {normalize_recurrence_text(s)[0] for s in stored}
        assert len(stored) == 9
        assert len(projected) == 7

    def test_phrasing_differences_survive_the_fold(self):
        """`'Aos domingos'` and `'TODOS OS DOMINGOS'` say the same thing in
        different WORDS, not different casing. Folding them would be
        rewriting meaning, which this rule never does."""
        assert (
            normalize_recurrence_text("Aos domingos")[0]
            != normalize_recurrence_text("TODOS OS DOMINGOS")[0]
        )

    def test_every_production_value_is_idempotent(self):
        for stored, _projected, _outcome, _n in _PRODUCTION_VALUES:
            once = normalize_recurrence_text(stored)[0]
            assert normalize_recurrence_text(once)[0] == once
            assert normalize_recurrence_text(once)[1] == OUTCOME_UNCHANGED


class TestAbsentAndMalformed:
    @pytest.mark.parametrize("value", [None, "", "   ", "\n\t ", 42, [], {}])
    def test_nothing_stored_reads_absent(self, value):
        assert normalize_recurrence_text(value) == (None, OUTCOME_ABSENT)

    def test_surrounding_whitespace_is_trimmed(self):
        """Trimming alone reads `unchanged`: the outcome compares the
        projected phrase against the TRIMMED input, so the counter measures
        casing work, not incidental whitespace."""
        assert normalize_recurrence_text("  Toda quarta  ") == (
            "Toda quarta", OUTCOME_UNCHANGED
        )

    def test_internal_whitespace_is_collapsed(self):
        assert normalize_recurrence_text("Toda    quarta") == (
            "Toda quarta", OUTCOME_NORMALIZED
        )


class TestEdges:
    def test_a_handle_token_keeps_its_own_casing(self):
        """An Instagram handle is a case-carrying identifier, not prose."""
        value, outcome = normalize_recurrence_text("toda quarta no @BarDoZe")
        assert value == "Toda quarta no @BarDoZe"
        assert outcome == OUTCOME_NORMALIZED

    def test_a_leading_handle_suppresses_the_initial_capital(self):
        """The phrase's first cased character lives inside the handle, which
        is preserved verbatim — so nothing is uppercased. Reaching past it
        to capitalise the next word would put a capital mid-sentence, and
        touching the handle itself would corrupt the identifier."""
        value, _outcome = normalize_recurrence_text("@BarDoZe toda quarta")
        assert value == "@BarDoZe toda quarta"

    def test_digits_and_punctuation_are_untouched(self):
        assert normalize_recurrence_text("Toda sexta, às 21 horas")[0] == (
            "Toda sexta, às 21 horas"
        )

    def test_a_leading_uncased_token_does_not_swallow_the_capital(self):
        value, _outcome = normalize_recurrence_text("🎉 TODA QUARTA")
        assert value == "🎉 Toda quarta"

    def test_a_phrase_with_no_cased_character_at_all_is_returned_as_is(self):
        assert normalize_recurrence_text("21/09")[0] == "21/09"

    def test_idempotence_over_every_edge(self):
        for raw in (
            "toda quarta no @BarDoZe", "@BarDoZe toda quarta", "🎉 TODA QUARTA",
            "21/09", "Toda    quarta", "  Toda quarta  ",
        ):
            once = normalize_recurrence_text(raw)[0]
            assert normalize_recurrence_text(once)[0] == once


class TestIdempotenceIsUnconditional:
    """F6: the docstring claims `f(f(x)) == f(x)` for EVERY input, and it did
    not hold. `_sentence_case` uppercased the first cased character with
    `char.upper()`, which is LENGTH-CHANGING for a handful of code points —
    `'ﬁ'` -> `'FI'`, `'ß'` -> `'SS'` — so `'ﬁesta …'` normalised to
    `'FIesta …'`, whose own normalisation is `'Fiesta …'`. Not a fixed point,
    and therefore not idempotent.

    Unreachable in real pt-BR recurrence prose, but it matters for the same
    reason every other rule here does: RDS keeps the verbatim text and the
    projector re-derives the served value on EVERY cycle, so a function
    without a fixed point is a value that could keep moving under a reader
    who is watching for change.
    """

    @pytest.mark.parametrize("raw", [
        "ﬁesta toda quarta",      # U+FB01 LATIN SMALL LIGATURE FI -> "FI"
        "ßonito toda quarta",     # U+00DF LATIN SMALL LETTER SHARP S -> "SS"
        "ﬄexível toda sexta",     # U+FB04 LATIN SMALL LIGATURE FFL -> "FFL"
        "ŉoite toda terça",       # U+0149 -> "ʼN"
    ])
    def test_a_length_changing_uppercase_is_left_alone(self, raw):
        once, _ = normalize_recurrence_text(raw)
        twice, _ = normalize_recurrence_text(once)
        assert once == twice, (raw, once, twice)

    def test_the_outcome_label_is_stable_too(self):
        """A value that is already normalised must report `unchanged` — an
        outcome that flips on a re-run would make the counter unreadable."""
        for raw in ("ﬁesta toda quarta", "Toda QUARTA", "🎉 TODA QUARTA"):
            once, _ = normalize_recurrence_text(raw)
            assert normalize_recurrence_text(once)[1] == OUTCOME_UNCHANGED, raw

    def test_idempotent_over_a_broad_codepoint_fuzz(self):
        """A named-case list would miss the NEXT expanding code point. This
        sweeps every character Python knows to be lowercase across the BMP
        and asserts the fixed-point property for a phrase beginning with it —
        which is exactly where `_sentence_case` acts."""
        offenders = []
        for cp in range(0x20, 0x10000):
            ch = chr(cp)
            if not ch.islower():
                continue
            raw = f"{ch}esta toda quarta"
            once, _ = normalize_recurrence_text(raw)
            twice, _ = normalize_recurrence_text(once)
            if once != twice:
                offenders.append((hex(cp), once, twice))
        assert not offenders, f"{len(offenders)} non-idempotent inputs, e.g. {offenders[:5]}"
