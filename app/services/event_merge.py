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

from datetime import datetime, timezone
from typing import Optional

from app.metrics import EVENT_MERGE_TOTAL, EVENT_SOURCES_PER_EVENT
from app.services.event_identity import normalize_title
from app.services.event_reconciliation import (
    PROTECTABLE_EVENT_FIELDS as _SCALAR_MERGE_FIELDS,
    REVIEW_REASON_DIVERGES_FROM_CONFIRMED,
    REVIEW_REASON_UNRESOLVED_VENUE,
    apply_operator_field_protection,
    event_field_is_absent as _is_empty,
    union_attractions as _union_attractions,
    union_lineup as _union_lineup,
)
from app.services.event_venue_resolution import METHOD_SIBLING_MERGE, RESOLUTION_AUTO

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


def _finish_absorption(venue_dao, duplicate_id: str, canonical_id: str, absorbed: set[str]) -> None:
    """Step 3+4 of any merge, venue-identity or handle-identity alike: re-
    point the duplicate's sources at the canonical event, drop its now-stale
    link candidates (ephemeral scoring evidence, recomputed on the next
    crawl — never provenance, so clearing rather than reattaching is
    correct, not lossy), and hard-delete the now-sourceless duplicate row.
    Factored out so the two merge directions can never drift on what
    "absorbed" means."""
    venue_dao.reattach_event_sources(duplicate_id, canonical_id)
    venue_dao.replace_event_venue_link_candidates(duplicate_id, [])
    venue_dao.delete_event(duplicate_id)
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


def merge_touched_events(venue_dao, event_ids: list[str], now: datetime) -> None:
    """Called once per post, immediately after
    `event_reconciliation.reconcile_post_events` persists it, with every
    event id that call touched (inserted or updated). For each, looks for
    another PRE-EXISTING event sharing its identity and folds them together
    — the runtime half of the collapse migration 0026 performs once,
    historically, so a post extracted after this feature ships never
    re-fragments an identity a previous post already established.

    `now` feeds `_absorb_unresolved_sibling`'s `linked_at` bookkeeping
    (plans/260811_merge-unresolved-into-resolved-sibling.md §D) when a
    handle merge adopts a venue — the merge decision itself is still driven
    entirely by each event's own already-stored `last_seen_at`
    (`_recency`), never by `now`.
    """
    absorbed: set[str] = set()
    for event_id in event_ids:
        if event_id in absorbed:
            continue
        _merge_one(venue_dao, event_id, absorbed, now)


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


__all__ = [
    "compute_event_identity", "compute_handle_identity", "choose_canonical",
    "merge_event_fields", "merge_touched_events", "REVIEW_REASON_SOURCES_DISAGREE",
]
