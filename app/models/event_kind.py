"""The post-kind vocabulary the extraction model is asked to classify every
qualifying post into. See
plans/260810_post-kind-and-post-extraction-attribution.md §A, and
plans/260811_post-items-and-categories.md §B for what happens to a kind once
it is read.

One definition, imported everywhere a kind is produced, stored, or filtered
on — never duplicated (CLAUDE.md: two `escHtml`s, two freeze rules, two cap
resolutions have each already cost this repo real time).
"""
from __future__ import annotations

from typing import Optional

KIND_EVENT = "event"
KIND_PROMOTION = "promotion"
KIND_MENU = "menu"
KIND_FOOD = "food"
KIND_OTHER = "other"

# Every kind the model is asked to choose between, in the SAME precedence
# order stated in both extraction prompts: event first (so an event
# advertised alongside a drinks offer is still an event), then promotion,
# menu, food, other.
EVENT_KINDS = (KIND_EVENT, KIND_PROMOTION, KIND_MENU, KIND_FOOD, KIND_OTHER)


def normalize_kind(raw) -> Optional[str]:
    """The model's raw `kind` answer, lowercased and stripped, stored
    VERBATIM when non-empty — never coerced into a known value here.
    Recording exactly what the model said (even a value this pipeline does
    not recognise) is the only way to judge whether the classifier is any
    good. Returns None for a missing/blank answer — NULL is the historically
    accurate value for a pre-migration row and for a response that omitted
    the field."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def resolve_post_type(kind: Optional[str]) -> str:
    """`kind` -> the `post_type` persisted on `events.post_item`
    (plans/260811_post-items-and-categories.md §B).

    Every extracted item is stored now — `260810_post-kind-and-post-
    extraction-attribution.md`'s interim (compute `kind`, then drop every
    non-event without persisting it) is retired. An UNRECOGNISED kind is
    stored VERBATIM (never coerced): the fail-toward-visible rule that used
    to force an unknown value to read as "event" existed because a dropped
    row was invisible; a persisted, typed row is inspectable and correctable
    through `operator_edited_fields`, so masquerading as an event is no
    longer necessary or honest. A MISSING/blank kind (`normalize_kind`
    already returns None for it) is different from an unrecognised one —
    there is no answer to preserve verbatim, and `post_type` is NOT NULL —
    so it still defaults to `KIND_EVENT`, the one case where "fail toward
    the safe assumption" still applies.
    """
    return kind if kind else KIND_EVENT


__all__ = [
    "KIND_EVENT", "KIND_PROMOTION", "KIND_MENU", "KIND_FOOD", "KIND_OTHER",
    "EVENT_KINDS", "normalize_kind", "resolve_post_type",
]
