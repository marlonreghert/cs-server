"""The resolution ladder: map a promoter event's stated location to a known
venue, or admit that it cannot be done safely. See
plans/260804_instagram-promoter-events.md §D.

Four rungs, tried in order, cheapest and most certain first. The FIRST
conclusive answer wins — later rungs are never even computed once an earlier
one resolves, which is what makes rung 1 outrank rung 3 even when a name
match would have scored higher: certainty is not the same as score.

  1. @-mention of a known venue handle (reverse `instagram.handle`). An
     IDENTITY the promoter stated, not a similarity computed. Free, exact,
     and never the promoter's own handle (a promoter posting about itself
     must never resolve a venue that way).
  2. Instagram's own location tag. Tolerant: an absent tag (unverified
     against live Apify data — see the plan) degrades straight to rungs 3-4
     rather than failing anything.
  3. Name match on `location_text`, reusing the SAME similarity function the
     Instagram handle cascade already uses (`instagram_cascade_service.
     name_similarity`) — one definition of "these two names are the same
     place", not two that can drift apart.
  4. Name match plus proximity: `venue_eligibility.haversine_km` breaks ties
     among same-named venues when the location tag supplied coordinates even
     though its NAME did not clear rung 2's bar.

An auto-link out of rungs 3-4 additionally requires BOTH an absolute
confidence floor and a margin over the runner-up. The margin is the gate that
matters: a top score of 0.91 against a runner-up of 0.89 is a coin toss
wearing a high score, and this city has several venues with variants of the
same name. Failing either gate queues the event for review with its ranked
candidates; failing the floor outright leaves it unresolved. Rungs 1-2 are
identities, not scores, so neither gate applies to them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from app.services.instagram_cascade_service import name_similarity
from app.services.instagram_handle_sources import normalize_handle
from app.services.venue_eligibility import haversine_km

METHOD_HANDLE_MENTION = "handle_mention"
METHOD_LOCATION_TAG = "location_tag"
METHOD_NAME_MATCH = "name_match"

# Stored in events.event.location_resolution (migration 0024) — exactly the
# three values the column's docstring names.
RESOLUTION_AUTO = "auto"
RESOLUTION_MANUAL = "manual"
RESOLUTION_UNRESOLVED = "unresolved"
# NOT a location_resolution value. "Awaiting a decision" is the ABSENCE of
# one (location_resolution IS NULL) — a sentinel the caller reads to know
# "leave the four link columns untouched", never written to the column.
RESOLUTION_QUEUED = "queued"

DEFAULT_CONFIDENCE_FLOOR = 0.55
DEFAULT_MARGIN = 0.08
# How sure a location TAG's own name must be of a venue before rung 2 treats
# it as conclusive. High on purpose: a location tag is trusted like an
# identity (Instagram's own place database), not scored like a guess, so it
# must not double as a fuzzy name match at a lower bar than rung 3 uses.
LOCATION_TAG_MATCH_FLOOR = 0.85

# An "@" immediately preceded by a letter, digit, or dot is the local part of
# an email address ("contato@barx.com.br"), not a mention: an Instagram
# mention is always preceded by whitespace, punctuation, or the start of the
# caption. This is the one rule that tells the two apart.
_MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{1,30})")


def extract_mentions(caption: Optional[str]) -> list[str]:
    """Every @-mention in `caption`, normalized (lowercased, no leading @),
    in the order they appear. Not de-duplicated — the caller decides whether
    repeats matter. An email-shaped match is never returned."""
    if not caption:
        return []
    out = []
    for m in _MENTION_RE.finditer(caption):
        handle = normalize_handle(m.group(1))
        if handle:
            out.append(handle)
    return out


@dataclass(frozen=True)
class VenueLite:
    """The slice of a venue the ladder needs — nothing more, so a caller can
    build this from a fake without a real Venue model."""

    venue_id: str
    venue_name: str
    lat: Optional[float] = None
    lng: Optional[float] = None


@dataclass(frozen=True)
class LinkCandidate:
    venue_id: str
    venue_name: str
    method: str
    score: float
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ResolutionResult:
    resolution: str  # auto | queued | unresolved
    venue_id: Optional[str]
    method: Optional[str]
    confidence: Optional[float]
    candidates: list  # list[LinkCandidate], rank order (best first)


def gate_auto_link(
    candidates: list[LinkCandidate], *, floor: float, margin: float,
) -> tuple[bool, str]:
    """Both gates a scored top candidate must clear to auto-link: the
    absolute floor, and (when there IS a runner-up) the margin over it.

    The single-candidate branch is deliberate: with no runner-up there is
    nothing to compare against, so the floor alone decides. Requiring a
    margin here would make a lone, well-scored candidate impossible to ever
    auto-link — the opposite of what the floor is for.
    """
    if not candidates:
        return False, "no_candidates"
    top = candidates[0]
    if top.score < floor:
        return False, "below_floor"
    if len(candidates) == 1:
        return True, "single_candidate_above_floor"
    runner_up = candidates[1]
    if (top.score - runner_up.score) < margin:
        return False, "margin"
    return True, "auto"


def build_venue_catalog(venue_dao) -> list[VenueLite]:
    """The servable catalog, sliced to what the ladder needs. Servable, not
    every venue: a deprecated venue should not attract a new promoter link."""
    out = []
    for venue_id in venue_dao.list_servable_venue_ids() or []:
        venue = venue_dao.get_venue(venue_id)
        if venue is None:
            continue
        out.append(VenueLite(
            venue_id=venue_id, venue_name=venue.venue_name,
            lat=venue.venue_lat, lng=venue.venue_lng,
        ))
    return out


def candidate_venues_for_ids(venue_dao, venue_ids: list[str]) -> list[VenueLite]:
    """The SAME `VenueLite` shape `build_venue_catalog` builds, bounded to a
    caller-supplied `venue_ids` instead of the whole servable catalog —
    plans/260810_stream-dedupe-and-venue-attribution.md §C: a handle shared
    by several venues is the same resolution question as a promoter post,
    just with a candidate set of two or three instead of the whole city.
    Reuses `resolve_event_venue` unmodified against this narrower list
    rather than forking a second matcher. A `venue_ids` entry with no
    matching row (deleted between the handle lookup and this call) is
    silently omitted, mirroring `build_venue_catalog`'s own None-skip."""
    out = []
    for venue_id in venue_ids:
        venue = venue_dao.get_venue(venue_id)
        if venue is None:
            continue
        out.append(VenueLite(
            venue_id=venue_id, venue_name=venue.venue_name,
            lat=venue.venue_lat, lng=venue.venue_lng,
        ))
    return out


def build_handle_index(venue_dao) -> dict[str, str]:
    """Reverse `instagram.handle`: normalized handle -> venue_id. Free — this
    reads what the cascade already discovered, no new provider call."""
    return {
        normalize_handle(handle): venue_id
        for venue_id, handle in (venue_dao.list_instagram_handles() or {}).items()
        if handle
    }


def _name_match_candidates(
    location_text: Optional[str],
    venues: list[VenueLite],
    tag_coords: Optional[tuple[float, float]] = None,
) -> list[LinkCandidate]:
    """Rungs 3-4: every venue scored against `location_text`, ranked best
    first. When `tag_coords` is available (the location tag supplied
    coordinates even though its name did not clear rung 2), distance to it
    breaks ties among equally-scored venues — rung 4's proximity tie-break. A
    venue with no distance information never outranks one that has a real
    measurement at the same score.
    """
    if not location_text:
        return []
    scored: list[tuple[VenueLite, float, Optional[float]]] = []
    for venue in venues:
        score = name_similarity(venue.venue_name, location_text, None)
        if score <= 0:
            continue
        distance = None
        if tag_coords is not None and venue.lat is not None and venue.lng is not None:
            distance = haversine_km(tag_coords[0], tag_coords[1], venue.lat, venue.lng)
        scored.append((venue, score, distance))

    scored.sort(key=lambda t: (-t[1], t[2] if t[2] is not None else float("inf")))
    return [
        LinkCandidate(
            venue_id=venue.venue_id, venue_name=venue.venue_name,
            method=METHOD_NAME_MATCH, score=round(score, 4),
            evidence={
                "location_text": location_text,
                "name_score": round(score, 4),
                "distance_km": round(distance, 3) if distance is not None else None,
            },
        )
        for venue, score, distance in scored
    ]


def resolve_event_venue(
    *,
    caption: Optional[str],
    location_text: Optional[str],
    location_tag: Optional[dict],
    promoter_handle: Optional[str],
    venues: list[VenueLite],
    handle_index: dict[str, str],
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    margin: float = DEFAULT_MARGIN,
) -> ResolutionResult:
    """Run the ladder for one event and return its verdict.

    `location_tag` is `{"name": str, "lat": float, "lng": float}` or None —
    lat/lng are optional even when a tag is present, in which case rung 2's
    name check still runs but rung 4 gets no proximity tie-break.
    """
    by_id = {venue.venue_id: venue for venue in venues}
    own_handle = normalize_handle(promoter_handle)

    # ── Rung 1 — @-mention of a known venue handle ──────────────────────────
    for mention in extract_mentions(caption):
        if own_handle is not None and mention == own_handle:
            continue  # never self-link
        venue_id = handle_index.get(mention)
        venue = by_id.get(venue_id) if venue_id else None
        if venue is not None:
            candidate = LinkCandidate(
                venue_id=venue.venue_id, venue_name=venue.venue_name,
                method=METHOD_HANDLE_MENTION, score=1.0,
                evidence={"mention": f"@{mention}"},
            )
            return ResolutionResult(
                RESOLUTION_AUTO, venue.venue_id, METHOD_HANDLE_MENTION, 1.0, [candidate],
            )

    # ── Rung 2 — Instagram's own location tag (tolerant of absence) ─────────
    tag_name: Optional[str] = None
    tag_coords: Optional[tuple[float, float]] = None
    if location_tag:
        tag_name = location_tag.get("name")
        lat, lng = location_tag.get("lat"), location_tag.get("lng")
        if lat is not None and lng is not None:
            tag_coords = (float(lat), float(lng))

    if tag_name:
        best_venue: Optional[VenueLite] = None
        best_score = 0.0
        for venue in venues:
            score = name_similarity(venue.venue_name, tag_name, None)
            if score > best_score:
                best_venue, best_score = venue, score
        if best_venue is not None and best_score >= LOCATION_TAG_MATCH_FLOOR:
            candidate = LinkCandidate(
                venue_id=best_venue.venue_id, venue_name=best_venue.venue_name,
                method=METHOD_LOCATION_TAG, score=round(best_score, 4),
                evidence={"tag_name": tag_name},
            )
            return ResolutionResult(
                RESOLUTION_AUTO, best_venue.venue_id, METHOD_LOCATION_TAG,
                round(best_score, 4), [candidate],
            )

    # ── Rungs 3-4 — name match, proximity tie-break ─────────────────────────
    candidates = _name_match_candidates(location_text, venues, tag_coords)
    if not candidates:
        return ResolutionResult(RESOLUTION_UNRESOLVED, None, None, None, [])

    ok, _reason = gate_auto_link(candidates, floor=confidence_floor, margin=margin)
    if ok:
        top = candidates[0]
        return ResolutionResult(RESOLUTION_AUTO, top.venue_id, top.method, top.score, candidates)
    if candidates[0].score >= confidence_floor:
        return ResolutionResult(RESOLUTION_QUEUED, None, None, None, candidates)
    return ResolutionResult(RESOLUTION_UNRESOLVED, None, None, None, candidates)


__all__ = [
    "METHOD_HANDLE_MENTION", "METHOD_LOCATION_TAG", "METHOD_NAME_MATCH",
    "RESOLUTION_AUTO", "RESOLUTION_MANUAL", "RESOLUTION_UNRESOLVED", "RESOLUTION_QUEUED",
    "DEFAULT_CONFIDENCE_FLOOR", "DEFAULT_MARGIN", "LOCATION_TAG_MATCH_FLOOR",
    "VenueLite", "LinkCandidate", "ResolutionResult",
    "extract_mentions", "gate_auto_link", "build_venue_catalog", "candidate_venues_for_ids",
    "build_handle_index", "resolve_event_venue",
]
