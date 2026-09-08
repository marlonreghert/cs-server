"""Casing-normalise an extracted `recurrence_text` for the serving
projection.

plans/260907_events-non-event-and-recurrence-normalisation.md §2 (defect
C2). Pure: no I/O, no config, no clock — a sibling of
`app.services.event_ticket_url`, and applied in the same place, once per
source row, at the projection boundary.

## Why this exists and why it lives at PROJECTION time

The extraction prompt orders the model to copy "the raw recurrence phrase"
verbatim, so the model is behaving correctly and this is not a prompt fix.
The 2026-09-07 production census found 33 of 44 occurrences carrying the
field across nine distinct raw values, two PAIRS of which differ by casing
alone — `'Toda QUARTA'`/`'toda quarta'` (9 occurrences) and
`'TODOS OS SÁBADOS'`/`'todos os sábados'` (6) — sitting side by side in one
shelf, saying the same thing in two spellings.

RDS is deliberately NOT rewritten: the extraction stays auditable, and
because the events projection rebuilds every payload on every cycle the
correction re-asserts itself on deploy with no migration and no
re-extraction. Nothing downstream of `weekdays_from_recurrence_text`
changes — that function keeps reading the verbatim RDS value.

## The rule, and its two deliberate limits

Trim, collapse internal whitespace, lowercase the phrase, then uppercase
its first cased character. SENTENCE case, not Title Case: pt-BR weekday
names are correctly lowercase, so this is the orthographically right form
as well as the collapsing one.

- **Casing only. This is NOT a vocabulary and nothing exact-matches against
  it** — see `app.models.post_category`'s module docstring for the incident
  that rule exists to prevent. A phrase the rule does not improve simply
  passes through; there is no non-match to fall out of. `'Aos domingos'`
  and `'Todos os domingos'` differ by PHRASING, not casing, and must stay
  two values.
- **A token beginning with `@` is preserved verbatim.** An Instagram handle
  is a case-carrying identifier, not prose.

Risk accepted and measured: lowercasing would also lowercase a proper noun
inside a recurrence phrase ("toda quarta no Bar do Zé"). Zero of the nine
live values contain one, and RDS keeps the raw text, so the rule can be
revised later without a migration or a re-extraction.
"""
from __future__ import annotations

from typing import Optional

# Outcome labels for `events_projection_recurrence_text_total{outcome}`.
OUTCOME_ABSENT = "absent"
OUTCOME_NORMALIZED = "normalized"
OUTCOME_UNCHANGED = "unchanged"


def _sentence_case(text: str) -> str:
    """`text` already trimmed and whitespace-collapsed. Lowercases every
    token except an `@handle`, then uppercases the phrase's FIRST CASED
    CHARACTER — with one exception: when that character lives inside an
    `@handle`, nothing is uppercased at all. The handle is preserved
    verbatim, so its own casing already IS the phrase's opening, and
    reaching past it to capitalise the next word would put a capital in the
    middle of a sentence.

    A leading token with no cased character (a numeral, an emoji,
    punctuation) is skipped rather than treated as the opening — "🎉 TODA
    QUARTA" reads "🎉 Toda quarta".

    **A character whose uppercase form is LONGER than one character is left
    alone**, and that guard is what makes the whole function idempotent. A
    handful of code points expand under `.upper()` — the ligature `'ﬁ'`
    becomes `'FI'`, `'ß'` becomes `'SS'` — so capitalising one would give
    `'ﬁesta'` -> `'FIesta'`, whose own normalisation is `'Fiesta'`: a
    different string, and therefore not a fixed point. Serving such a phrase
    in lower case is the honest outcome; inventing a two-character capital
    is not. Unreachable in real pt-BR recurrence prose (a 400k-case fuzz put
    it at ~3% of RANDOM unicode and 0 of the nine live values), but a
    docstring that claims idempotence has to be true."""
    tokens = text.split(" ")
    lowered = [t if t.startswith("@") else t.lower() for t in tokens]
    for index, token in enumerate(lowered):
        for position, char in enumerate(token):
            upper = char.upper()
            if upper == char:
                continue  # not a cased character
            if token.startswith("@"):
                return " ".join(lowered)
            if len(upper) != 1:
                return " ".join(lowered)  # length-changing; see the docstring
            lowered[index] = token[:position] + upper + token[position + 1:]
            return " ".join(lowered)
    return " ".join(lowered)


def normalize_recurrence_text(value) -> tuple[Optional[str], str]:
    """`(normalised_phrase_or_None, outcome_label)`.

    The outcome is what `events_projection_recurrence_text_total` counts,
    and it is returned alongside the value rather than re-derived by the
    caller so the two can never disagree:

    - `absent` — nothing was stored at all (None, a non-string, or blank).
    - `normalized` — the projected phrase differs from the stored (trimmed)
      one.
    - `unchanged` — the stored phrase was already in sentence case.

    Total and idempotent: `f(f(x)) == f(x)` for every input, and no input
    raises.
    """
    if not isinstance(value, str):
        return None, OUTCOME_ABSENT
    trimmed = value.strip()
    if not trimmed:
        return None, OUTCOME_ABSENT
    collapsed = " ".join(trimmed.split())
    result = _sentence_case(collapsed)
    outcome = OUTCOME_UNCHANGED if result == trimmed else OUTCOME_NORMALIZED
    return result, outcome


__all__ = [
    "OUTCOME_ABSENT",
    "OUTCOME_NORMALIZED",
    "OUTCOME_UNCHANGED",
    "normalize_recurrence_text",
]
