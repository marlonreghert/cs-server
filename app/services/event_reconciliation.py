"""Shared per-post event reconciliation for the venue and promoter multi-event
extraction paths. See plans/260806_venue-post-multi-event.md.

Both `EventExtractionService` (venue posts) and `PromoterCrawlService`
(promoter posts) extract a LIST of events from one post — a single-event post
still yields a list of one (plans/260806_multi-event-posts.md) — and both must
reconcile that list against whatever was already persisted for the SAME post,
by content-derived `source_event_key` (app.services.event_identity), never by
list position: the model does not guarantee stable ordering between runs, so
an ordinal would silently migrate an operator's confirmation onto a DIFFERENT
event the moment the model's list order changed.

Before this module, the two callers each grew their OWN copy of this
mechanism, and the copies had already drifted: the venue path flagged a
divergence when a confirmed event's title/date stopped matching the model's
answer, the promoter path did not. This module is the ONE implementation, and
it unifies on the RICHER behaviour — the promoter path gains divergence
flagging by moving onto it.

Owns everything that must never differ between the two callers:
  - indexing the post's existing rows by `source_event_key`;
  - a `confirmed` row: attribution never moves, and every CONTENT field goes
    through `_confirmed_update_fields`'s FIELD-LEVEL protection (plans/
    260807_auto-accept-and-field-level-protection.md §B) — a null/absent
    fresh value never degrades a known one, an unedited field updates from
    the model's fresh answer, and an OPERATOR-EDITED field holds and gets
    the divergence FLAGGED via `review_reason`, WITHOUT moving status away
    from confirmed. A row whose `operator_edited_fields` is NULL (no record
    of what, if anything, was edited) keeps the original whole-row freeze
    instead — see that helper's docstring;
  - a manually-linked row (`location_resolution == "manual"`): every
    attribution column is left untouched (content still refreshes);
  - otherwise: a full upsert, auto-`accept`ed when `is_clean_extraction`
    holds (no review reason, a resolved date, a linked venue, confidence at
    or above the floor) and left `pending_review` otherwise — plans/
    260807_auto-accept-and-field-level-protection.md §A;
  - every existing key this run did not return: superseded, never deleted,
    never touched once confirmed or manually linked, and never touched (nor
    treated as a candidate for anything) when it has no `source_event_key`
    at all — an `extraction_failed` placeholder row, which predates content
    identity entirely;
  - a confirmed/manually-linked row that is orphaned (its stored key matched
    no fresh event) and could not be unambiguously paired with one either:
    left with its operator-owned fields untouched, but its own review_reason/
    last_seen_at ARE refreshed so an operator can see the post no longer
    yields it, rather than a silently stale row;
  - observing `EVENT_EXTRACTION_EVENTS_PER_POST`.

The ONE thing that genuinely differs between the two callers is per-event
VENUE ATTRIBUTION: a venue post already knows its venue (attribution is a
constant `venue_id`, and `location_resolution` is never touched); a promoter
post must run the resolution ladder on EACH event's own `location_text`,
never a sibling's. That is why `attribute` is the only caller-supplied hook —
see the plan's §B for why a second knob (e.g. "should confirmed events be
preserved?") would re-create the exact drift this refactor removes.
`attribute` is never invoked for a confirmed or manually-linked existing
row — attribution must never touch either.

`attribute` returns `(fields, on_persisted)`, not a bare dict: `fields` is
merged before the row is written, but any SIDE EFFECT that references the
event by id (the promoter path's `replace_event_venue_link_candidates`) must
run AFTER `insert_event`/`update_event`, never before — migration
0024_promoter_accounts declares `event_venue_link_candidate.event_id` as a
real, non-deferrable FK to `events.event`, so writing a candidate row for an
event that is not committed yet raises ForeignKeyViolation on real Postgres
for every first-time QUEUED or auto-linked event (the normal case). See
plans/260806_venue-post-multi-event.md's review.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from app.metrics import EVENT_EXTRACTION_EVENTS_PER_POST, EVENTS_TOTAL
from app.services.event_identity import compute_source_event_key, normalize_title
from app.services.event_venue_resolution import RESOLUTION_MANUAL
from app.services.pipeline_run_registry import new_run_id

logger = logging.getLogger(__name__)

STATUS_PENDING_REVIEW = "pending_review"
STATUS_CONFIRMED = "confirmed"
# Already defined (and unused) by 0023_event_table's status vocabulary —
# plans/260806_multi-event-posts.md was the first thing to write it: an event
# a later extraction no longer finds is superseded, never hard-deleted, and
# never touched at all once confirmed or manually linked.
STATUS_SUPERSEDED = "superseded"
# plans/260807_auto-accept-and-field-level-protection.md: a clean extraction
# (see `is_clean_extraction`) needs no human action at all. Deliberately a
# DIFFERENT word from `confirmed` — `accepted` means the pipeline had no
# doubts, `confirmed` means a person looked. An audit asking "what has a
# human actually seen?" needs to tell those apart; collapsing them would
# lose that answer. `accepted` is NOT exempt from supersession (see the
# bottom of this module) — only `confirmed`/manually-linked are, and that
# must stay true even once most events are accepted, or 0025/0026's
# supersede behaviour quietly becomes dead code.
STATUS_ACCEPTED = "accepted"

# Every status events.event can hold — the EVENTS_TOTAL gauge's own label
# set (see `update_events_gauge` below), listed explicitly rather than left
# to that function's defensive zero-fill so `events_total{status="accepted"}`
# reports an honest 0 from the very first run after deploy (plans/260807_
# auto-accept-and-field-level-protection.md) rather than no data point at
# all — a dashboard querying it must be able to tell "genuinely zero" from
# "never observed".
ALL_STATUSES = (
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, "rejected", STATUS_SUPERSEDED,
    "extraction_failed", STATUS_ACCEPTED,
)

# plans/260810_date-correctness-review-reasons-and-path-parity.md §C: an
# event queued for want of a venue must SAY SO — "an unresolved venue is a
# perfectly good reason and simply is not written down" was the operator's
# standing complaint. Set at every attribution call site that ends without a
# venue_id, on BOTH crawl paths (a venue's own resolution never fails, so in
# practice this fires only for a promoter/shared-handle event) — and it is
# also the fallback `_persist` below reaches for when nothing else queued
# the event, since a review reason has been forgotten at least once at every
# call site that could set one.
REVIEW_REASON_UNRESOLVED_VENUE = "unresolved_venue"
# The fallback reason for the residual case: a fresh row landed on
# `pending_review` for some OTHER combination `is_clean_extraction` checks
# (e.g. confidence recorded as None) with no venue_id gap and no reason any
# call site set. Kept distinct from REVIEW_REASON_UNRESOLVED_VENUE so a
# reader of the queue is never told "no venue" when a venue was in fact
# linked — see `_persist`'s own invariant enforcement, below in
# `reconcile_post_events`, for exactly when each of the two fires.
REVIEW_REASON_NEEDS_REVIEW = "needs_review"

# Flagged on a `confirmed` row whose title or resolved date no longer matches
# the model's fresh answer — the operator's record is never reverted, but the
# divergence must be visible. Originally the venue path's behaviour only
# (app.services.event_extraction_service._preserve_confirmed); the promoter
# path gains it by moving onto this shared module.
REVIEW_REASON_DIVERGES_FROM_CONFIRMED = "model_diverges_from_confirmed_record"

# Flagged on a confirmed/manually-linked row that is orphaned (no fresh event
# this run shares its key) and could not be unambiguously paired with one
# either (see `_plausibly_same_event`). NOT a divergence — there is no
# specific fresh answer to compare against, so nothing about the operator's
# record is contradicted — but silence is worse: an operator must be able to
# tell "this run's extraction no longer yields your confirmed/linked event"
# apart from "everything is still fine".
REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION = "confirmed_event_absent_from_latest_extraction"

# attribute(fields, event_id) -> (fields_to_merge, on_persisted). `fields`
# already carries every column this module and the caller have built for
# this event so far (identity/bookkeeping + the caller's prepared content),
# so the ladder can read e.g. `fields["location_text"]`. `event_id` is
# stable by the time this is called — the existing row's id, or a freshly
# minted one — so a callback that needs it never has to guess it.
# `fields_to_merge` is applied BEFORE the row is written. `on_persisted` (or
# None) is called with NO arguments AFTER the row is written — this is
# where a side effect that references the event by id belongs (the FK note
# above is why this is split at all).
AttributeFn = Callable[[dict, str], tuple[dict, Optional[Callable[[], None]]]]


def new_event_id() -> str:
    """A ULID: same time-ordered rationale as the archive run id — see
    app/services/pipeline_run_registry.new_run_id."""
    return f"evt_{new_run_id()}"


def is_clean_extraction(
    *,
    review_reason: Optional[str],
    starts_at,
    venue_id: Optional[str],
    confidence: Optional[float],
    min_confidence: float,
) -> bool:
    """True when a freshly persisted extraction needs no human action at
    all: no review reason, a resolved start date, a linked venue, and
    confidence at or above the floor. See plans/260807_auto-accept-and-
    field-level-protection.md §A — the four conditions are independent
    (e.g. `confidence >= min_confidence` is checked even though both real
    callers already fold a low-confidence reading into `review_reason`
    themselves, so this predicate stays correct on its own even if that
    ever stops being true) and ALL must hold; anything short of clean stays
    `pending_review`."""
    return (
        not review_reason
        and starts_at is not None
        and venue_id is not None
        and confidence is not None
        and confidence >= min_confidence
    )


# ── field-level protection for a confirmed/canonical row (§B) ───────────────
# SHARED between this module's own confirmed-branch (a same-post re-
# extraction, below) and app.services.event_merge's confirmed-canonical
# branch (a CROSS-post merge) — coordination note on plans/260807_auto-
# accept-and-field-level-protection.md: the two used to implement the same
# table twice (once here, once drifting in event_merge.py's blunt "always
# freeze" rule) and that inconsistency is worse than either rule alone, since
# an operator cannot predict which of the two runtime paths will touch their
# edit next. event_merge.py imports these names FROM here (never the other
# way — it already imports REVIEW_REASON_DIVERGES_FROM_CONFIRMED from this
# module, and this module must never import back, or the two would cycle).
#
# `PROTECTABLE_EVENT_FIELDS` is the SAME set app.services.event_merge used to
# call `_SCALAR_MERGE_FIELDS`; `event_field_is_absent` is the same rule it
# used to call `_is_empty`. Both callers agree on these EXACTLY — only the
# equality check and the conflict resolution for an unedited, differing
# field are caller-specific (see `apply_operator_field_protection`).
PROTECTABLE_EVENT_FIELDS = (
    "starts_at", "ends_at", "is_recurring", "recurrence_text", "title",
    "description", "ticket_url", "price_text", "location_text", "confidence",
    # plans/260808_event-ticket-info-and-attractions.md §B: an ordinary
    # scalar, merged exactly like price_text/location_text. `attractions` is
    # deliberately NOT here — see union_attractions below and §C: a list is
    # additive, never a value to CHOOSE a winner for.
    "ticket_info",
    # plans/260811_post-items-and-categories.md §B: `post_type` joins the
    # operator-editable fields — a misclassification is fixed in place
    # through `operator_edited_fields`, the same protection every other
    # scalar here already gets (the plan requires a test for exactly this).
    # `category` joins for the SAME reason ordinary scalars like
    # `price_text`/`location_text` are here: an unedited value keeps
    # refreshing from each re-extraction's fresh (canonicalized) answer,
    # through this table's existing generic per-field rule — `EventPatch`
    # (app/routers/admin_events_router.py) does not accept `category` in
    # THIS PR (the console is updated separately), so `operator_edited_
    # fields` can never actually contain "category" yet; being in this
    # tuple only buys the "keep it fresh" half today; the protection half
    # activates automatically once a PATCH route exists, with no reconciler
    # change needed then.
    "post_type", "category",
)


def event_time_known(event: dict) -> bool:
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


def event_field_is_absent(field: str, value, event: dict) -> bool:
    """`value` counts as null/absent for the per-field table's FIRST rule
    ("new value null/absent -> keep existing — never degrade"). A teaser
    naming only a date resolves `starts_at` to a DEFAULTED midnight (never
    `None`) — treated as absent here too, so a later post's real time can
    still fill it in without being read as a contradiction. Every other
    field uses plain None/empty-string emptiness — `value in (None, "")`
    never matches a real value (0.0, False, ...) for a non-text field, so
    this is safe to apply uniformly across every field in
    `PROTECTABLE_EVENT_FIELDS` without a separate text/non-text split."""
    if field == "starts_at" and value is not None:
        return not event_time_known(event)
    return value in (None, "")


def union_lineup(*lineups) -> list:
    """Preserve first-seen order, drop duplicates. A later post naming more
    performers is additive, never a contradiction — true even when the
    event's title has been operator-edited, since neither caller ever
    treats `lineup` as a field a PATCH can protect (it is not a member of
    `PROTECTABLE_EVENT_FIELDS`; both callers union it unconditionally,
    outside the per-field table below)."""
    seen: set = set()
    out: list = []
    for lineup in lineups:
        for item in (lineup or []):
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def union_attractions(*attraction_lists) -> list:
    """Fold every `{name, type, stage, styles}` attraction across posts into
    one additive list — the SAME rationale `union_lineup` already applies to
    bare names, one level richer (plans/260808_event-ticket-info-and-
    attractions.md §C). Called at the SAME sites `union_lineup` is: this
    module's own confirmed-row re-extraction branch, and BOTH branches of
    app.services.event_merge.merge_event_fields (which imports this
    function, never re-implements it — the same discipline `union_lineup`
    already established).

    Grouped by `normalize_title(name)` — the SAME casefold/accent/whitespace
    normalisation `event_identity` uses everywhere else two names are
    compared, so "DJ Thomas Henry" and "dj thomas henry " never register as
    two different acts by construction.

    Within a name group:
      - an entry whose `stage` matches an existing one for that name EXACTLY
        (including two nulls) merges into it;
      - a null `stage` absorbs into (or is absorbed by) a non-null one for
        the SAME name — a teaser naming the act with no stage and a later
        flyer placing it on one describe the same real slot;
      - two entries for the same name with DIFFERENT non-null stages stay
        SEPARATE — one DJ playing both pistas is two real slots, not one;
      - `type`: a specific "dj"/"live" beats "other"; two disagreeing
        specific types keep the FIRST seen (no principled way to prefer one
        caption's claim over another's, so the earliest answer stands, the
        same "first seen wins" posture styles/lineup already take);
      - `styles`: unioned, preserving first-seen order, exactly like
        `union_lineup` does for names.

    Every element of `attraction_lists` is assumed ALREADY validated by
    app.api.openai_event_extraction_client._normalize_attractions (name
    non-empty, type in ATTRACTION_TYPES, styles taxonomy-filtered) — this
    function only merges, it never re-validates.
    """
    slots: list[dict] = []

    def _find_slot(norm: str, stage) -> Optional[dict]:
        for slot in slots:
            if slot["_norm"] == norm and slot["stage"] == stage:
                return slot
        if stage is not None:
            for slot in slots:
                if slot["_norm"] == norm and slot["stage"] is None:
                    return slot
        else:
            for slot in slots:
                if slot["_norm"] == norm:
                    return slot
        return None

    for attractions in attraction_lists:
        for attraction in (attractions or []):
            norm = normalize_title(attraction.get("name"))
            stage = attraction.get("stage")
            a_type = attraction.get("type") or "other"
            styles = attraction.get("styles") or []

            slot = _find_slot(norm, stage)
            if slot is None:
                slots.append({
                    "_norm": norm, "name": attraction.get("name"),
                    "type": a_type if a_type in ("dj", "live") else "other",
                    "stage": stage, "styles": list(dict.fromkeys(styles)),
                })
                continue

            if slot["stage"] is None and stage is not None:
                slot["stage"] = stage
            if slot["type"] == "other" and a_type in ("dj", "live"):
                slot["type"] = a_type
            for style in styles:
                if style not in slot["styles"]:
                    slot["styles"].append(style)

    return [
        {"name": s["name"], "type": s["type"], "stage": s["stage"], "styles": s["styles"]}
        for s in slots
    ]


def apply_operator_field_protection(
    *,
    existing: dict,
    new: dict,
    fields: tuple,
    values_equal: Callable[[str, object, object], bool],
    resolve_conflict: Callable[[str, object, object], object],
) -> tuple[dict, bool]:
    """The per-field decision table (§B), shared by BOTH callers for a row
    whose `operator_edited_fields` is a REAL list (never call this for a
    NULL one — see each caller's own legacy branch, which the coordination
    note requires stay exactly what it already does today, since the two
    callers' legacy/whole-row rules pre-date this table and genuinely
    differ from each other by design):
      - `new`'s value for a field is null/absent -> keep `existing`'s
        (never degrade a known value with one a later source lacks);
      - equal (per `values_equal`) -> no-op;
      - the field is NOT in `existing["operator_edited_fields"]` and the
        two differ -> resolve via `resolve_conflict` (caller-specific: a
        same-post re-extraction always prefers the fresh answer; a cross-
        post merge prefers whichever source was more recently seen —
        exactly what a non-confirmed canonical already does);
      - the field IS operator-edited and the two differ -> keep `existing`'s
        value, and this call reports a divergence for the caller to flag.

    `values_equal` and `resolve_conflict` are pluggable because the two
    callers genuinely disagree on ONE thing: title equality. A same-post
    re-extraction compares it RAW (a cosmetic re-casing between two runs of
    the identical post is still worth an operator's eye); a cross-post
    merge compares it NORMALIZED (two posts about the same night already
    agree post-normalization BY CONSTRUCTION of the identity they were
    grouped on — see event_merge.compute_event_identity — so a raw-string
    difference there is never real information). `lineup` is deliberately
    NOT one of `fields` here — both callers union it themselves, outside
    this table, unconditionally.

    Returns `(changed_fields, diverges)`. `changed_fields` never includes
    `lineup` or any identity/bookkeeping column — the caller's concern.
    """
    edited_set = set(existing["operator_edited_fields"])
    changed: dict = {}
    diverges = False
    for field in fields:
        new_value = new.get(field)
        if event_field_is_absent(field, new_value, new):
            continue  # never degrade a known value with a null/absent one
        existing_value = existing.get(field)
        if values_equal(field, new_value, existing_value):
            continue  # nothing to do
        if field in edited_set:
            diverges = True  # keep the operator's value, flag the divergence
            continue
        resolved = resolve_conflict(field, existing_value, new_value)
        if not values_equal(field, resolved, existing_value):
            changed[field] = resolved
    return changed, diverges


# Fields whose "empty" value is the empty string as well as None, for
# `_values_equal` below — comparing a fresh "" against a stored real value
# must never register as a meaningful equality miss.
_TEXT_FIELDS = (
    "title", "description", "recurrence_text", "ticket_url", "price_text",
    "location_text",
    # plans/260808_event-ticket-info-and-attractions.md §B: without this, a
    # fresh "" compares unequal to a stored None on every re-extraction,
    # registering a phantom change — the exact half-fix the plan calls out
    # for omitting ticket_info from ONE of these two tuples but not the
    # other.
    "ticket_info",
)


def _values_equal(field: str, a, b) -> bool:
    """This module's OWN `values_equal` for `apply_operator_field_protection`
    — plain equality, folding None and "" together for text fields (a
    same-post re-extraction's title is compared RAW, never normalized; see
    that function's docstring for why this differs from event_merge's own
    version, which normalizes title specifically)."""
    if field in _TEXT_FIELDS:
        return (a or None) == (b or None)
    return a == b


def _confirmed_update_fields(existing: dict, prepared: dict) -> tuple[dict, bool]:
    """The confirmed-row re-extraction rule (§B/§3): `existing.
    operator_edited_fields` is NULL for a legacy row (migrated before this
    column existed, or confirmed without ever being PATCHed) — for those,
    keep TODAY'S whole-row protection unchanged (only title/starts_at are
    compared, matching the exact legacy divergence check; no content field
    ever updates). Once it is a real list, apply the SHARED per-field table
    (`apply_operator_field_protection`) — a null/absent fresh value never
    degrades a known one; an unedited field differing from the fresh
    answer updates; an EDITED field differing from the fresh answer keeps
    the operator's value and flags the divergence. `lineup` and (plans/
    260808_event-ticket-info-and-attractions.md) `attractions` union
    UNCONDITIONALLY in THIS branch only — additive, never a value the
    per-field table needs to choose a winner for — but, like every other
    content field, are left untouched by the legacy whole-row freeze above:
    a row with no record of what an operator edited gets NOTHING updated,
    lists included, exactly as it did before either union existed.

    Returns `(changed_fields, diverges)`: `changed_fields` never includes
    identity/bookkeeping columns (the caller adds those), and `diverges`
    tells the caller whether to set `REVIEW_REASON_DIVERGES_FROM_CONFIRMED`.
    """
    edited_fields = existing.get("operator_edited_fields")

    if edited_fields is None:
        # Legacy/never-edited row: which fields, if any, an operator
        # corrected is genuinely unknown — inventing an answer would either
        # freeze everything (today's bug) or nothing (losing an operator's
        # past work). Keep today's exact whole-row protection: only
        # title/starts_at are compared, nothing content-level ever updates.
        title_diverges = (prepared.get("title") or None) != (existing.get("title") or None)
        date_diverges = prepared.get("starts_at") != existing.get("starts_at")
        return {}, (title_diverges or date_diverges)

    changed, diverges = apply_operator_field_protection(
        existing=existing, new=prepared, fields=PROTECTABLE_EVENT_FIELDS,
        values_equal=_values_equal,
        # A same-post re-extraction IS the newer answer, by construction —
        # never a source whose recency needs comparing against the row's own.
        resolve_conflict=lambda field, existing_value, new_value: new_value,
    )

    merged_lineup = union_lineup(existing.get("lineup"), prepared.get("lineup"))
    if merged_lineup != (existing.get("lineup") or []):
        changed["lineup"] = merged_lineup

    merged_attractions = union_attractions(existing.get("attractions"), prepared.get("attractions"))
    if merged_attractions != (existing.get("attractions") or []):
        changed["attractions"] = merged_attractions

    return changed, diverges


def _plausibly_same_event(existing: dict, prepared: dict) -> bool:
    """True when EXACTLY ONE of the two `source_event_key` components
    (normalized title, resolved calendar date) changed between `existing`
    and `prepared` — the signature of the SAME event drifting (the model
    re-phrased its title, or the event moved to a new date), never of an
    unrelated event replacing it.

    Uses `normalize_title` — the SAME normalization `compute_source_event_
    key` hashes — so this check and the key itself can never disagree about
    what counts as "the same title". `existing`/`prepared` reaching this
    function already have DIFFERENT keys (that is why one was orphaned and
    the other unmatched), so "neither changed" cannot occur here; the only
    real question is whether exactly one changed (pair) or both did (do
    not — see the caller for what happens then).
    """
    same_title = normalize_title(prepared.get("title")) == normalize_title(existing.get("title"))

    existing_starts_at = existing.get("starts_at")
    prepared_starts_at = prepared.get("starts_at")
    existing_date = existing_starts_at.date() if existing_starts_at is not None else None
    prepared_date = prepared_starts_at.date() if prepared_starts_at is not None else None
    # An unresolved date is not evidence of a MATCHING date — "unknown" must
    # never compare equal to "unknown". Two genuinely different dateless
    # events would otherwise look like "same date" and pair on title alone
    # differing, when really neither date says anything at all.
    same_date = existing_date is not None and prepared_date is not None and existing_date == prepared_date

    return same_title != same_date


def reconcile_post_events(
    *,
    venue_dao,
    source_kind: str,
    source_handle: str,
    source_shortcode: str,
    source_permalink: Optional[str],
    prepared_events: list[dict],
    now: datetime,
    attribute: AttributeFn,
    touched_event_ids: Optional[list] = None,
    min_confidence: float = 0.5,
) -> int:
    """Reconcile one post's freshly-extracted events against the rows already
    persisted for `(source_handle, source_shortcode)`. Returns the number of
    events persisted this call.

    `min_confidence` feeds `is_clean_extraction` (plans/260807_auto-accept-
    and-field-level-protection.md §A) for every event this call inserts or
    updates outside the confirmed branch: a clean one is auto-`accepted`,
    anything short of that stays `pending_review`. Defaults to 0.5 only so
    existing direct callers (unit tests) that never cared about the accept/
    pending distinction keep working; both real callers
    (EventExtractionService, PromoterCrawlService) pass their own
    configured floor explicitly.

    `prepared_events` holds one dict per successfully-parsed event (a
    single-event post still passes a list of one). Each dict must already
    carry every persisted column EXCEPT:
      - the identity/bookkeeping fields this module injects itself
        (`source_event_key`, `source_event_index`, `source_kind`,
        `source_handle`, `source_shortcode`, `source_permalink`, `status`,
        `last_seen_at`, and `event_id`/`first_seen_at` on a fresh insert);
      - the attribution fields `attribute` supplies (`venue_id`,
        `location_resolution`, `location_confidence`, `linked_by`,
        `linked_at`).

    In particular each dict must include a resolved `starts_at` (or None)
    and `title` — the content-derived key and the confirmed-divergence check
    are computed from these two, never from list position or the model's raw
    date text — plus `raw_extraction`, built however the caller needs to
    (the venue path folds in `time_known`; the promoter path passes the
    model's parsed dict through unchanged). Date resolution itself
    (app.services.event_date_resolver.resolve_event_datetime) is intentionally
    NOT done here: both callers already call it identically, and several of
    its outputs (review_reason nuances, raw_extraction shape) legitimately
    stay caller-specific — see plans/260806_venue-post-multi-event.md.

    `touched_event_ids`, when given a list, gets every event id this call
    persisted (inserted or updated) appended to it — nothing is inserted
    into the list itself, so a caller passing its own list can read it back
    after this returns. This is how a caller feeds
    `app.services.event_merge.merge_touched_events` without this module
    importing that one (which would import back into this one for
    `REVIEW_REASON_DIVERGES_FROM_CONFIRMED`, a cycle) — see
    plans/260807_one-event-many-posts.md.
    """
    existing_events = venue_dao.list_events_by_source(source_handle, source_shortcode)
    existing_by_key = {
        row["source_event_key"]: row for row in existing_events
        if row.get("source_event_key") is not None
    }
    handled_event_ids: set[str] = set()
    persisted = 0

    def _persist(existing: Optional[dict], prepared: dict, index: int, key: str) -> None:
        nonlocal persisted

        # A `confirmed` row is the operator's word: attribution is always
        # left untouched (below), and every CONTENT field now goes through
        # `_confirmed_update_fields`'s per-field table (plans/260807_auto-
        # accept-and-field-level-protection.md §B) instead of the blunt
        # "freeze everything" rule this replaces — a legacy row (`operator_
        # edited_fields IS NULL`) still gets exactly that whole-row freeze,
        # so no operator's past work is put at risk by this change alone.
        # A divergence is flagged via review_reason WITHOUT moving status
        # away from confirmed. `source_event_key` is kept in step with the
        # model's fresh answer so a STABLE divergence (the model keeps
        # saying the same new thing) matches directly next time, without
        # needing the fallback pairing below.
        if existing is not None and existing.get("status") == STATUS_CONFIRMED:
            changed_fields, diverges = _confirmed_update_fields(existing, prepared)
            update_fields = {
                **changed_fields,
                "raw_extraction": prepared.get("raw_extraction"),
                "last_seen_at": now,
                "source_event_key": key,
                # Identifies WHICH of this (possibly multi-source, post-merge)
                # event's sources is being refreshed — without these, an
                # event carrying more than one source (plans/260807_one-
                # event-many-posts.md) has no unambiguous target for
                # raw_extraction/last_seen_at/source_event_key.
                "source_handle": source_handle,
                "source_shortcode": source_shortcode,
            }
            if diverges:
                update_fields["review_reason"] = REVIEW_REASON_DIVERGES_FROM_CONFIRMED
            venue_dao.update_event(existing["event_id"], update_fields)
            handled_event_ids.add(existing["event_id"])
            persisted += 1
            if touched_event_ids is not None:
                touched_event_ids.append(existing["event_id"])
            return

        # A manual link outranks the model too: a later run of the same post
        # must never move venue_id/location_resolution/location_confidence/
        # linked_by/linked_at once an operator has set them for THIS event.
        # `attribute` is simply never invoked, so none of those columns ever
        # appear in `fields` below — on an update that leaves them exactly
        # as they were (the DAO's partial-update contract). A fresh row can
        # never be pre-existing-manual in the first place.
        preserve_manual_link = (
            existing is not None and existing.get("location_resolution") == RESOLUTION_MANUAL
        )

        fields = {
            "source_kind": source_kind,
            "source_handle": source_handle,
            "source_shortcode": source_shortcode,
            "source_permalink": source_permalink,
            "source_event_key": key,
            "source_event_index": index,
            "status": STATUS_PENDING_REVIEW,
            "last_seen_at": now,
        }
        fields.update(prepared)

        if existing is None:
            event_id = new_event_id()
            fields["event_id"] = event_id
            fields["first_seen_at"] = now
        else:
            event_id = existing["event_id"]
            handled_event_ids.add(event_id)

        # `attribute` returns (fields_to_merge, on_persisted): the fields
        # are merged NOW (needed for the insert/update below), but
        # `on_persisted` — the promoter path's candidate-row write — must
        # not run until AFTER the event row itself is committed.
        # event_venue_link_candidate.event_id is a real, non-deferrable FK
        # to events.event (migration 0024_promoter_accounts); calling it
        # before insert_event raises ForeignKeyViolation on real Postgres
        # for every first-time QUEUED or auto-linked event — the normal
        # case, not an edge case. See plans/260806_venue-post-multi-
        # event.md's review.
        on_persisted: Optional[Callable[[], None]] = None
        if not preserve_manual_link:
            attribution_fields, on_persisted = attribute(fields, event_id)
            fields.update(attribution_fields)

        # Auto-accept a clean extraction (plans/260807_auto-accept-and-
        # field-level-protection.md §A), replacing the unconditional
        # STATUS_PENDING_REVIEW set above — for a fresh insert AND for an
        # existing non-confirmed row being re-extracted alike. `venue_id`
        # falls back to the EXISTING row's stored value when
        # `preserve_manual_link` skipped `attribute` entirely (so `fields`
        # never gained a `venue_id` key this call): a partial update leaves
        # it unchanged in the DB, and the predicate must see that real,
        # already-linked value rather than a false "no venue" absence.
        if "venue_id" in fields:
            effective_venue_id = fields["venue_id"]
        elif existing is not None:
            effective_venue_id = existing.get("venue_id")
        else:
            effective_venue_id = None

        # plans/260810_date-correctness-review-reasons-and-path-parity.md
        # §C: "an unresolved venue is a perfectly good reason and simply is
        # not written down" — added unconditionally whenever this event has
        # no venue, on TOP of whatever the caller's own date/confidence
        # reasons already say (both can be true at once: a low-confidence
        # extraction with no venue either), never in place of them.
        reasons = [fields["review_reason"]] if fields.get("review_reason") else []
        if effective_venue_id is None:
            reasons.append(REVIEW_REASON_UNRESOLVED_VENUE)
        fields["review_reason"] = "; ".join(reasons) if reasons else None

        fields["status"] = (
            STATUS_ACCEPTED
            if is_clean_extraction(
                review_reason=fields["review_reason"],
                starts_at=fields.get("starts_at"),
                venue_id=effective_venue_id,
                confidence=fields.get("confidence"),
                min_confidence=min_confidence,
            )
            else STATUS_PENDING_REVIEW
        )
        # The invariant, enforced ONCE, here, rather than trusted to every
        # call site that can queue an event — every one of them has now
        # forgotten a reason at least once (plans/260810_date-correctness-
        # review-reasons-and-path-parity.md §C's own evidence: 13 queued
        # events, all with a null review_reason). Whatever residual
        # not-clean condition reaches this point with STILL no reason at
        # all (confidence recorded as None, say, with a venue already
        # linked and a resolved date) gets a generic fallback instead of
        # silence.
        if fields["status"] == STATUS_PENDING_REVIEW and not fields["review_reason"]:
            fields["review_reason"] = REVIEW_REASON_NEEDS_REVIEW

        if existing is None:
            # A fresh row has no prior value for the four link columns to
            # fall back on — unlike an update, where omitting them from
            # `fields` correctly leaves whatever was already stored (NULL,
            # from that same row's own first insert) untouched. The real
            # store's column default covers this for an insert; the
            # in-memory fake needs it explicit.
            fields.setdefault("venue_id", None)
            fields.setdefault("location_resolution", None)
            fields.setdefault("location_confidence", None)
            fields.setdefault("linked_by", None)
            fields.setdefault("linked_at", None)
            venue_dao.insert_event(fields)
        else:
            venue_dao.update_event(event_id, fields)

        if on_persisted is not None:
            on_persisted()

        persisted += 1
        if touched_event_ids is not None:
            touched_event_ids.append(event_id)

    unmatched: list[tuple[int, dict, str]] = []
    seen_keys_this_run: set[str] = set()
    for index, prepared in enumerate(prepared_events, start=1):
        key = compute_source_event_key(prepared.get("title"), prepared.get("starts_at"))
        if key in seen_keys_this_run:
            # compute_source_event_key's own docstring already names this:
            # two events in the SAME post with the same normalized title and
            # the same resolved date collapse onto one key — there is no
            # stronger identity signal available. Persisting a second row
            # for it would violate the real (source_handle, source_shortcode,
            # source_event_key) UNIQUE constraint (uq_event_source_key), so
            # the duplicate is skipped rather than crashing the whole post;
            # the first occurrence already represents this key for this run.
            logger.warning(
                f"[EventReconciliation] duplicate source_event_key within "
                f"one post ({source_handle}/{source_shortcode}); skipping a "
                f"second row for title={prepared.get('title')!r}"
            )
            continue
        seen_keys_this_run.add(key)
        existing = existing_by_key.get(key)
        if existing is None:
            unmatched.append((index, prepared, key))
            continue
        _persist(existing, prepared, index, key)

    # A confirmed or manually-linked row whose stored key matched nothing
    # this run is not necessarily evidence a DIFFERENT event replaced it —
    # the model's OWN answer for that same event may simply have moved
    # enough (a new title, a new date) to change the content-derived key,
    # which is exactly what a divergence flag exists to catch. Only pair
    # when it is UNAMBIGUOUS (exactly one orphaned protected row and exactly
    # one unmatched fresh event) — with more than one on either side there is
    # no principled way to guess which fresh event corresponds to which
    # orphaned row, so they are left to the normal insert/supersede paths
    # below instead of risking a wrong pairing.
    #
    # Cardinality alone is not enough, though: a confirmed event genuinely
    # REPLACED by an unrelated one is ALSO "exactly one orphaned, exactly one
    # unmatched" — nothing about the counts distinguishes "the same event
    # moved" from "a different event arrived while the old one vanished".
    # `_plausibly_same_event` is the extra check: pair only when EXACTLY ONE
    # of the two key components changed (same title/different date, or same
    # date/different title) — the signature of the SAME event drifting.
    # When BOTH changed, they are treated as two unrelated events: the
    # protected row is left alone (already exempt from supersession below)
    # and the fresh event is inserted as its own row instead of being
    # silently absorbed into the confirmed/manual row and lost.
    orphaned_protected = [
        row for row in existing_events
        if row["event_id"] not in handled_event_ids
        and (row.get("status") == STATUS_CONFIRMED or row.get("location_resolution") == RESOLUTION_MANUAL)
    ]
    if len(orphaned_protected) == 1 and len(unmatched) == 1:
        candidate = orphaned_protected[0]
        index, prepared, key = unmatched[0]
        if _plausibly_same_event(candidate, prepared):
            _persist(candidate, prepared, index, key)
            unmatched = []

    for index, prepared, key in unmatched:
        _persist(None, prepared, index, key)

    # An event previously extracted from this post and absent from THIS run
    # is superseded — never hard-deleted, and never touched at all once
    # confirmed or manually linked (the operator outranks the model, exactly
    # as the confirmed branch above already behaves). A row with NO
    # source_event_key at all is an extraction_failed placeholder that
    # predates content identity entirely (see
    # EventExtractionService._record_failure / PromoterCrawlService.
    # _record_failure) — it was never a candidate for THIS run's events and
    # must never be flipped to superseded by their mere presence.
    for row in existing_events:
        if row["event_id"] in handled_event_ids:
            continue
        if row.get("source_event_key") is None:
            continue
        is_protected = (
            row.get("status") == STATUS_CONFIRMED
            or row.get("location_resolution") == RESOLUTION_MANUAL
        )
        if is_protected:
            # Orphaned AND not unambiguously pairable with a fresh event
            # (see above) — never claim a divergence from a specific answer
            # that does not exist, and never move status or any
            # operator-owned field. But silence would mean the operator
            # never learns "the post no longer yields your confirmed/linked
            # event" — so review_reason and last_seen_at are refreshed to
            # say exactly that, and nothing else.
            venue_dao.update_event(row["event_id"], {
                "review_reason": REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION,
                "last_seen_at": now,
                # Same reason as the confirmed branch above: identifies which
                # of this (possibly multi-source) event's sources owns the
                # refreshed last_seen_at.
                "source_handle": row.get("source_handle"),
                "source_shortcode": row.get("source_shortcode"),
            })
            continue
        venue_dao.update_event(row["event_id"], {"status": STATUS_SUPERSEDED})

    # A post collapsing back to fewer events than it used to is exactly the
    # regression this feature exists to prevent — visible only in this
    # distribution, never in a single scalar. Now observed for venue posts
    # too, not just promoter roundups.
    EVENT_EXTRACTION_EVENTS_PER_POST.observe(len(prepared_events))
    return persisted


def update_events_gauge(venue_dao) -> None:
    """Snapshot `events.event` into `EVENTS_TOTAL{status}` — plans/260810_
    date-correctness-review-reasons-and-path-parity.md §D: previously
    `EventExtractionService`'s own private method, called only at the end of
    ITS `run()`. The shared-handle crawl path never calls that (it reuses
    `PromoterCrawlService`'s per-post pipeline instead — see
    `InstagramCrawlChainer._chain_shared_handle`), so a handle that only
    ever goes through the shared-handle branch left this gauge permanently
    at zero, indistinguishable from "no events exist". Moved here, the one
    module BOTH crawl paths already depend on, and called from both, so
    every run that persists events refreshes the same gauge regardless of
    which path produced them.
    """
    counts = {status: 0 for status in ALL_STATUSES}
    for row in venue_dao.list_events():
        status = row.get("status")
        counts[status] = counts.get(status, 0) + 1
    for status, count in counts.items():
        EVENTS_TOTAL.labels(status=status).set(count)


__all__ = [
    "reconcile_post_events", "new_event_id", "is_clean_extraction",
    "STATUS_PENDING_REVIEW", "STATUS_CONFIRMED", "STATUS_SUPERSEDED", "STATUS_ACCEPTED",
    "ALL_STATUSES", "update_events_gauge",
    "REVIEW_REASON_UNRESOLVED_VENUE", "REVIEW_REASON_NEEDS_REVIEW",
    "REVIEW_REASON_DIVERGES_FROM_CONFIRMED", "REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION",
    # Shared with app.services.event_merge's confirmed-canonical branch — see
    # the coordination note beside PROTECTABLE_EVENT_FIELDS above.
    "PROTECTABLE_EVENT_FIELDS", "event_field_is_absent", "event_time_known",
    "union_lineup", "union_attractions", "apply_operator_field_protection",
]
