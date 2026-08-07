"""Cross-post event identity and field merge.

See plans/260807_one-event-many-posts.md. Three Instagram posts running a
countdown for one Saturday at Club Metrópole ("NOITE DA PATROA • 08/AGO",
"Faltam 2 dias...", "É AMANHÃ!") each produced their own `events.event` row,
because `UNIQUE (source_handle, source_shortcode, source_event_key)`
(migration 0025) is scoped PER POST, deliberately — it exists to make
re-extracting the SAME post idempotent, and does that correctly. Nothing was
ever built to notice that several posts describe one real-world night. This
module is that recognition.

Runs from two places that must never disagree about the rules — both import
these functions rather than re-implementing them, the same discipline 0025
established for `app.services.event_identity.compute_source_event_key`:
  - at runtime, `merge_touched_events` runs right after
    `app.services.event_reconciliation.reconcile_post_events` persists a
    post's events, so a NEW post never re-fragments an identity a previous
    post already established (the alternative — merge only during a one-time
    migration, then let re-extraction re-diverge — was rejected: after a
    non-merging migration, three events already share one identity and a
    freshly-extracted post has no deterministic rule for which to attach to);
  - migration `0026_event_sources`'s one-time historical collapse, which
    calls `compute_event_identity`/`choose_canonical`/`merge_event_fields`
    directly against real rows fetched over a raw connection (no ORM/DAO
    layer exists inside a migration).

## Identity

`(venue_id, starts_at::date, normalize_title(title))` — DATE, never datetime:
the countdown posts disagree about the clock time (one names none at all),
and including it would defeat the very merge this exists to perform. Reuses
`app.services.event_identity.normalize_title`, the SAME normalisation
`compute_source_event_key` hashes, so identity and per-post idempotency can
never disagree about what counts as "the same title".

`compute_event_identity` returns `None` whenever `venue_id` or `starts_at` is
missing — such an event is NEVER a merge candidate. Without a venue there is
no way to tell two events are at the same place; without a date there is no
way to tell they are the same night. Both are common for unresolved promoter
events, and merging on title alone would attribute one venue's event to
another's.

**Known limitation, preserved on purpose:** identity is title-based. Two
posts phrasing the same night as "Noite da Patroa" and "Equilibrium na
Metrópole" will not merge — there is no stronger signal available without
fuzzy matching, and fuzzy matching would also merge genuinely different
same-night events at the same venue, which is the worse error.

## Canonical selection

The SOLE confirmed-or-manually-linked event in the group, when there is
exactly one — an operator's decision always outranks the merge: the source
attaches, the operator's fields stand untouched, a divergence is flagged the
same way a single-post re-extraction already flags one
(`event_reconciliation.REVIEW_REASON_DIVERGES_FROM_CONFIRMED`). Otherwise the
OLDEST `event_id` — event ids are time-ordered ULIDs
(`app.services.event_reconciliation.new_event_id`), so plain string order IS
chronological order, with no extra timestamp comparison needed.

A group holding MORE THAN ONE confirmed/manually-linked event is never
collapsed: `choose_canonical` returns `None` and the caller must leave every
member of the group exactly as it is. An operator confirmed two rows: only
they can say those two rows are the same night, and guessing would destroy
one operator's decision as surely as merging title-alone would.

## Field merge (§C)

Per scalar, in order: exactly one source has a value -> use it; several
agree -> use it; several DISAGREE -> take the value belonging to the more
RECENTLY SEEN event (a later post in a campaign is more plausibly a
correction than a regression) and flag `review_reason=sources_disagree`,
naming no single field (the same posture `weekday_mismatch` and
`model_diverges_from_confirmed_record` already take: a value that had to be
chosen between is not a value presented as settled). `lineup` is a list, not
a scalar: it is UNIONED, preserving first-seen order and dropping duplicates
— a teaser naming two DJs and a later flyer naming five yields five, never a
contested choice.

A CONFIRMED canonical's fields are never recomputed by a merge: `duplicate`
never overwrites them. A difference is still flagged (divergence, not
disagreement — the operator's confirmed record is the one specific answer
being diverged FROM, not one of several equally-uncertain answers).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.metrics import EVENT_MERGE_TOTAL, EVENT_SOURCES_PER_EVENT
from app.services.event_identity import normalize_title
from app.services.event_reconciliation import REVIEW_REASON_DIVERGES_FROM_CONFIRMED

# Flagged on a (non-confirmed) canonical event when the fold hit a genuine,
# unresolvable scalar disagreement between two sources — never silently
# picked. See the module docstring's §C.
REVIEW_REASON_SOURCES_DISAGREE = "sources_disagree"

STATUS_CONFIRMED = "confirmed"
RESOLUTION_MANUAL = "manual"

# Scalars merged by §C. Identity columns (event_id, venue_id, starts_at —
# starts_at is part of identity, so two events reaching a merge already
# AGREE on the date; only the full timestamp can still differ, which is why
# it participates in scalar merge too), attribution (location_resolution/
# location_confidence/linked_by/linked_at), status, and review_reason are
# never touched by this loop — they are handled explicitly by the caller.
_SCALAR_MERGE_FIELDS = (
    "starts_at", "ends_at", "is_recurring", "recurrence_text", "title",
    "description", "ticket_url", "price_text", "location_text", "confidence",
)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _recency(event: dict) -> datetime:
    value = event.get("last_seen_at")
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return _EPOCH


def compute_event_identity(
    venue_id: Optional[str], starts_at: Optional[datetime], title: Optional[str],
) -> Optional[tuple]:
    """`(venue_id, calendar date, normalized title)`, or `None` when
    `venue_id` or `starts_at` is missing — see the module docstring for why
    such an event is never merged."""
    if not venue_id or starts_at is None:
        return None
    return (venue_id, starts_at.date(), normalize_title(title))


def _is_protected(event: dict) -> bool:
    return (
        event.get("status") == STATUS_CONFIRMED
        or event.get("location_resolution") == RESOLUTION_MANUAL
    )


def choose_canonical(events: list[dict]) -> Optional[dict]:
    """The event whose row (identity + fields) survives the collapse, or
    `None` when the group must be left entirely alone."""
    protected = [e for e in events if _is_protected(e)]
    if len(protected) > 1:
        return None
    if len(protected) == 1:
        return protected[0]
    return min(events, key=lambda e: e["event_id"])


def _union_lineup(*lineups) -> list:
    seen: set = set()
    out: list = []
    for lineup in lineups:
        for item in (lineup or []):
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _values_agree(field: str, a, b) -> bool:
    """Plain equality, EXCEPT `title`: two events reaching a merge already
    agree on `normalize_title(title)` by construction (that IS the identity
    they were grouped on) — comparing the raw strings would flag "NOITE DA
    PATROA" vs "Noite da Patroa" as a disagreement over casing/accents that
    was never really information in conflict, defeating the exact merge
    §B's "titles differing only in case and accents" scenario exists to
    prove clean."""
    if field == "title":
        return normalize_title(a) == normalize_title(b)
    return a == b


def _time_known(event: dict) -> bool:
    """Whether `event`'s `starts_at` carries a REAL stated clock time, or a
    defaulted midnight standing in for "no time was stated"
    (app.services.event_date_resolver.resolve_event_datetime: "a stated
    '00h' and a defaulted midnight land on the exact same starts_at
    instant"). Read from the SOURCE's own `raw_extraction["time_known"]`
    (folded in by both extraction paths) — the only place this distinction
    still exists once `starts_at` itself is a plain datetime. Defaults True
    (never suppress a value this function cannot prove is a default) when
    the flag is absent, e.g. an extraction_failed placeholder."""
    raw = event.get("raw_extraction")
    if not isinstance(raw, dict) or "time_known" not in raw:
        return True
    return bool(raw["time_known"])


def _is_empty(field: str, value, event: dict) -> bool:
    """A teaser naming only a date resolves `starts_at` to that date at a
    DEFAULTED midnight (never `None`) — treated as EMPTY for merge purposes
    so a later post's real time fills it in cleanly instead of being read as
    a contradiction (§C's "complement, don't overwrite"). Every other field
    (including `starts_at` when a real time WAS stated) uses plain
    None/empty-string emptiness."""
    if field == "starts_at" and value is not None:
        return not _time_known(event)
    return value in (None, "")


def merge_event_fields(canonical: dict, duplicate: dict) -> tuple[dict, Optional[str]]:
    """Fold `duplicate`'s fields into `canonical`'s per §C. Returns
    `(changed_fields, review_reason)`:
      - `changed_fields` is the partial update to apply to the canonical
        event row (never includes identity/attribution/status columns —
        those are the caller's concern);
      - `review_reason` is the value the canonical row's `review_reason`
        should become (which may be unchanged from what it already was).

    A confirmed canonical is frozen: `changed_fields` is always empty, and
    `review_reason` becomes `REVIEW_REASON_DIVERGES_FROM_CONFIRMED` the
    moment `duplicate` states a non-empty value for any scalar that differs
    from the canonical's own — never silently absorbed, never overwritten.
    """
    if canonical.get("status") == STATUS_CONFIRMED:
        diverges = any(
            not _is_empty(field, duplicate.get(field), duplicate)
            and not _values_agree(field, duplicate.get(field), canonical.get(field))
            for field in _SCALAR_MERGE_FIELDS
        )
        review_reason = (
            REVIEW_REASON_DIVERGES_FROM_CONFIRMED if diverges else canonical.get("review_reason")
        )
        return {}, review_reason

    changed: dict = {}
    disagreed = False
    for field in _SCALAR_MERGE_FIELDS:
        c_val = canonical.get(field)
        d_val = duplicate.get(field)
        c_empty = _is_empty(field, c_val, canonical)
        d_empty = _is_empty(field, d_val, duplicate)
        if c_empty and not d_empty:
            changed[field] = d_val
        elif d_empty or _values_agree(field, c_val, d_val):
            continue  # duplicate empty, or both agree — canonical value stands
        else:
            disagreed = True
            more_recent = duplicate if _recency(duplicate) > _recency(canonical) else canonical
            changed[field] = more_recent.get(field)

    merged_lineup = _union_lineup(canonical.get("lineup"), duplicate.get("lineup"))
    if merged_lineup != (canonical.get("lineup") or []):
        changed["lineup"] = merged_lineup

    review_reason = (
        REVIEW_REASON_SOURCES_DISAGREE if disagreed else canonical.get("review_reason")
    )
    return changed, review_reason


def merge_touched_events(venue_dao, event_ids: list[str], now: datetime) -> None:
    """Called once per post, immediately after
    `event_reconciliation.reconcile_post_events` persists it, with every
    event id that call touched (inserted or updated). For each, looks for
    another PRE-EXISTING event sharing its identity and folds them together
    — the runtime half of the collapse migration 0026 performs once,
    historically, so a post extracted after this feature ships never
    re-fragments an identity a previous post already established.

    `now` is accepted for signature symmetry with the rest of the
    reconciliation pipeline (and to leave room for a future
    last-merged-at bookkeeping column); the merge itself is driven entirely
    by each event's own already-stored `last_seen_at`.
    """
    del now
    absorbed: set[str] = set()
    for event_id in event_ids:
        if event_id in absorbed:
            continue
        _merge_one(venue_dao, event_id, absorbed)


def _merge_one(venue_dao, event_id: str, absorbed: set[str]) -> None:
    event = venue_dao.get_event(event_id)
    if event is None:
        return  # already absorbed by an earlier event_id in this same call

    identity = compute_event_identity(
        event.get("venue_id"), event.get("starts_at"), event.get("title"),
    )
    if identity is None:
        EVENT_MERGE_TOTAL.labels(outcome="no_identity").inc()
        return

    siblings = [
        sibling for sibling in venue_dao.list_events(venue_id=event["venue_id"])
        if sibling["event_id"] != event_id
        and sibling["event_id"] not in absorbed
        and compute_event_identity(
            sibling.get("venue_id"), sibling.get("starts_at"), sibling.get("title"),
        ) == identity
    ]
    if not siblings:
        EVENT_MERGE_TOTAL.labels(outcome="no_match").inc()
        return

    group = [event] + siblings
    canonical = choose_canonical(group)
    if canonical is None:
        EVENT_MERGE_TOTAL.labels(outcome="two_confirmed").inc()
        return

    canonical_id = canonical["event_id"]
    for other in group:
        if other["event_id"] == canonical_id:
            continue
        changed_fields, review_reason = merge_event_fields(canonical, other)
        update: dict = dict(changed_fields)
        if review_reason != canonical.get("review_reason"):
            update["review_reason"] = review_reason
        if update:
            venue_dao.update_event(canonical_id, update)
            canonical = venue_dao.get_event(canonical_id)
        venue_dao.reattach_event_sources(other["event_id"], canonical_id)
        # Candidate rows are ephemeral scoring evidence, recomputed on the
        # next crawl — never provenance, so clearing them (rather than
        # reattaching) before the hard delete below is correct, not lossy.
        venue_dao.replace_event_venue_link_candidates(other["event_id"], [])
        venue_dao.delete_event(other["event_id"])
        absorbed.add(other["event_id"])

    absorbed.add(canonical_id)
    EVENT_MERGE_TOTAL.labels(outcome="merged").inc()
    EVENT_SOURCES_PER_EVENT.observe(len(venue_dao.list_event_sources(canonical_id)))


__all__ = [
    "compute_event_identity", "choose_canonical", "merge_event_fields",
    "merge_touched_events", "REVIEW_REASON_SOURCES_DISAGREE",
]
