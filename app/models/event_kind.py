"""The post-kind vocabulary the extraction model is asked to classify every
qualifying post into. See
plans/260810_post-kind-and-post-extraction-attribution.md §A.

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

# Every kind that is NOT an event — the set the review queue predicate
# excludes (app.dao.rds_venue_store.RdsVenueStore.list_events_awaiting_
# decision / tests.rds_fake.InMemoryRdsVenueStore's mirror). A missing or
# unrecognised kind is deliberately NOT a member of this tuple: the plan's
# fail-toward-visible rule requires an unknown value to read as `event`, so
# the queue predicate must exclude only a RECOGNISED non-event kind, never
# "anything that is not literally the string 'event'". A misclassified
# event is silent (it never reaches a human) in a way no other failure in
# this pipeline is — the asymmetry is why this tuple, not its complement,
# is what the predicate checks against.
NON_EVENT_KINDS = (KIND_PROMOTION, KIND_MENU, KIND_FOOD, KIND_OTHER)


def normalize_kind(raw) -> Optional[str]:
    """The model's raw `kind` answer, lowercased and stripped, stored
    VERBATIM when non-empty — never coerced into a known value here.
    Recording exactly what the model said (even a value this pipeline does
    not recognise) is the only way to judge whether the classifier is any
    good (§B); "an unknown kind reads as event" is enforced at READ time
    (the queue predicate reading NON_EVENT_KINDS), not by rewriting the
    model's own answer at write time. Returns None for a missing/blank
    answer — NULL is the historically accurate value for a pre-migration
    row and for a response that omitted the field."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


__all__ = [
    "KIND_EVENT", "KIND_PROMOTION", "KIND_MENU", "KIND_FOOD", "KIND_OTHER",
    "EVENT_KINDS", "NON_EVENT_KINDS", "normalize_kind",
]
