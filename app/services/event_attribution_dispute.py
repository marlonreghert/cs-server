"""When an event's OWN text disagrees with the venue its handle is mapped to.

See plans/260912_events-venue-night-duplication.md §C (Defect 2).
`EventExtractionService._extract_one` pins every event a venue post yields to
the crawled handle's single mapped venue and discards each event's own
`location_text` — correct when a handle really does belong to one venue, and
deliberately so (`plans/260806_venue-post-multi-event.md` §D). It stops being
correct for a chain: `beerdock_recife`'s weekly flyer names three physical
branches, only one of which has a catalog handle, so 20 of 44 live rows at
"BeerDock Boa Viagem" carry `location_text = 'CASA FORTE'` and the app shows
two "duplicates" that are two different bars.

## What "disagreement" means, and why not string comparison

Comparing `location_text` to the mapped venue's name is the obvious rule and
it is wrong: these are free strings written for humans (`CASA FORTE`,
`@beerdock.cf`, `Rua da Aurora, 123`, `nossa unidade de Boa Viagem`), and a
mismatch against a venue NAME carries no information at all — the majority of
correct rows would "mismatch". The only reliable evidence is the evidence the
resolution ladder already grades, so this module REUSES
`event_venue_resolution.resolve_event_venue` rather than inventing a second
notion of "which venue does this text name".

An attribution is DISPUTED when, and only when, the ladder returns
`RESOLUTION_AUTO` for a venue_id DIFFERENT from the mapped one via a method in
`DISPUTE_METHODS` — rung 1 (an `@`-handle identity the model read out of THIS
EVENT'S OWN text), rung 2 (Instagram's own place database) or rung 3 (a folded
substring of the event's own text inside a SIBLING BRANCH's address). It is
also disputed, with no resolvable target, when the event's own text names a
specific `@handle` we do not carry (`METHOD_VENUE_NOT_IN_CATALOG`).

## Why this does not contradict `260806_venue-post-multi-event.md` §D

§D forbids re-attributing a venue's own post **on a fuzzy NAME match** — rung
4, the scored rung, the one that produced the `@mahalilacafe` -> "Maria Café"
links `260813_handle-attribution-hardening.md` was written to kill.
`METHOD_NAME_MATCH` and `METHOD_CAPTION_HANDLE_MENTION` therefore NEVER
trigger a dispute here, and the caption is never even consulted: this module
passes `caption=None` into the ladder, so rung 5 is structurally unreachable
rather than merely excluded by a check a future edit could drop. §D also
assumed the premise "a handle really does belong to one venue", which is
exactly the premise that fails for a chain. The rule below keeps §D's ban
intact and narrows the exception to the case §D never contemplated.

## Two outcomes, two gates, both defaulting to today's behaviour

`event_attribution_dispute_action` (`flag` by default) decides whether a
dispute merely annotates the row or actually moves it;
`event_attribution_dispute_withhold_enabled` (`false` by default) decides
whether the recorded review reason is allowed to withhold auto-accept — a
real withdrawal of content, so it gets its own flag rather than a free ride
on a detection feature.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.services.event_dedup import (
    DEFAULT_GENERIC_VOCABULARY,
    DEFAULT_STOPWORDS,
    _load_validated_config,  # noqa: F401 — plan §C: "validated ... through the SAME `_load_validated_config` path"; imported rather than re-implemented so a stored `"false"` can never read back as True here either.
    _tokenize,
)
from app.services.event_identity import normalize_title
from app.services.event_venue_resolution import (
    METHOD_HANDLE_MENTION,
    METHOD_LOCATION_TAG,
    METHOD_NEIGHBOURHOOD_MATCH,
    METHOD_VENUE_NOT_IN_CATALOG,
    RESOLUTION_AUTO,
    resolve_event_venue,
)

logger = logging.getLogger(__name__)

# The ONE new review-reason token this plan adds, joining the `"; "`-separated
# set `event_reconciliation.reconcile_post_events` already folds.
REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE = "location_text_disputes_venue"

# Rungs 1, 2 and 3 — identities and bounded-certain matches, never scores.
# `METHOD_NAME_MATCH` (rung 4, the fuzzy one §D bans) and
# `METHOD_CAPTION_HANDLE_MENTION` (rung 5, post-level rather than per-event)
# are deliberately ABSENT; see this module's docstring.
DISPUTE_METHODS = frozenset({
    METHOD_HANDLE_MENTION, METHOD_LOCATION_TAG, METHOD_NEIGHBOURHOOD_MATCH,
})

ADMIN_CONFIG_DISPUTE_ACTION_KEY = "admin_config:event_attribution_dispute_action"
DISPUTE_ACTION_FLAG = "flag"
DISPUTE_ACTION_REATTRIBUTE = "reattribute"
DISPUTE_ACTIONS = (DISPUTE_ACTION_FLAG, DISPUTE_ACTION_REATTRIBUTE)
# Ships as `flag`: the deploy of this branch must provably change no stored
# row, and `reattribute` moves `venue_id` on the very next crawl.
DEFAULT_DISPUTE_ACTION = DISPUTE_ACTION_FLAG

ADMIN_CONFIG_DISPUTE_WITHHOLD_KEY = "admin_config:event_attribution_dispute_withhold_enabled"
# Ships OFF: a non-empty review_reason makes `is_clean_extraction` false,
# which makes the row `pending_review`, which removes it from serving. That
# is the RIGHT answer for a chain (better nothing than a Casa Forte party
# shown at Boa Viagem) but it is a real withdrawal of content, so it is an
# operator's deliberate act rather than a side effect of this deploy.
DEFAULT_DISPUTE_WITHHOLD_ENABLED = False


def validate_dispute_action_config(value) -> str:
    if not isinstance(value, str) or value not in DISPUTE_ACTIONS:
        raise ValueError(
            f"event attribution dispute action must be one of {DISPUTE_ACTIONS}"
        )
    return value


def validate_dispute_withhold_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("event attribution dispute withholding flag must be a boolean")
    return value


@dataclass(frozen=True)
class AttributionDisputeConfig:
    action: str = DEFAULT_DISPUTE_ACTION
    withhold_enabled: bool = DEFAULT_DISPUTE_WITHHOLD_ENABLED

    @property
    def reattributes(self) -> bool:
        return self.action == DISPUTE_ACTION_REATTRIBUTE


def load_attribution_dispute_config(redis_like) -> AttributionDisputeConfig:
    """Both keys, read independently and TRUSTED as whatever their own
    validator returns — never coerced (`bool("false")` -> True is the trap
    `_load_validated_config` exists to prevent). `redis_like=None` returns
    the shipped defaults, matching every sibling loader in this codebase."""
    action = _load_validated_config(
        redis_like, ADMIN_CONFIG_DISPUTE_ACTION_KEY, DEFAULT_DISPUTE_ACTION,
        validator=validate_dispute_action_config, module_tag="event_attribution_dispute",
    )
    withhold = _load_validated_config(
        redis_like, ADMIN_CONFIG_DISPUTE_WITHHOLD_KEY, DEFAULT_DISPUTE_WITHHOLD_ENABLED,
        validator=validate_dispute_withhold_enabled_config,
        module_tag="event_attribution_dispute",
    )
    return AttributionDisputeConfig(action=action, withhold_enabled=withhold)


# ── the brand root (rung 3's bounded candidate set) ─────────────────────────
# Words that are generic in a VENUE NAME. Deliberately NOT added to
# `event_dedup.DEFAULT_GENERIC_VOCABULARY`, which is a different question
# with a different blast radius: that list feeds `distinctive_set`, the
# TITLE dedup bar, which is live in production with auto-merge on and pinned
# by `260812`'s measured false-positive corpus. Adding "casa" there would
# change which EVENT TITLES are distinctive — `{casa, bacurau}` against
# `{casa}` is a subset today and auto-merges, and would become `{bacurau}`
# against `{}`, an empty-set refusal. "Generic in an event title" (sextou,
# aniversario, oficina) and "generic in a venue name" (restaurante, boteco,
# casa) are genuinely different sets, and conflating them would ship an
# unrelated merge-behaviour change.
#
# Measured against the real catalog (2,694 venues, 1,467 distinct leading
# tokens) rather than guessed. Every token below leads 4+ unrelated venues;
# the worst offenders are `restaurante` (203), `boteco` (102), `the` (45),
# `casa` (34) and `villa` (12) — the last two are what made
# `brand_root_venues("Casa Bacurau")` return a 33-member "sibling" set
# spanning six cities. `bar` (215) was already caught, which is why the gap
# went unnoticed: the guard worked for the one word it knew.
#
# Over-inclusion is CHEAP and under-inclusion is not: a word listed here
# merely disables rung 3 for that family (which is the status quo — rung 3
# matches nothing in production today), whereas a missing word hands rung 3
# an unbounded candidate set of unrelated venues.
DEFAULT_BRAND_ROOT_STOPWORDS: tuple[str, ...] = (
    # venue types
    "restaurante", "boteco", "botequim", "buteco", "budega", "cervejaria",
    "choperia", "chopperia", "boate", "cafe", "casa", "villa", "vila",
    "espaco", "armazem", "barraca", "quiosque", "quintal", "recanto",
    "padaria", "clube", "club", "pub", "house", "cantina", "adega",
    "churrascaria", "teatro", "centro", "praca", "emporio", "terraco",
    "lanchonete", "pizzaria", "sorveteria", "hamburgueria", "petiscaria",
    "taberna", "taverna", "bistro", "lounge", "hostel", "pousada", "hotel",
    "mercado", "feira", "galeria", "coffee", "ilha", "toca", "portal",
    "square", "point", "ponto", "tempero", "sabor",
    # articles, possessives and generic qualifiers used as name filler
    "the", "la", "le", "o", "a", "os", "as", "na", "no", "seu", "sua",
    "nosso", "nossa", "meu", "minha", "dona", "dom", "novo", "nova",
    "big", "grande", "maria", "sao", "santa", "santo", "blue", "sky",
    "fogo", "trilha", "camarada", "oficina", "pe",
)

# Defense in depth, independent of the list above: a hand-maintained
# vocabulary is always incomplete for whatever the next unforeseen generic
# word turns out to be, and the SAFETY ARGUMENT for rung 3 is that its
# candidate set is small and genuinely related — not that the list is
# perfect. If a leading token would produce more than this many venues, that
# is itself evidence it is not a brand, and the brand root collapses to the
# mapped venue alone (which disables rung 3 entirely, its own `>= 2` gate).
#
# 8, chosen against the catalog rather than picked: the largest GENUINE
# chain in it is Coco Bambu at 6 (Deltaexpresso 5, Sal e Brasa 5, Coffee
# Shop São Braz 5), so 8 clears the observed maximum with headroom while
# still catching every bogus family the vocabulary might miss — the 9- and
# 10-member `barraca`/`botequim`/`quintal`/`vila` groups all fail it.
MAX_BRAND_ROOT_VENUES = 8
def _ordered_tokens(name: Optional[str]) -> list:
    """The venue name's tokens IN ORDER, through the SAME
    `normalize_title` + non-alphanumeric split `event_dedup.venue_name_tokens`
    already applies — never a new normalisation. (`venue_name_tokens` itself
    returns a frozenset; a brand root is about a LEADING RUN, which a set
    cannot express.)"""
    if not name:
        return []
    return _tokenize(normalize_title(name))


def _is_distinctive_brand_token(
    token: str, *, generic_vocabulary, stopwords,
    brand_root_stopwords=DEFAULT_BRAND_ROOT_STOPWORDS,
) -> bool:
    """A leading token only counts toward a brand root when it is
    distinctive. Without this, "Bar do Zé" and "Bar Central" would be
    siblings on the strength of the word `bar` — and rung 3 would then be
    free to drag an event to a completely unrelated venue that merely shares
    a neighbourhood name, which is the exact failure
    `_neighbourhood_match_candidates`' own docstring says the bounded
    candidate set exists to prevent.

    THREE lists, not one, because they answer different questions: the title
    vocabularies (`generic_vocabulary`/`stopwords`, shared with the live
    merge bar) plus `DEFAULT_BRAND_ROOT_STOPWORDS`, the venue-name words
    measured from the real catalog. See that constant for why the two are
    kept apart."""
    if token in {normalize_title(w) for w in stopwords}:
        return False
    if token in {normalize_title(w) for w in generic_vocabulary}:
        return False
    if token in {normalize_title(w) for w in brand_root_stopwords}:
        return False
    if len(token) < 2 and not token.isdigit():
        return False
    return True


def brand_root_venues(
    mapped_venue_id: Optional[str],
    venues: list,
    *,
    generic_vocabulary=DEFAULT_GENERIC_VOCABULARY,
    stopwords=DEFAULT_STOPWORDS,
) -> list:
    """The mapped venue plus every catalog venue sharing a DISTINCTIVE
    leading token run with it — `BeerDock Boa Viagem` / `BeerDock Casa Forte`
    / `BeerDock Madalena`. Never the whole catalog, and never a similarity
    score: this is the caller-bounded `same_account_venues` set rung 3
    requires (`event_venue_resolution.resolve_event_venue`, gated on
    `len(same_account_venues) >= 2`), so when a venue has no siblings the
    list has one member, rung 3 does not run at all, and a dispute can then
    only come from rung 1 or rung 2."""
    by_id = {v.venue_id: v for v in venues}
    mapped = by_id.get(mapped_venue_id)
    if mapped is None:
        return []
    mapped_tokens = _ordered_tokens(mapped.venue_name)
    if not mapped_tokens:
        return [mapped]
    lead = mapped_tokens[0]
    if not _is_distinctive_brand_token(
        lead, generic_vocabulary=generic_vocabulary, stopwords=stopwords,
    ):
        return [mapped]
    out = [mapped]
    for venue in venues:
        if venue.venue_id == mapped.venue_id:
            continue
        tokens = _ordered_tokens(venue.venue_name)
        if tokens and tokens[0] == lead:
            out.append(venue)

    # The defensive bound (see `MAX_BRAND_ROOT_VENUES`): an oversized set is
    # itself evidence the leading token is a common word rather than a
    # brand, whatever the vocabulary happens to know. Collapsing to the
    # mapped venue alone disables rung 3 through its own `>= 2` gate, which
    # is the conservative answer — it returns that family to today's
    # behaviour rather than handing the rung an unbounded candidate set.
    if len(out) > MAX_BRAND_ROOT_VENUES:
        logger.info(
            "[EventAttributionDispute] leading token %r would yield %d "
            "'siblings' for %s — above the %d ceiling, so it is not treated "
            "as a brand root",
            lead, len(out), mapped.venue_id, MAX_BRAND_ROOT_VENUES,
        )
        return [mapped]
    return out


# ── the verdict ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DisputeVerdict:
    """A dispute, and what (if anything) can be done about it.

    `target_venue_id is None` is the `METHOD_VENUE_NOT_IN_CATALOG` case: the
    event's own text names a SPECIFIC place we do not carry. There is nowhere
    to re-attribute it to, so it is flagged whatever the action config says —
    and the handle it named is the venue-acquisition backlog's own entry.
    """

    method: str
    target_venue_id: Optional[str] = None
    target_venue_name: Optional[str] = None
    confidence: Optional[float] = None
    candidates: tuple = ()
    location_text: Optional[str] = None

    @property
    def has_target(self) -> bool:
        return self.target_venue_id is not None


def evaluate_attribution_dispute(
    *,
    mapped_venue_id: Optional[str],
    location_text: Optional[str],
    venues: list,
    handle_index: dict,
    location_tag: Optional[dict] = None,
    promoter_handle: Optional[str] = None,
    operator_edited_fields=None,
    generic_vocabulary=DEFAULT_GENERIC_VOCABULARY,
    stopwords=DEFAULT_STOPWORDS,
) -> Optional[DisputeVerdict]:
    """`None` when this event's own text is not evidence against its mapped
    venue — which is the answer for the overwhelming majority of rows, and
    the answer this function is biased toward.

    Deliberately refuses, in this order, BEFORE consulting the ladder at all:
      - no mapped venue (there is nothing to dispute);
      - no `location_text` (the event said nothing about where it is — the
        ONLY per-event evidence this rule is allowed to act on);
      - an operator-edited `venue_id` (they made a decision; an automatic
        path must never override it — the SAME "operator outranks the model"
        posture `_merge_handle_group` and `_edge_blocked_for_absorption`
        already take). Enforced HERE, in the function itself, rather than
        left to each call site to remember.
    """
    if not mapped_venue_id or not location_text:
        return None
    if "venue_id" in (operator_edited_fields or []):
        return None

    resolution = resolve_event_venue(
        # The caption is post-level evidence for every event a post yields;
        # this rule acts only on THIS event's own text. Passing None makes
        # rung 5 structurally unreachable rather than merely excluded.
        caption=None,
        location_text=location_text,
        location_tag=location_tag,
        promoter_handle=promoter_handle,
        venues=venues,
        handle_index=handle_index,
        same_account_venues=brand_root_venues(
            mapped_venue_id, venues,
            generic_vocabulary=generic_vocabulary, stopwords=stopwords,
        ),
        # This rule acts on `DISPUTE_METHODS` (rungs 1-3) plus the
        # `venue_not_in_catalog` sentinel, and on NOTHING else — rung 4's
        # `name_match` and rung 5's caption mention are excluded by name,
        # above. That is exactly the declaration `identity_methods_only`
        # asks for, so the ladder may skip its one superlinear rung wherever
        # that rung's result is unobservable here. See `resolve_event_venue`'s
        # rung-4 block for the proof; `tests/test_event_attribution_dispute.py`
        # pins it by differential-testing both settings against each other.
        identity_methods_only=True,
    )

    if (
        resolution.resolution == RESOLUTION_AUTO
        and resolution.method in DISPUTE_METHODS
        and resolution.venue_id
        and resolution.venue_id != mapped_venue_id
    ):
        by_id = {v.venue_id: v for v in venues}
        target = by_id.get(resolution.venue_id)
        return DisputeVerdict(
            method=resolution.method,
            target_venue_id=resolution.venue_id,
            target_venue_name=getattr(target, "venue_name", None),
            confidence=resolution.confidence,
            candidates=tuple(resolution.candidates),
            location_text=location_text,
        )

    if resolution.method == METHOD_VENUE_NOT_IN_CATALOG:
        # "We can tell EXACTLY where this is, and it is not a venue we
        # carry" — concrete per-event evidence against the mapped venue,
        # with nowhere to move the row to.
        return DisputeVerdict(
            method=METHOD_VENUE_NOT_IN_CATALOG, location_text=location_text,
        )

    return None


def fold_review_reason(existing: Optional[str], token: str) -> Optional[str]:
    """Append `token` to the `"; "`-separated set `reconcile_post_events`
    already writes, never repeating one already stated and never dropping
    another call site's reason."""
    reasons = [r for r in (existing or "").split("; ") if r]
    if token not in reasons:
        reasons.append(token)
    return "; ".join(reasons) if reasons else None


__all__ = [
    "REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE", "DISPUTE_METHODS",
    "ADMIN_CONFIG_DISPUTE_ACTION_KEY", "ADMIN_CONFIG_DISPUTE_WITHHOLD_KEY",
    "DISPUTE_ACTION_FLAG", "DISPUTE_ACTION_REATTRIBUTE", "DISPUTE_ACTIONS",
    "DEFAULT_DISPUTE_ACTION", "DEFAULT_DISPUTE_WITHHOLD_ENABLED",
    "validate_dispute_action_config", "validate_dispute_withhold_enabled_config",
    "AttributionDisputeConfig", "load_attribution_dispute_config",
    "brand_root_venues", "DisputeVerdict", "evaluate_attribution_dispute",
    "DEFAULT_BRAND_ROOT_STOPWORDS", "MAX_BRAND_ROOT_VENUES",
    "fold_review_reason",
]
