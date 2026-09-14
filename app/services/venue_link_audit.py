"""Venue-handle link audit: does a `kind='venue'` crawl target's own posts
give any evidence for the venue it is currently mapped to?

See plans/260913_venue-handle-link-audit.md. A `kind='venue'` crawl target
whose handle maps to exactly one venue (76 of 83 live targets, measured) has
every event FORCE-assigned to that venue by
`EventExtractionService._extract_one`'s default closure — never through
`event_venue_resolution.resolve_event_venue` at all. The one existing safety
net, `event_attribution_dispute.evaluate_attribution_dispute`, is
structurally blind to this: its `DISPUTE_METHODS` excludes
`METHOD_NAME_MATCH` by deliberate design (that module's own docstring), so a
crawl target simply set up against the wrong venue — not a sibling-brand
chain, just a wrong mapping — has no automatic check at all. Confirmed live:
`editaisculturape` force-assigns 14 events to "Casa da Cultura de
Pernambuco" though two of its posts name a different, real place
("Torre Malakoff", "Casa de Câmara e Cadeia de Brejo da Madre de Deus") in
plain prose, and 12 of those 14 are `status='accepted'` — live, served, and
wrong.

## The comparison, and why it is not the old, abandoned one

`event_dedup_backlog.py`'s own docstring records a PRIOR, REJECTED design:
comparing a location_text against a venue's name/address by substring
containment in the OTHER direction (is the whole `location_text` contained
in the venue's name/address) — which broke on self-referential prose
("`nossa unidade de Boa Viagem`" is not a substring of "BeerDock Boa
Viagem") and buried real signal under false positives (14 self-referential
spellings for one venue alone).

This module compares the other way, and on a different, more stable
fragment: does `name_similarity` (the SAME scorer
`event_venue_resolution`'s rung 2/4 already use) clear
`DEFAULT_CONFIDENCE_FLOOR` against the venue's own NAME, OR does the
venue's own `neighborhood` (a short, stable, already-structured
`venues.address` column — 100% populated for every venue currently mapped
from a `kind='venue'` crawl target, measured live) appear, folded, as a
SUBSTRING WITHIN `location_text` (needle=neighbourhood, haystack=
location_text — the reverse of `event_venue_resolution.
_neighbourhood_match_candidates`'s own containment direction, which assumes
the opposite: a short bounded fragment inside a longer stored address).
Measured against the real self-referential case this codebase already hit
once ("nossa unidade de Boa Viagem" for a venue whose own neighbourhood is
"Boa Viagem"): the neighbourhood IS found, so this module correctly treats
it as corroborating — the same case the old design got wrong, now right.

Unbounded, catalog-wide name-matching was tried and rejected too (see the
plan's Evidence): "Torre Malakoff" top-scores an unrelated "Torra Café"
(0.6364) ahead of the real landmark "Malakoff Tower" (0.6154) — a
coincidental collision across 2,694 venues. This module never searches the
whole catalog; every comparison is bounded to the ONE (or, for a
double-mapped handle, the few) venue(s) the handle is ALREADY mapped to.

## The flagging bar is a pattern, not a single mention

A `(handle, venue)` pair is flagged only once it has at least
`MIN_NON_CORROBORATING_EVENTS` (2) checkable events that corroborate
NEITHER venue in the handle's own mapped set — calibrated against
`editaisculturape`'s own real count (exactly 2 decisive non-corroborating
posts; a bar of 3, mirroring `PromoterRegistryService.
DEFAULT_MENTION_THRESHOLD`, would miss the one confirmed real case this
sweep exists to catch). This is the task's own named false-positive guard:
a caption mentioning a sponsor or a nearby landmark in passing, once, never
flags a handle on its own.

Pure computation (`compute_venue_link_audit`) + a thin DAO adapter
(`collect_venue_link_audit`), mirroring `event_dedup_backlog.py`'s own
`compute_*`/`collect_*` split: the pure function is what every test drives,
and it never touches a DAO.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.services.event_dedup import _load_validated_config  # noqa: F401 — the SAME validated-read path every sibling admin-config key in this pipeline uses; never coerced (`bool("false")` -> True is the trap it exists to prevent).
from app.services.event_dedup_backlog import STATUS_SUPERSEDED
from app.services.event_venue_resolution import (  # noqa: F401 — reused, not re-implemented: the SAME confidence floor and character-folding primitive the resolution ladder itself already uses.
    DEFAULT_CONFIDENCE_FLOOR,
    _fold_for_containment,
    _venue_field,
)
from app.services.instagram_cascade_service import name_similarity
from app.services.instagram_handle_sources import group_venue_ids_by_handle

logger = logging.getLogger(__name__)

ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY = "admin_config:venue_link_audit_enabled"
# OFF by default, like every sibling flag in this pipeline: the deploy of
# this branch must provably change no stored row and no existing response
# field — see the router wiring, which skips `collect_venue_link_audit`
# entirely while this is False, not merely hides its output.
DEFAULT_VENUE_LINK_AUDIT_ENABLED = False

# Calibrated against real measured data (this module's own docstring above
# and the plan's Evidence section), not picked blind. Do not "round up" to
# 3 (PromoterRegistryService.DEFAULT_MENTION_THRESHOLD's bar) without
# re-reading why: `editaisculturape` — the one confirmed real case this
# sweep exists to catch — has exactly 2 decisive non-corroborating posts: a
# bar of 3 would silently un-flag it.
MIN_NON_CORROBORATING_EVENTS = 2

# Same minimum `event_venue_resolution._neighbourhood_match_candidates`
# already enforces — a folded string shorter than this is too generic to
# mean anything either way.
_MIN_CHECKABLE_TEXT_LEN = 3


def validate_venue_link_audit_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("venue link audit flag must be a boolean")
    return value


def load_venue_link_audit_enabled(redis_like) -> bool:
    return _load_validated_config(
        redis_like, ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY,
        DEFAULT_VENUE_LINK_AUDIT_ENABLED,
        validator=validate_venue_link_audit_enabled_config,
        module_tag="venue_link_audit",
    )


def venue_corroborates_location_text(
    venue_name: Optional[str], venue_neighborhood: Optional[str],
    location_text: Optional[str],
) -> Optional[bool]:
    """`None` when `location_text` is too short/empty to be evidence either
    way (the caller must exclude these from both the corroborating and
    non-corroborating counts — see this module's docstring on the pattern
    bar). Otherwise `True` when EITHER `name_similarity(venue_name,
    location_text) >= DEFAULT_CONFIDENCE_FLOOR` OR the venue's own folded
    `neighborhood` is a substring of the folded `location_text`; `False`
    otherwise."""
    if not location_text:
        return None
    folded_text = _fold_for_containment(location_text)
    if len(folded_text) < _MIN_CHECKABLE_TEXT_LEN or not any(ch.isalpha() for ch in folded_text):
        return None
    if venue_name and name_similarity(venue_name, location_text, None) >= DEFAULT_CONFIDENCE_FLOOR:
        return True
    if venue_neighborhood:
        folded_neighbourhood = _fold_for_containment(venue_neighborhood)
        if folded_neighbourhood and folded_neighbourhood in folded_text:
            return True
    return False


@dataclass(frozen=True)
class MappedVenueAudit:
    """One mapped venue's verdict for one handle. `flagged` is this venue's
    OWN bar check — a double-mapped handle (the `real.botequim` shape)
    carries one of these per mapped venue, so the correct sibling
    (`flagged=False`) stays visible alongside the spurious one
    (`flagged=True`) rather than either being silently dropped or the two
    being conflated into one yes/no answer for the whole handle."""

    venue_id: str
    venue_name: Optional[str]
    checkable_count: int
    non_corroborating_count: int
    flagged: bool
    sample_location_texts: tuple = ()
    # Parallel to sample_location_texts — the raw name_similarity score
    # against THIS venue, for operator transparency (None when the venue
    # has no name to score against, which should not occur in practice).
    sample_scores: tuple = ()

    def to_dict(self) -> dict:
        return {
            "venue_id": self.venue_id, "venue_name": self.venue_name,
            "checkable_count": self.checkable_count,
            "non_corroborating_count": self.non_corroborating_count,
            "flagged": self.flagged,
            "sample_location_texts": list(self.sample_location_texts),
            "sample_scores": list(self.sample_scores),
        }


@dataclass(frozen=True)
class VenueLinkAuditCandidate:
    """One handle with at least one flagged mapped venue. `mapped_venues`
    carries EVERY venue currently mapped to this handle, flagged or not —
    never only the flagged one — so a double-mapped handle's good sibling
    is visible in the same record."""

    handle: str
    mapped_venues: tuple  # tuple[MappedVenueAudit, ...]

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "mapped_venues": [v.to_dict() for v in self.mapped_venues],
        }


def compute_venue_link_audit(
    targets: list,
    events_by_handle: dict,
    *, min_non_corroborating: int = MIN_NON_CORROBORATING_EVENTS,
) -> list:
    """PURE — takes already-fetched data, touches no DAO, mirrors
    `event_dedup_backlog.compute_dedup_backlog`'s own stated posture.

    `targets`: `[{"handle": str, "mapped_venues": [{"venue_id", "venue_name",
    "neighborhood"}, ...]}, ...]` — every `kind='venue'` crawl target that
    currently has at least one venue mapped to it (the caller excludes an
    orphaned target with zero mapped venues, per the plan's Non-goals — it
    has no evidence to check against).

    `events_by_handle`: `{handle: [event_row, ...]}`, already filtered to
    live (non-superseded) rows. Only each row's own `location_text` is
    read — never the caption, the same per-event-over-post-level precedence
    `resolve_event_venue`/`evaluate_attribution_dispute` already use.

    Each mapped venue V is checked only against events whose OWN CURRENT
    `venue_id` is `None` (never resolved — the `editaisculturape`/
    `real.botequim` shape this sweep exists to catch) or V itself — never
    against an event already, confidently resolved to a DIFFERENT venue by
    an unrelated mechanism (the attribution-dispute ladder, a historical
    backfill). plans/260914_venue-link-audit-checks-current-attribution.md:
    running this sweep against production found it re-flagging
    `beerdock_recife` for 16 events already correctly reattributed to
    "Beerdock Casa Forte" hours earlier — an event that is no longer
    evidence about its ORIGINAL mapping's correctness at all, it is a
    closed case. Do not "simplify" this back to iterating the handle's
    full event list — that is precisely the bug this filter fixes.

    Returns one `VenueLinkAuditCandidate` per handle that has at least one
    flagged mapped venue — a handle with zero flagged venues (including one
    with zero checkable events at all) produces no candidate."""
    out = []
    for target in targets:
        handle = target["handle"]
        mapped = target.get("mapped_venues") or []
        if not mapped:
            continue
        events = events_by_handle.get(handle) or []
        venue_audits = []
        any_flagged = False
        for venue in mapped:
            checkable = 0
            non_corroborating = 0
            samples: list = []
            scores: list = []
            # Only events currently unresolved (venue_id is None) or
            # already resolved to THIS venue are evidence about THIS
            # venue's mapping — see this function's own docstring.
            checkable_rows = [
                row for row in events
                if row.get("venue_id") in (None, venue["venue_id"])
            ]
            for row in checkable_rows:
                verdict = venue_corroborates_location_text(
                    venue.get("venue_name"), venue.get("neighborhood"),
                    row.get("location_text"),
                )
                if verdict is None:
                    continue
                checkable += 1
                if verdict:
                    continue
                non_corroborating += 1
                if len(samples) < 5:
                    samples.append(row.get("location_text"))
                    score = None
                    if venue.get("venue_name"):
                        score = round(
                            name_similarity(venue["venue_name"], row.get("location_text"), None), 4,
                        )
                    scores.append(score)
            flagged = non_corroborating >= min_non_corroborating
            any_flagged = any_flagged or flagged
            venue_audits.append(MappedVenueAudit(
                venue_id=venue["venue_id"], venue_name=venue.get("venue_name"),
                checkable_count=checkable, non_corroborating_count=non_corroborating,
                flagged=flagged, sample_location_texts=tuple(samples), sample_scores=tuple(scores),
            ))
        if any_flagged:
            out.append(VenueLinkAuditCandidate(handle=handle, mapped_venues=tuple(venue_audits)))
    return out


# ── the one adapter (see this module's docstring) ───────────────────────────
def collect_venue_link_audit(venue_dao, *, redis_like=None) -> list:
    """Fetch and delegate. Holds no rules of its own — every rule lives in
    `compute_venue_link_audit`, which is what every test drives.

    `redis_like` is accepted but unused: unlike `collect_dedup_backlog`,
    nothing in this function's OWN logic reads an admin-config key (the
    single `venue_link_audit_enabled` gate is the CALLER's decision whether
    to invoke this at all — see `app.routers.admin_events_router.
    get_dedup_backlog`). Kept for calling-convention parity with its
    sibling collector, which every other report-only collector in this
    module's family also accepts.

    TWO bulk reads, never a `get_venue`/`get_address` per venue — the exact
    discipline `collect_dedup_backlog` already established for the same
    reason: this can run on every `GET /admin/events/dedup-backlog` hit."""
    target_rows = venue_dao.list_crawl_targets(kind="venue") or []
    if not target_rows:
        return []

    handles_map = venue_dao.list_instagram_handles() or {}
    by_handle = group_venue_ids_by_handle(handles_map)

    mapped_venue_ids = sorted({
        vid for t in target_rows for vid in by_handle.get(t["handle"], [])
    })
    if not mapped_venue_ids:
        return []

    addresses = venue_dao.get_address_bulk(mapped_venue_ids) or {}
    venues_by_id = venue_dao.get_venues_by_ids(mapped_venue_ids) or {}

    targets = []
    events_by_handle = {}
    for t in target_rows:
        handle = t["handle"]
        venue_ids = by_handle.get(handle, [])
        if not venue_ids:
            # The orphaned-target shape (`champagne.clubrecife`, measured
            # live): a kind='venue' crawl target with no venue currently
            # pointing at it has no mapped venue to check evidence against
            # — skipped, never flagged, never an error (plan Non-goals).
            continue
        mapped = [
            {
                "venue_id": vid,
                "venue_name": _venue_field(venues_by_id.get(vid), "venue_name"),
                "neighborhood": (addresses.get(vid) or {}).get("neighborhood"),
            }
            for vid in venue_ids
        ]
        targets.append({"handle": handle, "mapped_venues": mapped})
        rows = venue_dao.list_events_by_handle(handle) or []
        events_by_handle[handle] = [r for r in rows if r.get("status") != STATUS_SUPERSEDED]

    candidates = compute_venue_link_audit(targets, events_by_handle)
    for c in candidates:
        flagged = [v for v in c.mapped_venues if v.flagged]
        logger.info(
            "[VenueLinkAudit] flagged handle=%s venues=%s",
            c.handle,
            [(v.venue_id, v.non_corroborating_count, v.checkable_count) for v in flagged],
        )
    return candidates


__all__ = [
    "ADMIN_CONFIG_VENUE_LINK_AUDIT_ENABLED_KEY", "DEFAULT_VENUE_LINK_AUDIT_ENABLED",
    "MIN_NON_CORROBORATING_EVENTS",
    "validate_venue_link_audit_enabled_config", "load_venue_link_audit_enabled",
    "venue_corroborates_location_text",
    "MappedVenueAudit", "VenueLinkAuditCandidate",
    "compute_venue_link_audit", "collect_venue_link_audit",
]
