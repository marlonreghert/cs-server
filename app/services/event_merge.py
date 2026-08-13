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

## Handle identity (plans/260811_merge-unresolved-into-resolved-sibling.md)

`compute_event_identity` returning `None` for a venue-less event is correct
and stays exactly as it is — but it is not the only signal available. Two
posts from the SAME Instagram account, same calendar date, same normalized
title are the same announcement whether or not both posts named a venue: a
holiday-programme flyer lists the weeks AND the activities, a second post
lists the activities alone, and only one of the two happens to name the
venue. `compute_handle_identity` is that second key —
`(source_handle, starts_at::date, normalize_title(title))`, reusing the
IDENTICAL `normalize_title` and date-truncation `compute_event_identity`
already uses (one normalisation, never two) — used ONLY to attach an event
with no `venue_id` to a resolved sibling that already shares it. `starts_at`
is still required: a title alone is not an announcement, and the date is
what makes two posts about the same night recognisable at all.

**Direction is structural, never incidental.** The resolved member is
ALWAYS the canonical, the unresolved member is ALWAYS the duplicate —
`_merge_handle_group` picks the canonical via `choose_canonical` over the
RESOLVED subgroup only (never a mix of resolved and unresolved event ids),
so a venue-less event can never win regardless of which of a pair happens to
be processed first, which ULID happens to sort first, or which arrived at
the DAO last. This project has shipped exactly this ordering bug twice
before — an event attributed to whichever venue was processed last, and a
cursor advanced before the work it guarded — so `_merge_handle_group` is
called from BOTH `_merge_one` branches (a touched event with no venue_id
searches outward for a resolved sibling; a touched, already venue-identified
event ALSO searches for a waiting unresolved sibling), and re-derives its
whole candidate set fresh every call rather than trusting anything cached
from an earlier call — the same final set of facts must produce the same
outcome no matter which fact was touched last.

**Refuses to guess.** A handle can legitimately map to more than one venue
(`@entreamigosobode` does) — when the matching resolved siblings disagree on
`venue_id`, or a matching resolved subgroup itself has more than one
protected (confirmed/manually-linked) member, `_merge_handle_group` merges
NOTHING and leaves every member — resolved and unresolved alike — exactly as
it was. Per unresolved candidate, the SAME group protections a resolved-to-
resolved merge already honours are extended, never bypassed: `_is_protected`
(confirmed or manually-linked) and an `operator_edited_fields` that names
`venue_id` (an operator who cleared or set it made a decision) both refuse
that one candidate without touching the rest of the group.

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
contested choice. `attractions` (plans/260808_event-ticket-info-and-
attractions.md) is a richer list, unioned the same way via
`app.services.event_reconciliation.union_attractions` — grouped by
normalised name, with same-name entries on genuinely different stages kept
apart. `ticket_info` joins the scalar table above instead — a purchase
reference is a value to protect, not to accumulate.

A CONFIRMED canonical with NO record of which fields an operator edited
(`operator_edited_fields IS NULL` — a row confirmed before that column
existed, or confirmed without ever being PATCHed) is frozen exactly as
before: `duplicate` never overwrites any field, and a difference is still
flagged (divergence, not disagreement — the operator's confirmed record is
the one specific answer being diverged FROM, not one of several equally-
uncertain answers). See plans/260807_auto-accept-and-field-level-
protection.md's coordination note: this whole-row rule is now the LEGACY
fallback, not the only behaviour — a CONFIRMED canonical whose operator_
edited_fields IS a real list instead applies the SAME field-level table
`app.services.event_reconciliation` applies for a same-post re-extraction
(`apply_operator_field_protection`, imported from there rather than
reimplemented — the two runtime paths that can touch a confirmed event's
fields must never drift apart on this again): a field the operator never
edited still folds in `duplicate`'s answer (picking whichever source was
more recently seen, same as an unprotected canonical already does below);
an EDITED field holds and gets flagged only when it genuinely disagrees.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Optional

from app.metrics import EVENT_MERGE_TOTAL, EVENT_SOURCES_PER_EVENT
from app.models.event_kind import KIND_EVENT, KIND_MENU
from app.services import event_dedup
from app.services.event_identity import normalize_title
from app.services.event_reconciliation import (
    PROTECTABLE_EVENT_FIELDS as _SCALAR_MERGE_FIELDS,
    REVIEW_REASON_DIVERGES_FROM_CONFIRMED,
    REVIEW_REASON_UNRESOLVED_VENUE,
    STATUS_PENDING_REVIEW,
    STATUS_SUPERSEDED,
    apply_operator_field_protection,
    event_field_is_absent as _is_empty,
    union_attractions as _union_attractions,
    union_lineup as _union_lineup,
)
from app.services.event_venue_resolution import METHOD_SIBLING_MERGE, RESOLUTION_AUTO
from app.services.pipeline_run_registry import new_run_id

logger = logging.getLogger(__name__)

# Flagged on a (non-confirmed) canonical event when the fold hit a genuine,
# unresolvable scalar disagreement between two sources — never silently
# picked. See the module docstring's §C.
REVIEW_REASON_SOURCES_DISAGREE = "sources_disagree"

STATUS_CONFIRMED = "confirmed"
RESOLUTION_MANUAL = "manual"

# `_SCALAR_MERGE_FIELDS` (imported above as `PROTECTABLE_EVENT_FIELDS`) is
# the SAME set app.services.event_reconciliation protects for a same-post
# re-extraction — the two must never drift apart again (see the module
# docstring's coordination note). Identity columns (event_id, venue_id,
# starts_at — starts_at is part of identity, so two events reaching a merge
# already AGREE on the date; only the full timestamp can still differ,
# which is why it participates in scalar merge too), attribution
# (location_resolution/location_confidence/linked_by/linked_at), status,
# and review_reason are never touched by this loop — they are handled
# explicitly by the caller.

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


def compute_menu_identity(venue_id: Optional[str], title: Optional[str]) -> Optional[tuple]:
    """`(venue_id, normalized title)` — see plans/260811_menu-item-
    lifecycle.md §A. ONLY for `post_type == "menu"` rows (see
    `app.models.event_kind.KIND_MENU`); `compute_event_identity` stays the
    identity for every event AND promotion, completely untouched by this
    function's existence — see `merge_touched_events`'s dispatch for where
    that boundary is drawn.

    Deliberately, and ONLY here, WITHOUT a date — the reverse of `compute_
    event_identity`, and the reverse for a reason that must stay visible:
    for an EVENT, the date is what DISTINGUISHES two otherwise-identical
    listings ("Karaoke" on Friday and "Karaoke" on Saturday are two real
    nights, and merging them on title alone would silently lose one). For a
    DISH, the date is noise about when someone happened to post about it —
    "Especial do dia" posted in March and again in June is the same daily
    special, not two dishes. A production menu item makes the problem this
    function exists to fix concrete: `title='Especial do dia', starts_at=
    NULL, is_recurring=true` — no date, so `compute_event_identity` would
    return `None` for it forever, and every re-extraction (or future post
    about the same dish) would create another row.

    Reuses `normalize_title` — the SAME normalisation `compute_event_
    identity`/`compute_source_event_key` already hash, never a second one.
    `None` when `venue_id` is missing: an unresolved menu item has no
    identity, stays unmerged and stays queued — same posture as an
    unresolved event, no venue, no guess."""
    if not venue_id:
        return None
    return (venue_id, normalize_title(title))


def compute_handle_identity(event: dict) -> Optional[tuple]:
    """`(source_handle, calendar date, normalized title)` — see the module
    docstring's "Handle identity" section. Used ONLY to attach an event with
    no `venue_id` to a resolved sibling from the SAME account; never a
    replacement for `compute_event_identity`, which stays the identity for
    every resolved-to-resolved merge.

    Reuses `normalize_title` and the `starts_at.date()` truncation
    `compute_event_identity` already uses — one normalisation, never two.
    `None` when `source_handle` or `starts_at` is missing: a title alone is
    not an announcement, and an item with no date has no handle identity
    either, exactly as it has no venue identity."""
    handle = event.get("source_handle")
    starts_at = event.get("starts_at")
    if not handle or starts_at is None:
        return None
    return (handle, starts_at.date(), normalize_title(event.get("title")))


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


def _values_agree(field: str, a, b) -> bool:
    """Plain equality, EXCEPT `title`: two events reaching a merge already
    agree on `normalize_title(title)` by construction (that IS the identity
    they were grouped on) — comparing the raw strings would flag "NOITE DA
    PATROA" vs "Noite da Patroa" as a disagreement over casing/accents that
    was never really information in conflict, defeating the exact merge
    §B's "titles differing only in case and accents" scenario exists to
    prove clean. This is why `values_equal` is pluggable in
    `event_reconciliation.apply_operator_field_protection`: a same-post
    re-extraction (that module's OWN `_values_equal`) compares title RAW —
    see its docstring for why cosmetic re-casing is still worth a look
    there, unlike here."""
    if field == "title":
        return normalize_title(a) == normalize_title(b)
    return a == b


def merge_event_fields(canonical: dict, duplicate: dict) -> tuple[dict, Optional[str]]:
    """Fold `duplicate`'s fields into `canonical`'s per §C. Returns
    `(changed_fields, review_reason)`:
      - `changed_fields` is the partial update to apply to the canonical
        event row (never includes identity/attribution/status columns —
        those are the caller's concern);
      - `review_reason` is the value the canonical row's `review_reason`
        should become (which may be unchanged from what it already was).

    A confirmed canonical with `operator_edited_fields IS NULL` (a legacy
    row — confirmed before that column existed, or confirmed without ever
    being PATCHed) is frozen exactly as this shipped originally:
    `changed_fields` is always empty, and `review_reason` becomes
    `REVIEW_REASON_DIVERGES_FROM_CONFIRMED` the moment `duplicate` states a
    non-empty value for any scalar that differs from the canonical's own.
    Pinned deliberately: migration 0026's one-time historical collapse
    calls this SAME function directly against rows its own raw SQL built —
    rows that NEVER carry an `operator_edited_fields` key at all, since
    that column (migration 0027) postdates 0026 — so `canonical.get(
    "operator_edited_fields")` is always `None` there and this branch is
    the ONLY one 0026's replay can ever reach, unchanged from what it
    shipped with, with no special-casing required.

    A confirmed canonical whose `operator_edited_fields` IS a real list
    (plans/260807_auto-accept-and-field-level-protection.md's coordination
    note) instead applies the SAME per-field table a same-post
    re-extraction does — `app.services.event_reconciliation.
    apply_operator_field_protection`, imported rather than reimplemented:
    a field the operator never touched still folds in `duplicate`'s answer
    (recency-broken, exactly like an unprotected canonical below); an
    EDITED field holds and is flagged only when it genuinely disagrees.
    """
    if canonical.get("status") == STATUS_CONFIRMED:
        edited_fields = canonical.get("operator_edited_fields")
        if edited_fields is None:
            diverges = any(
                not _is_empty(field, duplicate.get(field), duplicate)
                and not _values_agree(field, duplicate.get(field), canonical.get(field))
                for field in _SCALAR_MERGE_FIELDS
            )
            review_reason = (
                REVIEW_REASON_DIVERGES_FROM_CONFIRMED if diverges else canonical.get("review_reason")
            )
            return {}, review_reason

        # Recency decided ONCE per call (not per field) — the same "more
        # recently seen source wins a disagreement" rule the unprotected
        # branch below already applies, just expressed as a fixed pick
        # between the two whole rows rather than a per-field comparison.
        prefer_duplicate = _recency(duplicate) > _recency(canonical)

        def _resolve_unedited_conflict(field: str, existing_value, new_value):
            del field
            return new_value if prefer_duplicate else existing_value

        changed, diverges = apply_operator_field_protection(
            existing=canonical, new=duplicate, fields=_SCALAR_MERGE_FIELDS,
            values_equal=_values_agree, resolve_conflict=_resolve_unedited_conflict,
        )
        if "starts_at" in changed:
            # `time_known` (plans/260811_expose-time-known.md) is
            # deliberately NOT a member of `_SCALAR_MERGE_FIELDS` — see
            # event_reconciliation._confirmed_update_fields's docstring for
            # why comparing it as an ordinary scalar would spuriously flag
            # REVIEW_REASON_DIVERGES_FROM_CONFIRMED. It travels WITH
            # starts_at instead, from whichever side just won it.
            changed["time_known"] = (
                duplicate.get("time_known", False) if prefer_duplicate
                else canonical.get("time_known", False)
            )
        merged_lineup = _union_lineup(canonical.get("lineup"), duplicate.get("lineup"))
        if merged_lineup != (canonical.get("lineup") or []):
            changed["lineup"] = merged_lineup
        merged_attractions = _union_attractions(canonical.get("attractions"), duplicate.get("attractions"))
        if merged_attractions != (canonical.get("attractions") or []):
            changed["attractions"] = merged_attractions
        review_reason = (
            REVIEW_REASON_DIVERGES_FROM_CONFIRMED if diverges else canonical.get("review_reason")
        )
        return changed, review_reason

    changed: dict = {}
    disagreed = False
    for field in _SCALAR_MERGE_FIELDS:
        c_val = canonical.get(field)
        d_val = duplicate.get(field)
        c_empty = _is_empty(field, c_val, canonical)
        d_empty = _is_empty(field, d_val, duplicate)
        if c_empty and not d_empty:
            changed[field] = d_val
            if field == "starts_at":
                # See merge_event_fields' confirmed branch above for why
                # time_known travels WITH starts_at rather than being its
                # own entry in _SCALAR_MERGE_FIELDS.
                changed["time_known"] = duplicate.get("time_known", False)
        elif d_empty or _values_agree(field, c_val, d_val):
            continue  # duplicate empty, or both agree — canonical value stands
        else:
            disagreed = True
            more_recent = duplicate if _recency(duplicate) > _recency(canonical) else canonical
            changed[field] = more_recent.get(field)
            if field == "starts_at":
                changed["time_known"] = more_recent.get("time_known", False)

    merged_lineup = _union_lineup(canonical.get("lineup"), duplicate.get("lineup"))
    if merged_lineup != (canonical.get("lineup") or []):
        changed["lineup"] = merged_lineup

    merged_attractions = _union_attractions(canonical.get("attractions"), duplicate.get("attractions"))
    if merged_attractions != (canonical.get("attractions") or []):
        changed["attractions"] = merged_attractions

    review_reason = (
        REVIEW_REASON_SOURCES_DISAGREE if disagreed else canonical.get("review_reason")
    )
    return changed, review_reason


def _fold_review_reason(canonical_reason: Optional[str], duplicate_reason: Optional[str]) -> Optional[str]:
    """The handle-merge path's OWN review_reason rule (plans/260811_merge-
    unresolved-into-resolved-sibling.md §"Say where the venue came from" —
    the venue-identity path never needs this: two events reaching THAT merge
    already both have a venue, so `unresolved_venue` never appears on either
    side). Unions both sides' `"; "`-separated reasons (the exact join
    `event_reconciliation.reconcile_post_events` writes), dropping
    `REVIEW_REASON_UNRESOLVED_VENUE` — the ONE reason a successful handle
    merge always resolves, since the duplicate's venue gap is exactly what
    adopting the canonical's venue just filled. Any OTHER reason the
    duplicate carried (e.g. a collapsed date range) survives, in first-seen
    order, never repeated when both sides already stated it."""
    reasons: list[str] = []
    for value in (canonical_reason, duplicate_reason):
        for reason in (value or "").split("; "):
            if reason and reason != REVIEW_REASON_UNRESOLVED_VENUE and reason not in reasons:
                reasons.append(reason)
    return "; ".join(reasons) if reasons else None


MERGE_MODE_DELETE = "delete"
MERGE_MODE_SUPERSEDE = "supersede"


def _finish_absorption(
    venue_dao, duplicate_id: str, canonical_id: str, absorbed: set[str], *, mode: str = MERGE_MODE_DELETE,
) -> None:
    """Step 3+4 of any merge: re-point the duplicate's sources at the
    canonical event, drop its now-stale link candidates (ephemeral scoring
    evidence, recomputed on the next crawl — never provenance, so clearing
    rather than reattaching is correct, not lossy), then dispose of the
    now-sourceless duplicate row per `mode`. Factored out so every merge
    direction can never drift on what "absorbed" means.

    plans/260812_event-dedup-fuzzy-title.md §E: an EXACT-identity merge
    (same venue, same date, byte-identical normalized title — there was
    nothing to be WRONG about) keeps `mode="delete"` — the ORIGINAL,
    UNCHANGED behaviour every existing caller still passes implicitly via
    the default, and the one migration 0026's historical replay depends on.
    A TITLE-SIMILARITY merge (this plan's own auto band) uses
    `mode="supersede"` instead: the row is never destroyed, only moved to
    `STATUS_SUPERSEDED` with `superseded_by` recording who absorbed it — the
    SAME vocabulary `event_reconciliation.py` already uses for "an item the
    pipeline retired but did not destroy" (already excluded from the review
    queue and every served path), reused rather than invented. That the two
    modes differ is a wart, not an oversight — unifying them is a follow-up,
    not smuggled in here (see the plan's own §E)."""
    venue_dao.reattach_event_sources(duplicate_id, canonical_id)
    venue_dao.replace_event_venue_link_candidates(duplicate_id, [])
    if mode == MERGE_MODE_DELETE:
        venue_dao.delete_event(duplicate_id)
    elif mode == MERGE_MODE_SUPERSEDE:
        venue_dao.update_event(duplicate_id, {
            "status": STATUS_SUPERSEDED, "superseded_by": canonical_id,
        })
    else:
        raise ValueError(f"_finish_absorption: unknown mode {mode!r}")
    absorbed.add(duplicate_id)


def _absorb_unresolved_sibling(
    venue_dao, canonical: dict, duplicate: dict, absorbed: set[str], now: datetime,
) -> dict:
    """Fold `duplicate` (an eligible, venue-less handle-identity match) into
    `canonical` (the resolved sibling it adopts) — the SAME field merge
    (`merge_event_fields`) and reattach/delete bookkeeping
    (`_finish_absorption`) a venue-identity merge already uses, so the two
    directions can never drift on what a merge does. Two things ONLY a
    handle merge needs on top:
      - `duplicate`'s own review reasons fold in via `_fold_review_reason`
        (dropping `unresolved_venue`, keeping the rest) — UNLESS canonical
        is confirmed, in which case `merge_event_fields`'s own confirmed
        branch already owns review_reason (an operator's word, or a
        genuine divergence from it) and nothing here should override that;
      - the adopted venue is recorded as such (module docstring's "Handle
        identity" section, plan §D) via `location_resolution`/`linked_by`
        — UNLESS canonical is already manually linked, which must never be
        overwritten by an automatic path (the SAME "manual outranks the
        model" rule `_is_protected`/`reconcile_post_events` already apply
        everywhere else a link can be touched).

    Returns the refreshed canonical row.
    """
    changed_fields, review_reason = merge_event_fields(canonical, duplicate)
    update: dict = dict(changed_fields)
    if canonical.get("status") != STATUS_CONFIRMED:
        review_reason = _fold_review_reason(review_reason, duplicate.get("review_reason"))
    if review_reason != canonical.get("review_reason"):
        update["review_reason"] = review_reason
    if canonical.get("location_resolution") != RESOLUTION_MANUAL:
        update["location_resolution"] = RESOLUTION_AUTO
        update["linked_by"] = METHOD_SIBLING_MERGE
        update["linked_at"] = now
    if update:
        venue_dao.update_event(canonical["event_id"], update)
        canonical = venue_dao.get_event(canonical["event_id"])
    _finish_absorption(venue_dao, duplicate["event_id"], canonical["event_id"], absorbed)
    return canonical


def _merge_handle_group(venue_dao, event: dict, absorbed: set[str], now: datetime) -> None:
    """Look for a handle-identity match for `event` — called from BOTH
    `_merge_one` branches (see the module docstring's "Direction is
    structural" note), so the outcome never depends on whether the
    resolved or the unresolved half of a pair happens to be touched, or
    which of them was processed first.

    Re-derives the WHOLE candidate group fresh every call — `event` plus
    every OTHER not-yet-absorbed event sharing its handle identity — rather
    than trusting anything about which side triggered this call, so the
    ambiguity check below sees every resolved sibling that exists at this
    moment regardless of arrival order.
    """
    identity = compute_handle_identity(event)
    if identity is None:
        EVENT_MERGE_TOTAL.labels(identity="handle", outcome="no_identity").inc()
        return
    handle, calendar_date, normalized_title = identity

    group = [event] + [
        candidate for candidate in venue_dao.list_events_by_handle(handle)
        if candidate["event_id"] != event["event_id"]
        and candidate["event_id"] not in absorbed
        and candidate.get("starts_at") is not None
        and candidate["starts_at"].date() == calendar_date
        and normalize_title(candidate.get("title")) == normalized_title
    ]

    resolved = [member for member in group if member.get("venue_id")]
    unresolved = [member for member in group if not member.get("venue_id")]
    if not resolved or not unresolved:
        EVENT_MERGE_TOTAL.labels(identity="handle", outcome="no_match").inc()
        return

    # A handle can legitimately map to more than one venue — refuse rather
    # than guess which resolved sibling is "the" venue. `choose_canonical`
    # is reused (not reimplemented) over the resolved subgroup ONLY: every
    # member of `resolved` already agrees on venue_id when there is exactly
    # one distinct value, exactly the precondition `choose_canonical` is
    # built for.
    if len({member["venue_id"] for member in resolved}) > 1:
        EVENT_MERGE_TOTAL.labels(identity="handle", outcome="ambiguous_venue").inc()
        return
    canonical = choose_canonical(resolved)
    if canonical is None:
        EVENT_MERGE_TOTAL.labels(identity="handle", outcome="ambiguous_venue").inc()
        return

    merged_any = False
    for duplicate in unresolved:
        # The SAME group protections a resolved-to-resolved merge already
        # honours, extended here rather than bypassed: an operator's
        # confirmation/manual link, or an operator_edited_fields entry for
        # venue_id (cleared or set — either way a decision), refuses THIS
        # candidate without touching the rest of the group.
        if _is_protected(duplicate):
            EVENT_MERGE_TOTAL.labels(identity="handle", outcome="confirmed_member").inc()
            continue
        if "venue_id" in (duplicate.get("operator_edited_fields") or []):
            EVENT_MERGE_TOTAL.labels(identity="handle", outcome="operator_edited").inc()
            continue
        canonical = _absorb_unresolved_sibling(venue_dao, canonical, duplicate, absorbed, now)
        merged_any = True

    if merged_any:
        EVENT_MERGE_TOTAL.labels(identity="handle", outcome="merged").inc()
        EVENT_SOURCES_PER_EVENT.observe(len(venue_dao.list_event_sources(canonical["event_id"])))


def _merge_menu_item(venue_dao, item: dict, absorbed: set[str], now: datetime) -> None:
    """Cross-post identity for a `post_type == "menu"` row — plans/260811_
    menu-item-lifecycle.md §A. Reuses `choose_canonical`/`merge_event_
    fields`/`_finish_absorption` COMPLETELY UNCHANGED — a dish and an event
    absorb into their canonical row via the exact same mechanics (oldest-
    ULID-or-protected wins, a null never overwrites a known value,
    `operator_edited_fields` wins) once two rows are found to share an
    identity; the ONLY thing that differs for a menu item is HOW that
    identity is computed (`compute_menu_identity`, never `compute_event_
    identity` — see its own docstring for why the two must stay apart) and
    HOW its siblings are found (every OTHER menu item at the same venue,
    never date-scoped, and never routed through `_merge_handle_group` —
    handle identity exists to attach a DATED, venue-less event to a
    resolved sibling from the same account, which has no equivalent for a
    dish: a venue-less menu item has no identity at all, per `compute_menu_
    identity`, and stays queued exactly like any other unresolved item)."""
    identity = compute_menu_identity(item.get("venue_id"), item.get("title"))
    if identity is None:
        EVENT_MERGE_TOTAL.labels(identity="menu", outcome="no_identity").inc()
        return

    siblings = [
        sibling for sibling in venue_dao.list_events(venue_id=item["venue_id"])
        if sibling["event_id"] != item["event_id"]
        and sibling["event_id"] not in absorbed
        and sibling.get("post_type") == KIND_MENU
        and compute_menu_identity(sibling.get("venue_id"), sibling.get("title")) == identity
    ]
    if not siblings:
        EVENT_MERGE_TOTAL.labels(identity="menu", outcome="no_match").inc()
        return

    group = [item] + siblings
    canonical = choose_canonical(group)
    if canonical is None:
        EVENT_MERGE_TOTAL.labels(identity="menu", outcome="two_confirmed").inc()
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
        _finish_absorption(venue_dao, other["event_id"], canonical_id, absorbed)

    absorbed.add(canonical_id)
    EVENT_MERGE_TOTAL.labels(identity="menu", outcome="merged").inc()
    EVENT_SOURCES_PER_EVENT.observe(len(venue_dao.list_event_sources(canonical_id)))


def merge_touched_events(
    venue_dao, event_ids: list[str], now: datetime, *, redis_like=None,
) -> None:
    """Called once per post, immediately after
    `event_reconciliation.reconcile_post_events` persists it, with every
    event id that call touched (inserted or updated). For each, looks for
    another PRE-EXISTING event/dish sharing its identity and folds them
    together — the runtime half of the collapse migration 0026 performs
    once, historically, so a post extracted after this feature ships never
    re-fragments an identity a previous post already established.

    Dispatches on `post_type` BEFORE touching anything else — see plans/
    260811_menu-item-lifecycle.md's own warning: a date-less identity
    applied to an EVENT would silently merge two real, differently-dated
    listings into one. `post_type == "menu"` (`app.models.event_kind.
    KIND_MENU`) routes to `_merge_menu_item`; EVERY OTHER post_type (event,
    promotion, or an unrecognised value stored verbatim) routes to
    `_merge_one` — the ORIGINAL, completely unmodified venue/handle-identity
    path (see tests/test_menu_item_lifecycle.py's `TestEventAndPromotion
    IdentityUnchangedByMenuDispatch`, pinned against this exact branch).

    `now` feeds `_absorb_unresolved_sibling`'s `linked_at` bookkeeping
    (plans/260811_merge-unresolved-into-resolved-sibling.md §D) when a
    handle merge adopts a venue — the merge decision itself is still driven
    entirely by each event's own already-stored `last_seen_at`
    (`_recency`), never by `now`.

    plans/260812_event-dedup-fuzzy-title.md §A/§C: AFTER the exact-identity
    pass above has run for every touched event (never before — this is a
    SECOND pass over what that one leaves behind, never a replacement for
    it), `run_title_similarity_pass` runs once per DISTINCT venue_id this
    call touched — never once per event, so a batch of several events at
    the SAME venue examines that venue's live cluster exactly once, and
    never for a touched event with no venue_id (title/lineup similarity
    needs `venue_id` for its own candidate window, same as the exact pass
    needs it for identity). `redis_like` is the caller's Redis client (or
    `None`, for a caller with none wired), read-through to
    `app.services.event_dedup.load_dedup_config`.
    """
    absorbed: set[str] = set()
    touched_venue_ids: set[str] = set()
    for event_id in event_ids:
        if event_id in absorbed:
            continue
        item = venue_dao.get_event(event_id)
        if item is None:
            continue  # already absorbed by an earlier event_id in this same call
        if item.get("venue_id"):
            touched_venue_ids.add(item["venue_id"])
        if item.get("post_type") == KIND_MENU:
            _merge_menu_item(venue_dao, item, absorbed, now)
        else:
            _merge_one(venue_dao, event_id, absorbed, now)

    for venue_id in touched_venue_ids:
        run_title_similarity_pass(venue_dao, venue_id, now, redis_like=redis_like)


def _merge_one(venue_dao, event_id: str, absorbed: set[str], now: datetime) -> None:
    event = venue_dao.get_event(event_id)
    if event is None:
        return  # already absorbed by an earlier event_id in this same call

    identity = compute_event_identity(
        event.get("venue_id"), event.get("starts_at"), event.get("title"),
    )
    if identity is None:
        # No venue_id (with a real starts_at) is the ONE case a handle
        # identity can still apply — see the module docstring. No starts_at
        # at all has no identity of either kind.
        if event.get("venue_id") is None and event.get("starts_at") is not None:
            _merge_handle_group(venue_dao, event, absorbed, now)
        else:
            EVENT_MERGE_TOTAL.labels(identity="venue", outcome="no_identity").inc()
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
        EVENT_MERGE_TOTAL.labels(identity="venue", outcome="no_match").inc()
        # This event is still a solo resolved event — it may itself be the
        # resolved sibling a handle-identity match is waiting for (see the
        # module docstring's "Direction is structural" note).
        _merge_handle_group(venue_dao, event, absorbed, now)
        return

    group = [event] + siblings
    canonical = choose_canonical(group)
    if canonical is None:
        EVENT_MERGE_TOTAL.labels(identity="venue", outcome="two_confirmed").inc()
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
        _finish_absorption(venue_dao, other["event_id"], canonical_id, absorbed)

    absorbed.add(canonical_id)
    EVENT_MERGE_TOTAL.labels(identity="venue", outcome="merged").inc()
    EVENT_SOURCES_PER_EVENT.observe(len(venue_dao.list_event_sources(canonical_id)))

    canonical = venue_dao.get_event(canonical_id)
    _merge_handle_group(venue_dao, canonical, absorbed, now)


# ── plans/260812_event-dedup-fuzzy-title.md: title containment (§B) and
# shared lineup (§B2), a SECOND pass over what the exact-identity pass above
# leaves behind. See app.services.event_dedup for the pure predicate this
# section applies — the SAME predicate `scripts/measure_event_dedup.py`
# imports, so the measurement and this runtime/sweep path can never
# disagree about a single pair (plan §C2's own requirement). Nothing below
# this line ever changes `compute_event_identity` or
# `compute_source_event_key`; it only decides whether two rows that ALREADY
# have their own separate identities describe one night. ──────────────────

# §F's own signal — not §B's title-containment and not §B2's shared-lineup,
# so it gets its own reason token, distinct from
# app.services.event_dedup.REASON_TITLE/REASON_LINEUP.
REASON_UNDATED_TWIN = "undated_twin"


def _venue_name_of(venue_dao, venue_id: Optional[str]) -> Optional[str]:
    """`venue_dao.get_venue` returns a plain dict from a bare DAO
    (`tests.rds_fake.InMemoryRdsVenueStore`/`RdsVenueStore` used directly,
    as this repo's own BDD steps do) but a `Venue` PYDANTIC MODEL when
    called through `app.dao.venue_repository.VenueRepository` (the real
    production wiring, and what several existing unit-test fixtures build
    too) — `venue_from_row` converts on the way out. Both shapes carry
    `venue_name`; this reads it either way rather than assuming one."""
    if not venue_id:
        return None
    venue = venue_dao.get_venue(venue_id)
    if venue is None:
        return None
    if isinstance(venue, dict):
        return venue.get("venue_name")
    return getattr(venue, "venue_name", None)


def _dedup_candidate_events(venue_dao, venue_id: str) -> list[dict]:
    """The live candidate pool for BOTH the pairwise pass and the undated-
    absorption pass at one venue: `post_type == KIND_EVENT` (plan §B2's
    non-event guard) AND `status != STATUS_SUPERSEDED` — a row this
    feature (or the exact-identity pass) has already absorbed must never
    re-enter consideration on a LATER call (a fresh crawl at the same
    venue, or a resumed sweep), or it could be evaluated against its own
    canonical again, or a stale row wrongly picked as some OTHER row's
    canonical. `venue_dao.list_events(venue_id=...)` itself applies no
    status filter (by design — callers with different needs read
    different subsets), so this repo's convention is that each caller
    filters for its own purpose; this is this feature's one, shared
    filter, so the pairwise pass and the undated pass can never quietly
    diverge on what counts as "still live" either."""
    return [
        e for e in venue_dao.list_events(venue_id=venue_id)
        if e.get("post_type") == KIND_EVENT and e.get("status") != STATUS_SUPERSEDED
    ]


_DECISION_PENDING = "pending"
_DECISION_AUTO_MERGED = "auto_merged"
_DECISION_APPLIED = "applied"
_DECISION_REJECTED = "rejected"
_DECISION_REVERSED = "reversed"


def _suggestion_id() -> str:
    return f"mrg_{new_run_id()}"


def _title_edited(row: dict) -> bool:
    return "title" in (row.get("operator_edited_fields") or [])


def _venue_edited(row: dict) -> bool:
    return "venue_id" in (row.get("operator_edited_fields") or [])


def _edge_blocked_for_absorption(canonical: dict, other: dict, reasons: tuple) -> bool:
    """plan §E: "a row whose operator_edited_fields names title is never
    absorbed by title similarity — the operator wrote that title... It may
    still be a suggestion." Extended here, by the same reasoning, to a row
    whose operator_edited_fields names venue_id — an operator who corrected
    a row's venue attribution made a decision a fuzzy merge must not
    override (the same "operator outranks the model" posture
    `_merge_handle_group`'s own venue_id-edited refusal already takes for
    the handle-identity path).

    Checked on BOTH members of the pair, not only the one that would end up
    absorbed: `choose_canonical`'s oldest-ULID rule can just as easily make
    the OPERATOR-EDITED row the survivor, and merging its title-alone
    partner into it would fold the OTHER side's content into the very row
    the operator corrected — using their edit as leverage for a merge they
    never asked for, exactly what this guard exists to prevent, regardless
    of which side happens to keep the row. The venue_id guard blocks
    absorption via EITHER signal; the title guard blocks absorption ONLY
    when title-containment is the ONLY reason this edge reached auto — a
    lineup-backed edge is untouched by an operator's title correction."""
    if _venue_edited(other) or _venue_edited(canonical):
        return True
    if reasons == (event_dedup.REASON_TITLE,) and (_title_edited(other) or _title_edited(canonical)):
        return True
    return False


def _pair_key(id_a: str, id_b: str) -> tuple:
    return (id_a, id_b) if id_a <= id_b else (id_b, id_a)


def _upsert_pending_suggestion(
    venue_dao, event_a: dict, event_b: dict, decision: "event_dedup.PairDecision", now: datetime,
) -> None:
    """Persist one SUGGEST-band pair as a pending `event_merge_suggestion`
    row — idempotent across repeated passes (a pair already surfaced as
    pending is never re-inserted). `event_id`/`candidate_event_id` are
    ordered by plain string comparison (never "which is canonical" — a
    pending suggestion has not decided that yet) purely so the SAME pair
    dedupes regardless of which order a caller happened to evaluate it in."""
    lo_id, hi_id = _pair_key(event_a["event_id"], event_b["event_id"])
    already_pending = any(
        s["event_id"] == lo_id and s["candidate_event_id"] == hi_id
        for s in venue_dao.list_event_merge_suggestions(candidate_event_id=hi_id, decision=_DECISION_PENDING)
    )
    if already_pending:
        return
    lo, hi = (event_a, event_b) if event_a["event_id"] == lo_id else (event_b, event_a)
    lo_words = decision.event_distinctive_words if lo is event_a else decision.candidate_distinctive_words
    hi_words = decision.candidate_distinctive_words if lo is event_a else decision.event_distinctive_words
    venue_dao.create_event_merge_suggestion({
        "suggestion_id": _suggestion_id(),
        "event_id": lo_id, "candidate_event_id": hi_id,
        "band": decision.band, "reasons": list(decision.reasons),
        "event_distinctive_words": list(lo_words), "candidate_distinctive_words": list(hi_words),
        "shared_lineup_names": list(decision.shared_lineup_names),
        "decision": _DECISION_PENDING, "created_at": now,
    })


def _observe_title_refusal(event_a: dict, event_b: dict, venue_name, config: "event_dedup.DedupConfig") -> None:
    """`EVENT_MERGE_TOTAL{identity="title"}` refusal outcomes (plan Error
    Handling And Observability) — computed independently of whether lineup
    also refused, since these two labels are specifically about the TITLE
    signal's own refusal reason."""
    venue_tokens = event_dedup.venue_name_tokens(venue_name)
    set_a = event_dedup.distinctive_set(
        event_a.get("title"), venue_tokens=venue_tokens,
        generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
    )
    set_b = event_dedup.distinctive_set(
        event_b.get("title"), venue_tokens=venue_tokens,
        generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
    )
    outcome = "refused_no_distinctive_tokens" if (not set_a or not set_b) else "refused_disjoint"
    EVENT_MERGE_TOTAL.labels(identity="title", outcome=outcome).inc()


def _absorb_title_similarity(
    venue_dao, canonical: dict, duplicate: dict, decision: "event_dedup.PairDecision",
    absorbed: set[str], now: datetime,
) -> dict:
    """Absorb `duplicate` into `canonical` via title/lineup similarity —
    the SAME `merge_event_fields` fold every other merge in this module
    uses, `_finish_absorption(mode="supersede")` instead of the exact-
    identity path's `mode="delete"` (plan §E), and one
    `event_merge_suggestion` audit row recording exactly which source ids
    moved and what the duplicate's status was before, so a reversal is
    mechanical (`reverse_title_similarity_merge`, below). Logs the merge at
    info with both titles, both distinctive sets and the canonical's id
    (plan Error Handling: "the record an operator reads when they ask why
    two listings became one, and it must be readable without a database").
    """
    moved_source_ids = [s["id"] for s in venue_dao.list_event_sources(duplicate["event_id"])]
    absorbed_status_before = duplicate.get("status")

    changed_fields, review_reason = merge_event_fields(canonical, duplicate)
    update: dict = dict(changed_fields)
    if review_reason != canonical.get("review_reason"):
        update["review_reason"] = review_reason
    if update:
        venue_dao.update_event(canonical["event_id"], update)
        canonical = venue_dao.get_event(canonical["event_id"])

    _finish_absorption(
        venue_dao, duplicate["event_id"], canonical["event_id"], absorbed, mode=MERGE_MODE_SUPERSEDE,
    )
    venue_dao.create_event_merge_suggestion({
        "suggestion_id": _suggestion_id(),
        "event_id": canonical["event_id"], "candidate_event_id": duplicate["event_id"],
        "band": event_dedup.BAND_AUTO, "reasons": list(decision.reasons),
        "event_distinctive_words": list(decision.event_distinctive_words),
        "candidate_distinctive_words": list(decision.candidate_distinctive_words),
        "shared_lineup_names": list(decision.shared_lineup_names),
        "decision": _DECISION_AUTO_MERGED, "moved_source_ids": moved_source_ids,
        "absorbed_status_before": absorbed_status_before, "created_at": now, "decided_at": now,
    })

    canonical = venue_dao.get_event(canonical["event_id"])
    EVENT_MERGE_TOTAL.labels(identity="title", outcome="merged").inc()
    EVENT_SOURCES_PER_EVENT.observe(len(venue_dao.list_event_sources(canonical["event_id"])))
    logger.info(
        "[EventMerge] title-similarity merge: canonical=%s title=%r <- absorbed=%s title=%r "
        "reasons=%s event_words=%s candidate_words=%s shared_lineup=%s",
        canonical["event_id"], canonical.get("title"), duplicate["event_id"], duplicate.get("title"),
        decision.reasons, decision.event_distinctive_words, decision.candidate_distinctive_words,
        decision.shared_lineup_names,
    )
    return canonical


def _run_pairwise_pass(
    venue_dao, events: list[dict], venue_name, config: "event_dedup.DedupConfig", now: datetime,
    *, record_suggestions: bool = True,
) -> None:
    """§B/§B2/§D over ONE venue's live `post_type == "event"` rows: build
    the auto-band graph (union-find), always surface every suggest-band
    edge, then (only when `config.auto_merge_enabled`) absorb every auto
    component via `choose_canonical` — REUSED, not re-derived, so a
    component with two protected members is left entirely alone exactly as
    an exact-identity group already is.

    `record_suggestions=False` (plan §C2: "Only the auto band sweeps ... a
    historical backlog of suggestions nobody asked for is queue landfill")
    is `scripts/measure_event_dedup.py --apply`'s ONE deliberate divergence
    from the runtime pipeline's own call — it still walks the identical
    graph and applies the identical auto absorptions, it just never writes
    a `event_merge_suggestion` row for a suggest-band or blocked-auto edge.
    The runtime pipeline (`merge_touched_events`) never passes this — every
    real crawl call keeps recording suggestions, exactly as before."""
    n = len(events)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    edges: dict[tuple, "event_dedup.PairDecision"] = {}
    for i, j in combinations(range(n), 2):
        a, b = events[i], events[j]
        if not event_dedup.in_candidate_window(
            a.get("starts_at"), b.get("starts_at"), window_hours=config.candidate_window_hours,
        ):
            continue
        decision = event_dedup.evaluate_pair(a, b, venue_name=venue_name, config=config)
        if decision is None:
            _observe_title_refusal(a, b, venue_name, config)
            continue
        edges[(i, j)] = decision
        if decision.band == event_dedup.BAND_AUTO:
            union(i, j)

    # Suggest-band evidence is ALWAYS surfaced — independent of
    # `auto_merge_enabled` (plan §C: "the suggest band may ship enabled: it
    # writes only derived rows and applies nothing").
    for (i, j), decision in edges.items():
        if decision.band == event_dedup.BAND_SUGGEST:
            if record_suggestions:
                _upsert_pending_suggestion(venue_dao, events[i], events[j], decision, now)
            EVENT_MERGE_TOTAL.labels(identity="title", outcome="suggested").inc()

    if not config.auto_merge_enabled:
        return

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    absorbed_ids: set[str] = set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        _absorb_component(
            venue_dao, events, idxs, edges, absorbed_ids, now, record_suggestions=record_suggestions,
        )


def _absorb_component(
    venue_dao, events: list[dict], idxs: list[int], edges: dict, absorbed_ids: set[str], now: datetime,
    *, record_suggestions: bool = True,
) -> None:
    members = []
    for i in idxs:
        row = venue_dao.get_event(events[i]["event_id"])
        if row is not None and row["event_id"] not in absorbed_ids:
            members.append((i, row))
    if len(members) < 2:
        return

    canonical = choose_canonical([row for _, row in members])
    if canonical is None:
        # Plan §E: "choose_canonical returning None for a group with two
        # protected members still means leave everything alone" — reused
        # unchanged. The whole component's auto edges are still surfaced as
        # suggestions rather than silently dropped: an operator confirming
        # two rows did not also ask never to be told they might be the
        # same night.
        EVENT_MERGE_TOTAL.labels(identity="title", outcome="refused_protected").inc()
        if record_suggestions:
            for (i, j), decision in edges.items():
                if decision.band == event_dedup.BAND_AUTO and i in idxs and j in idxs:
                    _upsert_pending_suggestion(venue_dao, events[i], events[j], decision, now)
        return

    for i, other in members:
        if other["event_id"] == canonical["event_id"] or other["event_id"] in absorbed_ids:
            continue
        incident = [
            decision for (x, y), decision in edges.items()
            if decision.band == event_dedup.BAND_AUTO and i in (x, y) and x in idxs and y in idxs
        ]
        unblocked = [d for d in incident if not _edge_blocked_for_absorption(canonical, other, d.reasons)]
        if unblocked:
            canonical = _absorb_title_similarity(venue_dao, canonical, other, unblocked[0], absorbed_ids, now)
        else:
            for decision in incident:
                if record_suggestions:
                    _upsert_pending_suggestion(venue_dao, canonical, other, decision, now)
                outcome = (
                    "refused_operator_title"
                    if decision.reasons == (event_dedup.REASON_TITLE,) and _title_edited(other)
                    else "refused_protected"
                )
                EVENT_MERGE_TOTAL.labels(identity="title", outcome=outcome).inc()


def _undated_absorption_pass(
    venue_dao, venue_id: str, now: datetime, config: "event_dedup.DedupConfig",
) -> None:
    """§F: the mirror of `_absorb_unresolved_sibling`, for a DATED gap
    instead of a venue gap. An undated row may be absorbed by a dated
    sibling, at the auto bar only, when they share `venue_id` AND
    `source_handle`, their distinctive sets are EQUAL (not merely a
    subset), and the undated row's `first_seen_at` is within `config.
    undated_window_days` of the dated sibling's `starts_at`. The dated row
    is ALWAYS canonical — direction is structural, never incidental, the
    same rule `_merge_handle_group`'s own docstring insists on. Restricted
    to `post_type == "event"` rows by the caller (`run_title_similarity_
    pass` already filters its `events` list before either pass runs), so
    the "never crosses `compute_menu_identity`'s dateless identity" gate
    (plan §F) holds by construction, not by a redundant check here."""
    events = _dedup_candidate_events(venue_dao, venue_id)
    venue_tokens = event_dedup.venue_name_tokens(_venue_name_of(venue_dao, venue_id))

    undated = [e for e in events if e.get("starts_at") is None]
    dated = [e for e in events if e.get("starts_at") is not None]
    if not undated or not dated:
        return

    window = timedelta(days=config.undated_window_days)
    absorbed_ids: set[str] = set()
    for u in undated:
        if u["event_id"] in absorbed_ids:
            continue
        if not u.get("source_handle"):
            continue
        if _is_protected(u) or _title_edited(u) or _venue_edited(u):
            continue
        first_seen = u.get("first_seen_at")
        if first_seen is None:
            continue
        u_set = event_dedup.distinctive_set(
            u.get("title"), venue_tokens=venue_tokens,
            generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
        )
        if not u_set:
            continue
        for d in dated:
            if d["event_id"] in absorbed_ids or d["event_id"] == u["event_id"]:
                continue
            if d.get("source_handle") != u.get("source_handle"):
                continue
            d_set = event_dedup.distinctive_set(
                d.get("title"), venue_tokens=venue_tokens,
                generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
            )
            if d_set != u_set:
                continue
            if abs(first_seen - d["starts_at"]) > window:
                continue
            decision = event_dedup.PairDecision(
                band=event_dedup.BAND_AUTO, reasons=(REASON_UNDATED_TWIN,),
                event_distinctive_words=tuple(sorted(d_set)),
                candidate_distinctive_words=tuple(sorted(u_set)), shared_lineup_names=(),
            )
            _absorb_title_similarity(venue_dao, d, u, decision, absorbed_ids, now)
            break


def run_title_similarity_pass(
    venue_dao, venue_id: Optional[str], now: datetime, *, redis_like=None,
    config: Optional["event_dedup.DedupConfig"] = None, record_suggestions: bool = True,
) -> None:
    """plans/260812_event-dedup-fuzzy-title.md §C/§D: the fuzzy-title/
    shared-lineup merge pass for ONE venue's currently-live event rows.
    Always computes and records SUGGEST-band evidence. Only APPLIES the
    auto band (title-containment §B, shared-lineup §B2, and the undated-
    twin absorption §F) when `event_dedup_auto_merge_enabled` reads true
    (plan §C, "the six things most likely to go wrong" #1) — while it reads
    false an auto-eligible pair is left completely untouched and
    UNRECORDED, never written as a 'pending' suggestion either: recording
    it would misrepresent an auto-only candidate as something an operator
    is meant to action by hand, when the flag's whole point is that turning
    it on is the operator's own deliberate act.

    Never a candidate at all: any row whose `post_type` is not
    `app.models.event_kind.KIND_EVENT` (plan §B2's non-event guard — `'31
    Anos'`/a promotion/a menu item never enters either pass's candidate
    pool, so it can neither absorb nor be absorbed, and there is no edge
    for it to appear on even as a suggestion).

    `config`, when given, is used AS-IS instead of reading `redis_like` —
    `scripts/measure_event_dedup.py --apply` uses this to force
    `auto_merge_enabled=True` for its own one-time historical sweep without
    touching the LIVE admin-config flag the ongoing pipeline reads (turning
    the sweep on is a separate, explicit decision from turning the ongoing
    pipeline's auto band on). `record_suggestions=False` is that same
    script's other divergence — see `_run_pairwise_pass`'s own docstring.
    Every other caller (the real crawl paths, via `merge_touched_events`)
    passes neither, reading the live config and always recording
    suggestions, exactly as before. Reused (never reimplemented) by that
    same script for its `--apply` sweep, so the measurement and the sweep
    can never disagree about a pair.
    """
    if not venue_id:
        return
    if config is None:
        config = event_dedup.load_dedup_config(redis_like)
    events = _dedup_candidate_events(venue_dao, venue_id)
    venue_name = _venue_name_of(venue_dao, venue_id)

    if len(events) >= 2:
        _run_pairwise_pass(venue_dao, events, venue_name, config, now, record_suggestions=record_suggestions)

    if config.auto_merge_enabled:
        _undated_absorption_pass(venue_dao, venue_id, now, config)


def apply_merge_suggestion(
    venue_dao, suggestion_id: str, now: datetime, *, decided_by: Optional[str] = None,
) -> dict:
    """plan §C: applying a SUGGEST-band pair is an explicit operator action
    — NEVER performed by the pipeline. Reuses the SAME `choose_canonical`/
    `merge_event_fields`/`_finish_absorption(mode="supersede")` path an
    auto-band merge uses, so a suggestion applied by hand is reversible and
    audited identically to one the pipeline applied itself."""
    suggestion = venue_dao.get_event_merge_suggestion(suggestion_id)
    if suggestion is None:
        raise ValueError(f"apply_merge_suggestion: no suggestion {suggestion_id!r}")
    if suggestion["decision"] != _DECISION_PENDING:
        raise ValueError(
            f"apply_merge_suggestion: suggestion {suggestion_id!r} is not pending "
            f"(decision={suggestion['decision']!r})"
        )
    a = venue_dao.get_event(suggestion["event_id"])
    b = venue_dao.get_event(suggestion["candidate_event_id"])
    if a is None or b is None:
        raise ValueError(f"apply_merge_suggestion: {suggestion_id!r} references a missing event")

    canonical = choose_canonical([a, b])
    if canonical is None:
        raise ValueError(
            f"apply_merge_suggestion: {suggestion_id!r} has two protected members — leave it alone"
        )
    duplicate = b if canonical["event_id"] == a["event_id"] else a

    moved_source_ids = [s["id"] for s in venue_dao.list_event_sources(duplicate["event_id"])]
    absorbed_status_before = duplicate.get("status")
    changed_fields, review_reason = merge_event_fields(canonical, duplicate)
    update: dict = dict(changed_fields)
    if review_reason != canonical.get("review_reason"):
        update["review_reason"] = review_reason
    if update:
        venue_dao.update_event(canonical["event_id"], update)
        canonical = venue_dao.get_event(canonical["event_id"])

    absorbed: set[str] = set()
    _finish_absorption(venue_dao, duplicate["event_id"], canonical["event_id"], absorbed, mode=MERGE_MODE_SUPERSEDE)
    venue_dao.update_event_merge_suggestion(suggestion_id, {
        "decision": _DECISION_APPLIED, "decided_at": now, "decided_by": decided_by,
        "moved_source_ids": moved_source_ids, "absorbed_status_before": absorbed_status_before,
        # The pending row's event_id/candidate_event_id ordering was
        # arbitrary (`_pair_key`'s plain string order) — overwritten here
        # with the ACTUAL canonical/duplicate roles `choose_canonical` just
        # decided, since reversal reads `candidate_event_id` as "the row to
        # restore" and must find the correct one.
        "event_id": canonical["event_id"], "candidate_event_id": duplicate["event_id"],
    })
    EVENT_MERGE_TOTAL.labels(identity="title", outcome="merged").inc()
    return venue_dao.get_event(canonical["event_id"])


def reject_merge_suggestion(
    venue_dao, suggestion_id: str, now: datetime, *, decided_by: Optional[str] = None,
) -> dict:
    """An operator's explicit "no" — the suggestion stops being surfaced as
    pending; neither event row is touched."""
    suggestion = venue_dao.get_event_merge_suggestion(suggestion_id)
    if suggestion is None:
        raise ValueError(f"reject_merge_suggestion: no suggestion {suggestion_id!r}")
    return venue_dao.update_event_merge_suggestion(suggestion_id, {
        "decision": _DECISION_REJECTED, "decided_at": now, "decided_by": decided_by,
    })


def reverse_title_similarity_merge(
    venue_dao, absorbed_event_id: str, now: datetime, *, decided_by: Optional[str] = None,
) -> dict:
    """plan §E: mechanical reversal — flip the superseded row back, detach
    EXACTLY the sources this specific merge moved (never the canonical's
    own, and never a source a LATER, unrelated merge subsequently attached
    to the canonical too — `moved_source_ids`, recorded at merge time, is
    what makes this precise rather than a guess)."""
    matches = [
        s for s in venue_dao.list_event_merge_suggestions(candidate_event_id=absorbed_event_id)
        if s["decision"] in (_DECISION_AUTO_MERGED, _DECISION_APPLIED)
    ]
    if not matches:
        raise ValueError(
            f"reverse_title_similarity_merge: no applied title-similarity merge found "
            f"for {absorbed_event_id!r}"
        )
    record = max(matches, key=lambda s: (s.get("decided_at") or s.get("created_at")))
    for source_id in (record.get("moved_source_ids") or []):
        venue_dao.reattach_event_source_by_id(source_id, absorbed_event_id)
    venue_dao.update_event(absorbed_event_id, {
        "status": record.get("absorbed_status_before") or STATUS_PENDING_REVIEW,
        "superseded_by": None,
    })
    venue_dao.update_event_merge_suggestion(record["suggestion_id"], {
        "decision": _DECISION_REVERSED, "decided_at": now, "decided_by": decided_by,
    })
    return venue_dao.get_event(absorbed_event_id)


__all__ = [
    "compute_event_identity", "compute_menu_identity", "compute_handle_identity",
    "choose_canonical", "merge_event_fields", "merge_touched_events",
    "REVIEW_REASON_SOURCES_DISAGREE",
    "MERGE_MODE_DELETE", "MERGE_MODE_SUPERSEDE", "REASON_UNDATED_TWIN",
    "run_title_similarity_pass", "apply_merge_suggestion", "reject_merge_suggestion",
    "reverse_title_similarity_merge",
]
