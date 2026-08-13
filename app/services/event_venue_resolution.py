"""The resolution ladder: map a promoter event's stated location to a known
venue, or admit that it cannot be done safely. See
plans/260804_instagram-promoter-events.md §D, reordered by
plans/260812_event-attribution-and-dates.md §A/§B.

Five rungs, tried in order, cheapest and most certain first. The FIRST
conclusive answer wins — later rungs are never even computed once an earlier
one resolves.

  1. @-mention of a known venue handle found in THIS EVENT'S OWN
     `location_text` (reverse `instagram.handle`). An IDENTITY the promoter
     stated about THIS event specifically, not a post-level guess. Free,
     exact, and never the promoter's own handle (a promoter posting about
     itself must never resolve a venue that way).
  2. Instagram's own location tag. Tolerant: an absent tag (unverified
     against live Apify data — see the plan) degrades straight to rung 3
     rather than failing anything.
  3. §B: neighbourhood/address match, restricted to a caller-bounded
     `same_account_venues` set (never the whole servable catalog) and only
     consulted when that set genuinely has more than one member — a bounded
     "which of this account's own branches" question, answered against each
     candidate's ADDRESS rather than its name (a neighbourhood like "CASA
     FORTE" routinely matches no venue NAME at all but is decisive against
     an address). See `_neighbourhood_match_candidates`.
  4. Name match on `location_text`, reusing the SAME similarity function the
     Instagram handle cascade already uses (`instagram_cascade_service.
     name_similarity`) — one definition of "these two names are the same
     place", not two that can drift apart — plus proximity:
     `venue_eligibility.haversine_km` breaks ties among same-named venues
     when the location tag supplied coordinates even though its NAME did not
     clear rung 2's bar. plans/260813_handle-attribution-hardening.md §A:
     every `@handle` token is stripped out of `location_text` BEFORE this
     rung ever sees it (`_strip_handles_for_name_match`) — an @handle is an
     exact identifier, not a name fragment, and fuzzy-scoring its characters
     against a venue's name is a category error, not a low-confidence match
     (`@mahalilacafe` scored 0.76 against "Maria Café" on the shared
     substring "café", well above the floor). When nothing with actual
     alphabetic content survives the strip, this rung does not run at all
     for that text.
  5. @-mention in the POST CAPTION — kept, but demoted below every per-event
     signal, and only trusted when it is UNAMBIGUOUS (the caption names
     exactly one known venue). A caption naming several known venues (a
     promoter roundup) is worthless as evidence for any ONE event and is
     refused rather than guessed: the event resolves to no venue rather than
     inheriting whichever venue the caption happened to mention first — the
     precedence bug measured at 487/494 wrong links in production. See
     `METHOD_AMBIGUOUS_CAPTION_REFUSAL` and `METHOD_VENUE_NOT_IN_CATALOG`.

An auto-link out of rung 4 additionally requires BOTH an absolute confidence
floor and a margin over the runner-up. The margin is the gate that matters: a
top score of 0.91 against a runner-up of 0.89 is a coin toss wearing a high
score, and this city has several venues with variants of the same name.
Failing either gate queues the event for review with its ranked candidates;
failing the floor outright leaves it unresolved. Rungs 1-3 are identities or
bounded-certain matches, not open-ended scores, so neither gate applies to
them.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from app.metrics import EVENT_VENUE_LINK_TOTAL, EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL
from app.services.instagram_cascade_service import name_similarity
from app.services.instagram_handle_sources import normalize_handle
from app.services.venue_eligibility import haversine_km

logger = logging.getLogger(__name__)

# Rung 1 (event's own location_text) AND rung 5 (post caption, demoted) both
# resolve via an exact @-mention identity — the SAME method value the ladder
# has always used for that identity class. What changed is WHICH text wins
# when both are present (plans/260812_event-attribution-and-dates.md §A);
# `METHOD_CAPTION_HANDLE_MENTION` is the new, distinct value for the demoted
# rung, so an operator auditing `linked_by` can tell "the event's own text
# said so" from "only the post caption said so" apart — the two used to be
# indistinguishable, which is how 487 of 494 links were silently wrong.
METHOD_HANDLE_MENTION = "handle_mention"
METHOD_CAPTION_HANDLE_MENTION = "caption_handle_mention"
METHOD_LOCATION_TAG = "location_tag"
METHOD_NAME_MATCH = "name_match"
# §B: an address/neighbourhood match within a caller-bounded same-account
# candidate set — see `_neighbourhood_match_candidates`.
METHOD_NEIGHBOURHOOD_MATCH = "neighbourhood_match"
# A resolution.method SENTINEL (never a linked_by value — RESOLUTION_
# UNRESOLVED always nulls linked_by in build_location_text_attribute_fn)
# distinguishing "the caption named more than one known venue and the event
# named none" from a genuine no-evidence unresolved — read by
# `build_location_text_attribute_fn` only to label the EVENT_VENUE_LINK_TOTAL
# metric; the persisted review_reason stays `unresolved_venue` either way
# (event_reconciliation's own unconditional rule).
METHOD_AMBIGUOUS_CAPTION_REFUSAL = "ambiguous_caption_refusal"
# A resolution.method SENTINEL for the OTHER new outcome §A names: the
# event's own location_text named a SPECIFIC @-handle that is not in
# `handle_index` at all — we know exactly where this is, we just do not
# carry that venue. Its literal value MUST equal
# `app.services.event_reconciliation.REVIEW_REASON_VENUE_NOT_IN_CATALOG` —
# not imported directly (event_reconciliation already imports
# RESOLUTION_MANUAL FROM this module; importing back would cycle) — kept in
# lockstep by tests/test_event_venue_resolution.py's own literal-equality
# guard. `build_location_text_attribute_fn` copies this STRING straight into
# `review_reason` when it sees this sentinel.
METHOD_VENUE_NOT_IN_CATALOG = "venue_not_in_catalog"
# plans/260811_merge-unresolved-into-resolved-sibling.md §D: NOT one of this
# ladder's four rungs — this event's OWN post never resolved a venue at all.
# Recorded on a resolved event's `linked_by` when it absorbs a venue-less
# handle-identity sibling (app.services.event_merge), so an operator auditing
# the link can tell "the post said so" (a rung above) from "a sibling post
# said so" (this one) — the SAME `linked_by` column, per migration 0024's own
# docstring ("a resolution method name for an automatic one"), never a
# parallel field.
METHOD_SIBLING_MERGE = "sibling_merge"

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
    build this from a fake without a real Venue model.

    `address` (plans/260812_event-attribution-and-dates.md §B) is the raw
    `venue.venue_address` string, consulted ONLY by the neighbourhood rung
    against a caller-bounded `same_account_venues` set — never by the name-
    match rung, which stays name-only exactly as before."""

    venue_id: str
    venue_name: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    address: Optional[str] = None


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
            lat=venue.venue_lat, lng=venue.venue_lng, address=venue.venue_address,
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
            lat=venue.venue_lat, lng=venue.venue_lng, address=venue.venue_address,
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


def _strip_handles_for_name_match(location_text: Optional[str]) -> Optional[str]:
    """plans/260813_handle-attribution-hardening.md §A: remove every
    `@handle` token from `location_text` before it is ever fed to rung 4's
    fuzzy name-similarity scorer. An `@handle` is an exact identifier — it
    either resolves against `handle_index` (rung 1) or it does not — never a
    fragment of a venue's NAME. Feeding it to `name_similarity` anyway is
    how `@mahalilacafe` (nothing left after stripping) scored 0.76 against
    "Maria Café" and `@espaco.muta` scored 0.73 against "Espaço Tucano": both
    comfortably clear `DEFAULT_CONFIDENCE_FLOOR` (0.55) on shared characters
    that mean nothing about the venue's identity.

    Uses the SAME `_MENTION_RE` rung 1/5 already use to find mentions
    (including the email-address guard, and handles that themselves contain
    dots or underscores — `@espaco.muta`, `@bar54_` are real production
    handles), so "what counts as a handle" has exactly one definition in
    this module.

    Returns `None` — never an empty or punctuation-only string — when
    nothing with actual alphabetic content survives the strip, which tells
    `resolve_event_venue` to skip rung 4 entirely for this text rather than
    name-match on whatever punctuation or digits happened to be left over.
    `@obarpraia Ponta Negra` strips to "Ponta Negra", a real place name and
    legitimate rung-4 input; `@mahalilacafe` strips to "", which returns
    `None`.
    """
    if not location_text:
        return location_text
    stripped = _MENTION_RE.sub("", location_text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not any(ch.isalpha() for ch in stripped):
        return None
    return stripped


def _name_match_candidates(
    location_text: Optional[str],
    venues: list[VenueLite],
    tag_coords: Optional[tuple[float, float]] = None,
) -> list[LinkCandidate]:
    """Rung 4: every venue scored against `location_text`, ranked best
    first. When `tag_coords` is available (the location tag supplied
    coordinates even though its name did not clear rung 2), distance to it
    breaks ties among equally-scored venues — the proximity tie-break. A
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


# A folded (case/accent-insensitive, whitespace-collapsed) location_text
# shorter than this is too generic to trust as a neighbourhood/address
# match on its own — guards against a stray one- or two-letter fragment
# matching nearly every address.
_MIN_NEIGHBOURHOOD_TEXT_LEN = 3


def _fold_for_containment(text: Optional[str]) -> str:
    """Case/accent-insensitive, whitespace-collapsed — the SAME
    normalisation `app.services.event_identity.normalize_title` applies to a
    title, reused here (not re-implemented) for comparing `location_text`
    against a venue's address."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text.strip().casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", folded)


def _neighbourhood_match_candidates(
    location_text: Optional[str], same_account_venues: list[VenueLite],
) -> list[LinkCandidate]:
    """§B: `location_text` against each SIBLING candidate's own ADDRESS
    (never its name — `_name_match_candidates` already owns that), by
    substring containment on folded text — "CASA FORTE" names no venue by
    NAME but is decisive against an address. Deliberately restricted to a
    caller-bounded `same_account_venues` set (never the whole servable
    catalog, and never called at all unless the caller has one — see
    `resolve_event_venue`): an address-substring match is generous enough
    that running it city-wide would drag an event to an unrelated venue that
    merely happens to share a neighbourhood name."""
    folded_location = _fold_for_containment(location_text)
    if len(folded_location) < _MIN_NEIGHBOURHOOD_TEXT_LEN:
        return []
    out = []
    for venue in same_account_venues:
        folded_address = _fold_for_containment(venue.address)
        if folded_address and folded_location in folded_address:
            out.append(LinkCandidate(
                venue_id=venue.venue_id, venue_name=venue.venue_name,
                method=METHOD_NEIGHBOURHOOD_MATCH, score=1.0,
                evidence={"location_text": location_text, "address": venue.address},
            ))
    return out


def _known_venue_mentions(
    text: Optional[str], *, by_id: dict, handle_index: dict, own_handle: Optional[str],
) -> tuple[list[tuple[str, VenueLite]], Optional[str]]:
    """Every @-mention in `text` that resolves to a KNOWN venue (never the
    promoter's own handle), in appearance order, as `(mention, venue)`
    pairs — plus the FIRST mention that looked like a real handle but
    resolved to NO known venue (or `None` if every mention resolved, or
    there were none at all). That second value is what lets rung 1 tell
    "the event names a specific place we do not carry" (§A's
    `METHOD_VENUE_NOT_IN_CATALOG`) apart from "the event names nothing at
    all"."""
    resolved: list[tuple[str, VenueLite]] = []
    unrecognized: Optional[str] = None
    for mention in extract_mentions(text):
        if own_handle is not None and mention == own_handle:
            continue  # never self-link
        venue_id = handle_index.get(mention)
        venue = by_id.get(venue_id) if venue_id else None
        if venue is not None:
            resolved.append((mention, venue))
        elif unrecognized is None:
            unrecognized = mention
    return resolved, unrecognized


def _venue_not_in_catalog_result(handle: str) -> ResolutionResult:
    """The ONE place `resolve_event_venue` returns
    `METHOD_VENUE_NOT_IN_CATALOG` (plans/260813_handle-attribution-
    hardening.md §B) — both the ladder's own end-of-line fallback and the
    ambiguous-caption branch's per-event-outranks-post-level short-circuit
    reach this, so the log line and the metric-visible `method` value can
    never drift between the two call sites. Logs the handle: this is the
    venue-acquisition backlog (Error Handling) — the most frequently named
    unknown handle across these logs is the venue most worth adding next."""
    logger.info(
        f"[EventVenueResolution] venue_not_in_catalog: event's own text names "
        f"unrecognized handle @{handle}"
    )
    return ResolutionResult(
        RESOLUTION_UNRESOLVED, None, METHOD_VENUE_NOT_IN_CATALOG, None, [],
    )


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
    # §B: a caller-bounded subset of `venues` sharing the SAME crawl
    # target/handle or brand as this event's source — e.g. two branches of
    # one account. `None` (the default) or a single-member list disables
    # rung 3 entirely: with nothing to disambiguate, there is no branch
    # question to answer, and every existing single-venue caller keeps
    # behaving exactly as before this rung existed.
    same_account_venues: Optional[list[VenueLite]] = None,
) -> ResolutionResult:
    """Run the ladder for one event and return its verdict.

    `location_tag` is `{"name": str, "lat": float, "lng": float}` or None —
    lat/lng are optional even when a tag is present, in which case rung 2's
    name check still runs but rung 4 gets no proximity tie-break.
    """
    by_id = {venue.venue_id: venue for venue in venues}
    own_handle = normalize_handle(promoter_handle)

    # ── Rung 1 — @-mention of a known venue handle in THIS EVENT'S OWN
    # location_text (plans/260812_event-attribution-and-dates.md §A: the
    # correct answer the model already extracted, consulted BEFORE the
    # post-level caption) ─────────────────────────────────────────────────
    event_mentions, unrecognized_event_handle = _known_venue_mentions(
        location_text, by_id=by_id, handle_index=handle_index, own_handle=own_handle,
    )
    if event_mentions:
        mention, venue = event_mentions[0]
        candidate = LinkCandidate(
            venue_id=venue.venue_id, venue_name=venue.venue_name,
            method=METHOD_HANDLE_MENTION, score=1.0,
            evidence={"mention": f"@{mention}", "source": "location_text"},
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

    # ── Rung 3 (§B) — neighbourhood/address match, bounded to the caller's
    # own same-account candidate set, only when there is genuinely more than
    # one branch to choose between ──────────────────────────────────────────
    if same_account_venues and len(same_account_venues) >= 2:
        neighbourhood_candidates = _neighbourhood_match_candidates(
            location_text, same_account_venues,
        )
        if len(neighbourhood_candidates) == 1:
            winner = neighbourhood_candidates[0]
            return ResolutionResult(
                RESOLUTION_AUTO, winner.venue_id, winner.method, winner.score,
                neighbourhood_candidates,
            )
        # Zero matches (nothing to go on) or more than one (genuinely
        # ambiguous between branches) both fall through to rung 4 — never
        # guessed here.

    # ── Rung 4 — name match, proximity tie-break ─────────────────────────
    # §A: strip @handle tokens before this rung ever sees `location_text` —
    # an @handle is an identity, not a name fragment (see
    # `_strip_handles_for_name_match`'s own docstring). `None` means nothing
    # with real alphabetic content survived the strip, so this rung does not
    # run at all for this text.
    name_match_text = _strip_handles_for_name_match(location_text)
    if name_match_text is None:
        candidates = []
        if location_text:
            EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL.inc()
    else:
        candidates = _name_match_candidates(name_match_text, venues, tag_coords)
    if candidates:
        ok, _reason = gate_auto_link(candidates, floor=confidence_floor, margin=margin)
        if ok:
            top = candidates[0]
            return ResolutionResult(
                RESOLUTION_AUTO, top.venue_id, top.method, top.score, candidates,
            )

    # ── Rung 5 — @-mention in the POST CAPTION, demoted, unambiguous-only
    # (plans/260812_event-attribution-and-dates.md §A) ──────────────────────
    caption_mentions, _unrecognized_caption_handle = _known_venue_mentions(
        caption, by_id=by_id, handle_index=handle_index, own_handle=own_handle,
    )
    distinct_caption_venues: dict[str, tuple[str, VenueLite]] = {}
    for mention, venue in caption_mentions:
        distinct_caption_venues.setdefault(venue.venue_id, (mention, venue))
    if len(distinct_caption_venues) == 1:
        mention, venue = next(iter(distinct_caption_venues.values()))
        candidate = LinkCandidate(
            venue_id=venue.venue_id, venue_name=venue.venue_name,
            method=METHOD_CAPTION_HANDLE_MENTION, score=1.0,
            evidence={"mention": f"@{mention}", "source": "caption"},
        )
        return ResolutionResult(
            RESOLUTION_AUTO, venue.venue_id, METHOD_CAPTION_HANDLE_MENTION, 1.0, [candidate],
        )
    if len(distinct_caption_venues) > 1:
        # plans/260813_handle-attribution-hardening.md §B: the event's own
        # text is per-event evidence; the caption is post-level evidence for
        # every event the post yields. plans/260812_event-attribution-and-
        # dates.md §A already established that the FORMER outranks the
        # LATTER for WHICH venue an event links to (rung 1 before rung 5) —
        # the same precedence applies to WHY an event has no venue. An
        # event whose own text names a SPECIFIC handle we do not carry has
        # concrete per-event evidence; a caption naming several venues is
        # only ambiguous POST-level evidence. Report the specific reason,
        # not the ambiguous one — measured live: without this, all 12
        # unlinked events on one incremental crawl of a roundup account
        # stored the generic `unresolved_venue` even though every one named
        # a specific unknown handle, because this branch returned first and
        # the venue_not_in_catalog check below was never reached for them.
        if unrecognized_event_handle is not None:
            return _venue_not_in_catalog_result(unrecognized_event_handle)

        # The caption names several known venues and the event's own text
        # gave nothing conclusive — worthless as evidence for THIS event.
        # Refuse rather than guess (the 487/494 bug): no venue, and a
        # method SENTINEL (never persisted to linked_by — see its own
        # docstring) so the metric can tell this refusal apart from a
        # genuine no-evidence unresolved.
        #
        # Logged with the mention count (plans/260812_event-attribution-
        # and-dates.md Error Handling): "a promoter whose EVERY post
        # refuses is a target that needs a different strategy, not a bug"
        # — an operator grepping logs needs the count to tell "one chatty
        # roundup" from "this account's captions are systematically
        # unusable as evidence."
        logger.info(
            "[EventVenueResolution] ambiguous caption refusal: caption names "
            f"{len(distinct_caption_venues)} known venues, event's own text "
            "gave nothing conclusive"
        )
        return ResolutionResult(
            RESOLUTION_UNRESOLVED, None, METHOD_AMBIGUOUS_CAPTION_REFUSAL, None, [],
        )

    # ── Nothing in rungs 1-5 resolved. The event's own location_text may
    # still have named a SPECIFIC handle we simply do not carry — concrete
    # evidence of exactly where this is, distinct from "no idea at all".
    if unrecognized_event_handle is not None:
        return _venue_not_in_catalog_result(unrecognized_event_handle)

    if not candidates:
        return ResolutionResult(RESOLUTION_UNRESOLVED, None, None, None, [])
    if candidates[0].score >= confidence_floor:
        return ResolutionResult(RESOLUTION_QUEUED, None, None, None, candidates)
    return ResolutionResult(RESOLUTION_UNRESOLVED, None, None, None, candidates)


# `method` is what makes EVENT_VENUE_LINK_TOTAL worth reading: whether links
# come from exact @-mentions/location tags or fuzzy name matching is the
# difference between a resolver that is working and one that is guessing
# successfully so far. `result` is auto, queued, or unresolved (never
# `manual` — a manual link is set by an operator PATCH, never by this ladder).
RESULT_LABEL = {
    RESOLUTION_AUTO: "auto",
    RESOLUTION_QUEUED: "queued",
    RESOLUTION_UNRESOLVED: "unresolved",
}

# The SAME shape `event_reconciliation.AttributeFn` names — duplicated as a
# type alias only (not the logic it describes) because event_reconciliation
# already imports FROM this module (RESOLUTION_MANUAL, above); importing
# the other way would cycle. `attribute(fields, event_id) -> (fields_to_
# merge, on_persisted)`, see `event_reconciliation.reconcile_post_events`'s
# own docstring for the full contract.
AttributeFn = Callable[[dict, str], tuple[dict, Optional[Callable[[], None]]]]


def build_location_text_attribute_fn(
    *,
    caption: Optional[str],
    location_tag: Optional[dict],
    promoter_handle: Optional[str],
    venues: list,
    handle_index: dict,
    venue_dao,
    now: datetime,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    margin: float = DEFAULT_MARGIN,
    location_text_fallback_to_caption: bool = False,
    attribution_outcomes: Optional[list] = None,
) -> AttributeFn:
    """THE one definition of "how a caller resolves one post's events against
    a bounded venue candidate set from each event's own `location_text`" —
    an `event_reconciliation.AttributeFn` closure, built once per POST
    (closing over the post-level `caption`/`location_tag`/`promoter_handle`
    and the caller's own `venues`/`handle_index`), called once per EVENT that
    post yields.

    Extracted from `PromoterCrawlService._process_post`'s own inline
    `_attribute` closure (plans/260811_extract-by-handle.md's coordination
    note) so there is ONE ladder call site, not two that can drift — this
    repo has already been bitten by exactly that shape of duplication four
    times (two escapers, two freeze rules, two cap resolutions, two
    field-list arrays). `PromoterCrawlService._process_post` (every live
    promoter/shared-handle crawl) and `EventExtractionService._run_handles`
    (a multi-venue handle's ALREADY-ARCHIVED posts, re-extracted with no
    re-crawl) both call this SAME function; neither builds its own ladder.

    `location_text_fallback_to_caption` — plans/260810_post-kind-and-post-
    extraction-attribution.md §C: default False, so a REAL promoter account
    (whose candidate set is the WHOLE servable catalog) is byte-for-byte
    unchanged — a caption is noisier free text than a promoter's own
    `location_text`, and name-matching it against the whole catalog risks a
    false match the bounded, few-candidate shared-handle/multi-venue-handle
    case does not. Both callers that pass `venues` bounded to one handle's
    own two or three venues pass `True`.

    Returns `(fields_to_merge, on_persisted)` per `reconcile_post_events`'
    contract: `on_persisted` writes `event_venue_link_candidate` rows and
    bumps `EVENT_VENUE_LINK_TOTAL` — deferred until AFTER the event row is
    committed, since that table's FK is real and non-deferrable (migration
    0024_promoter_accounts).
    """

    def _attribute(fields: dict, event_id: str) -> tuple[dict, Optional[Callable[[], None]]]:
        location_text = fields["location_text"]
        if not location_text and location_text_fallback_to_caption:
            location_text = caption
        resolution = resolve_event_venue(
            caption=caption, location_text=location_text,
            location_tag=location_tag, promoter_handle=promoter_handle,
            venues=venues, handle_index=handle_index,
            confidence_floor=confidence_floor, margin=margin,
            # §B: `location_text_fallback_to_caption=True` is EXACTLY the
            # signal both real bounded-candidate callers already set (see
            # this function's own docstring: "Both callers that pass
            # `venues` bounded to one handle's own two or three venues pass
            # `True`") — reused here rather than adding a second parameter
            # every call site would have to learn, so `venues` doubles as
            # `same_account_venues` for exactly the callers where that is
            # true by construction.
            same_account_venues=venues if location_text_fallback_to_caption else None,
        )

        def _on_persisted() -> None:
            if resolution.candidates:
                venue_dao.replace_event_venue_link_candidates(event_id, [
                    {
                        "venue_id": c.venue_id, "rank": rank, "score": c.score,
                        "method": c.method, "evidence": c.evidence,
                    }
                    for rank, c in enumerate(resolution.candidates, start=1)
                ])
            EVENT_VENUE_LINK_TOTAL.labels(
                method=resolution.method or "none",
                result=RESULT_LABEL[resolution.resolution],
            ).inc()

        if attribution_outcomes is not None:
            attribution_outcomes.append(resolution.resolution)

        if resolution.resolution == RESOLUTION_AUTO:
            result_fields = {
                "venue_id": resolution.venue_id,
                "location_resolution": RESOLUTION_AUTO,
                "location_confidence": resolution.confidence,
                "linked_by": resolution.method,
                "linked_at": now,
            }
        elif resolution.resolution == RESOLUTION_UNRESOLVED:
            result_fields = {
                "venue_id": None,
                "location_resolution": RESOLUTION_UNRESOLVED,
                "location_confidence": None,
                "linked_by": None,
                "linked_at": None,
            }
            if resolution.method == METHOD_VENUE_NOT_IN_CATALOG:
                # §A: the event's own text named a SPECIFIC handle we do not
                # carry — distinct from "no idea at all". Its literal value
                # IS `event_reconciliation.REVIEW_REASON_VENUE_NOT_IN_
                # CATALOG` (see METHOD_VENUE_NOT_IN_CATALOG's own docstring
                # for why this is a shared literal, not an import).
                # `reconcile_post_events`'s own unconditional-append rule
                # recognizes this value and skips layering
                # `unresolved_venue` on top of it.
                result_fields["review_reason"] = METHOD_VENUE_NOT_IN_CATALOG
        else:
            # RESOLUTION_QUEUED: leave the four link columns out of the
            # returned dict entirely — the reconciler's partial-update path
            # means they stay NULL, the "awaiting review" state.
            result_fields = {}
        return result_fields, _on_persisted

    return _attribute


__all__ = [
    "METHOD_HANDLE_MENTION", "METHOD_CAPTION_HANDLE_MENTION", "METHOD_LOCATION_TAG",
    "METHOD_NAME_MATCH", "METHOD_NEIGHBOURHOOD_MATCH", "METHOD_AMBIGUOUS_CAPTION_REFUSAL",
    "METHOD_VENUE_NOT_IN_CATALOG",
    "RESOLUTION_AUTO", "RESOLUTION_MANUAL", "RESOLUTION_UNRESOLVED", "RESOLUTION_QUEUED",
    "DEFAULT_CONFIDENCE_FLOOR", "DEFAULT_MARGIN", "LOCATION_TAG_MATCH_FLOOR",
    "VenueLite", "LinkCandidate", "ResolutionResult", "RESULT_LABEL", "AttributeFn",
    "extract_mentions", "gate_auto_link", "build_venue_catalog", "candidate_venues_for_ids",
    "build_handle_index", "resolve_event_venue", "build_location_text_attribute_fn",
]
