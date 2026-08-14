"""Operator CLI: re-resolve every stored event's date text against the
CURRENT resolver, using only data already in RDS.

See plans/260812_history-repair-dates.md.

`260812_event-attribution-and-dates.md` and `260813_review-gate-and-date-
vocabulary.md` taught `app.services.event_date_resolver.resolve_event_datetime`
new vocabulary (word-boundary "hoje"/"amanhã", three-letter months, weekday-
plus-day, a grace window on the year roll, no-weekday cadences) and taught
`app.services.event_reconciliation.is_clean_extraction` to stop requiring a
date for a post_type that never has one. Neither change touches a single row
already sitting in `events.post_item`/`events.post_item_source` — a row
extracted before either landed keeps its wrong date, its missing date, or its
stale `review_reason` forever unless something re-runs the CURRENT resolver
over its stored text. This script is that re-run: for every `post_item_source`
row, it feeds the SAME `raw_extraction.date_text`/`.time_text` and
`source_uploaded_at` the pipeline itself would use into `resolve_event_datetime`
— imported, never reimplemented — and writes the result back. No model call,
no Apify, no S3 read: everything this script needs was already persisted by
the pipeline at extraction time.

### Identity moves with the date (plan §B)
`app.services.event_identity.compute_source_event_key` hashes normalised
title plus resolved calendar date. Whenever a source's own resolution
changes, its `source_event_key` is recomputed and written in the SAME
`update_event` call as `starts_at` — never one without the other, or the
next real extraction of that post would compute a key that matches nothing
under `uq_post_item_source_post (source_handle, source_shortcode,
source_event_key)` and insert a second row for the same real event.

A recomputed key can collide with a DIFFERENT source row already using it
under the same `(source_handle, source_shortcode)` — the discovery that two
rows extracted from the same post were always the same event and only a date
bug kept them apart. This script never forces that write: the pair is left
exactly as stored, reported as a merge candidate for a human (or the dedup
sweep) to resolve, and nothing about EITHER row's key or date changes. Title-
similarity merge suggestions (`app.services.event_merge._upsert_pending_
suggestion`) carry banding/distinctive-word evidence for a FUZZY signal this
script's collisions have nothing to do with — an EXACT (title, date) match is
a stronger, different claim, and forcing it through that machinery would
misuse a mechanism built for a different question. Reporting it — the same
posture `scripts.backfill_event_venue_links` already takes for ITS OWN
identity collisions — is the honest, low-risk choice.

### Several sources of one event (plan §A)
A `post_item` can carry more than one `post_item_source` row (a prior merge
folded two posts about the same real event together). Each source resolves
its OWN date independently — never a sibling's raw text — and the post_item's
shared `starts_at`/`ends_at`/`time_known`/`is_recurring`/`recurrence_text`
are decided by folding every contributing source's resolution through
`app.services.event_merge.merge_event_fields`, the SAME field-by-field,
recency-broken precedence a real cross-post merge already uses. This is
deliberate reuse, not a new notion of "which source wins": see
`_fold_candidates`.

### Never overwrite an operator (plan §C)
- `status = 'confirmed'` is never touched silently — a confirmed row IS
  still re-resolved (so a genuine disagreement can be reported), but nothing
  about it is ever written; see the `confirmed_conflict`/`skipped_confirmed`
  dispositions.
- `operator_edited_fields` containing `starts_at` is never touched.
- `status = 'superseded'`, `'extraction_failed'`, and `'rejected'` are never
  touched — a superseded row lost a real reconciliation already and
  repairing it risks a fresh identity collision with the row that won; the
  other two have no real content identity to repair at all.

### Never invent a date
A source with no `source_uploaded_at` has no anchor for "hoje"/"amanhã" or an
unrolled year and is reported `skipped_no_anchor`, never guessed with `now()`.

Dry-run by default, `--apply` to write. Idempotent (a second `--apply` over
an already-repaired corpus changes nothing — every row's resolution is a pure
function of data this script never writes to) and resumable (`--since-id`,
the source row's own chronological id). Exits non-zero: before reading any
row if the date-vocabulary prerequisite has not landed; after loading if too
large a share of the selected sources have no anchor; after writing if the
outcome arithmetic does not balance or an UPDATE ever reports zero rows
affected.

Usage:
    python -m scripts.repair_event_dates                        # dry-run: report only
    python -m scripts.repair_event_dates --apply                # write the repaired dates
    python -m scripts.repair_event_dates --apply --since-id X   # resume after source id X

Capture the dry-run report to a file BEFORE running --apply, and read the
per-`date_text`-shape breakdown first: a shape that resolved correctly
yesterday appearing under "unchanged" or "skipped_no_anchor" is the signal
that the forward fix regressed, not that this repair has nothing to do.
There is no revert for a row once --apply has written it — the report plus a
pre-apply RDS snapshot are the recovery path.
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.models.event_kind import KIND_EVENT
from app.services.event_date_resolver import (
    REASON_DATE_RANGE,
    REASON_MISSING_DATE,
    REASON_WEEKDAY_MISMATCH,
    REASON_YEAR_INFERRED,
    ResolvedDate,
    resolve_event_datetime,
)
# Private regexes, imported rather than reimplemented — the report's
# date_text-SHAPE grouping (plan §E) must classify text using the resolver's
# OWN vocabulary, never a second, potentially-drifting guess at what counts
# as (say) a "range". See `date_text_shape` below.
from app.services.event_date_resolver import (
    _AMANHA_RE,
    _DAILY_RECURRENCE_RE,
    _DATE_RANGE_PREFIX_RE,
    _DATE_RANGE_PREPOSITION_RE,
    _HOJE_RE,
    _NO_COMPUTABLE_DAY_RECURRENCE_RE,
    _NUMERIC_DATE_RE,
    _RECURRING_MARKER_RE,
    _TEXTUAL_DATE_RE,
    _TEXTUAL_DATE_REVERSED_RE,
    _WEEKDAY_RANGE_RE,
    _WEEKDAY_RE,
    _WEEKEND_RECURRENCE_RE,
)
from app.services.event_identity import compute_source_event_key
from app.services.event_merge import merge_event_fields
from app.services.event_extraction_service import REVIEW_REASON_UNREAD_TIME
from app.services.event_reconciliation import (
    REVIEW_REASON_NEEDS_REVIEW,
    STATUS_ACCEPTED,
    STATUS_CONFIRMED,
    STATUS_PENDING_REVIEW,
    STATUS_SUPERSEDED,
    date_required_for_post_type,
    is_clean_extraction,
)

logger = logging.getLogger("repair_event_dates")

# ── dispositions — plan §E's own list, plus the two protection statuses
# (extraction_failed/rejected) scripts.backfill_event_venue_links already
# established are never touched, and `error` for a row whose OWN processing
# raised (plan Error Handling: "must not abort the run").
DISPOSITION_REPAIRED = "repaired"
DISPOSITION_UNCHANGED = "unchanged"
DISPOSITION_SKIPPED_NO_ANCHOR = "skipped_no_anchor"
DISPOSITION_SKIPPED_OPERATOR = "skipped_operator"
DISPOSITION_SKIPPED_SUPERSEDED = "skipped_superseded"
DISPOSITION_SKIPPED_STATUS = "skipped_status"
DISPOSITION_SKIPPED_CONFIRMED = "skipped_confirmed"
DISPOSITION_CONFIRMED_CONFLICT = "confirmed_conflict"
DISPOSITION_COLLIDED = "collided"
DISPOSITION_ERROR = "error"

# Protection reasons (plan §C) — internal, one step coarser than the
# dispositions above (a protected GROUP fans out into a disposition per row
# depending on whether that row even has an anchor).
_PROTECT_SUPERSEDED = "superseded"
_PROTECT_STATUS = "status"
_PROTECT_CONFIRMED = "confirmed"
_PROTECT_OPERATOR = "operator"
_UNTOUCHED_STATUSES = frozenset({"extraction_failed", "rejected"})

# The four review_reason tokens date resolution alone can add or remove —
# every OTHER token (unresolved_venue, sources_disagree, ...) is some other
# script's or path's concern and is carried through untouched. plan §D:
# "operate on the reason SET, not the string."
_DATE_REASON_TOKENS = frozenset({
    REASON_MISSING_DATE, REASON_WEEKDAY_MISMATCH, REASON_YEAR_INFERRED, REASON_DATE_RANGE,
})

# RETIRED reasons: tokens the pipeline no longer writes at all, so any stored
# copy is a leftover from a run under the old rules. Dropped unconditionally
# and never re-added — unlike `_DATE_REASON_TOKENS`, which are removed only to
# be recomputed.
#
# `unread_time` was retired by plans/260813_review-gate-and-date-vocabulary.md
# §D: an event whose DATE resolved is no longer held up by a clock time alone,
# and the signal now rides on the admin API as a flag instead. Without this,
# a row queued under the old rule stays queued forever for a reason nothing
# will ever recompute — nothing re-extracts a post whose crawl cursor has
# already moved past it, which is exactly the "the queue shows defects that
# were already fixed" trap this whole plan exists to clear.
_RETIRED_REASON_TOKENS = frozenset({REVIEW_REASON_UNREAD_TIME})

DEFAULT_MAX_NULL_ANCHOR_SHARE = 0.2
# Below this many selected candidates, a share is noise, not a signal — one
# unanchored row out of a two-row test fixture is not evidence the back-fill
# regressed. A real corpus slice (114 venue rows, 587 promoter rows per the
# plan's own Evidence section) is always far larger than this floor.
_MIN_SAMPLE_FOR_ANCHOR_GUARD = 20

# ── date_text SHAPE, for the report's grouping (plan §E) — never used to
# resolve anything, only to classify already-resolved text for a human
# scanning the report. Order mirrors resolve_event_datetime's own precedence
# loosely (relative first, cadences before single dates, ranges before plain
# dates) so a shape bucket reads the same way an operator would read the
# resolver's own decisions, but this is a reporting aid, not a parser.
SHAPE_EMPTY = "empty"
SHAPE_RELATIVE = "relative_hoje_amanha"
SHAPE_NO_COMPUTABLE_CADENCE = "cadence_toda_semana_or_sempre"
SHAPE_DAILY_OR_WEEKEND_CADENCE = "cadence_daily_or_weekend"
SHAPE_RECURRING_WEEKDAY = "cadence_todo_plus_weekday"
SHAPE_WEEKDAY_RANGE_OR_LIST = "weekday_range_or_list"
SHAPE_RANGE_PREPOSITION = "range_de_x_a_y_de_mes"
SHAPE_RANGE_LIST = "range_comma_or_e_list"
SHAPE_NUMERIC_DATE = "numeric_dmy"
SHAPE_TEXTUAL_DATE = "textual_month_name"
SHAPE_BARE_WEEKDAY = "bare_weekday"
SHAPE_OTHER = "unrecognized"


def date_text_shape(date_text: Optional[str]) -> str:
    if not date_text or not date_text.strip():
        return SHAPE_EMPTY
    text = date_text.strip().lower()
    if _HOJE_RE.search(text) or _AMANHA_RE.search(text):
        return SHAPE_RELATIVE
    if _NO_COMPUTABLE_DAY_RECURRENCE_RE.search(text):
        return SHAPE_NO_COMPUTABLE_CADENCE
    if _DAILY_RECURRENCE_RE.search(text) or _WEEKEND_RECURRENCE_RE.search(text):
        return SHAPE_DAILY_OR_WEEKEND_CADENCE
    if _RECURRING_MARKER_RE.search(text) and _WEEKDAY_RE.search(text):
        return SHAPE_RECURRING_WEEKDAY
    if _WEEKDAY_RANGE_RE.search(text):
        return SHAPE_WEEKDAY_RANGE_OR_LIST
    if _DATE_RANGE_PREPOSITION_RE.search(text):
        return SHAPE_RANGE_PREPOSITION
    if _DATE_RANGE_PREFIX_RE.search(text):
        return SHAPE_RANGE_LIST
    if _NUMERIC_DATE_RE.search(text):
        return SHAPE_NUMERIC_DATE
    if _TEXTUAL_DATE_RE.search(text) or _TEXTUAL_DATE_REVERSED_RE.search(text):
        return SHAPE_TEXTUAL_DATE
    if _WEEKDAY_RE.search(text):
        return SHAPE_BARE_WEEKDAY
    return SHAPE_OTHER


class RepairError(Exception):
    """Base for every hard-stop this script can raise. `report`, when set,
    is whatever partial `Report` had been built before the failure, so the
    CLI can still print it before exiting non-zero."""

    def __init__(self, message: str, *, report: "Optional[Report]" = None):
        super().__init__(message)
        self.report = report


class DependencyNotLanded(RepairError):
    """Raised before a single row is read — plan's Data/Config/API Impact:
    "enforce [260812_event-attribution-and-dates.md landing] by importing a
    symbol the new resolver exports and aborting on ImportError.\""""


class AnchorCoverageTooLow(RepairError):
    """Raised after loading (it needs the data) but before touching a single
    row — plan's Data/Config/API Impact: "enforce [the source_uploaded_at
    back-fill] by refusing to run when more than a configurable share of
    sources have a null anchor.\""""


class ArithmeticImbalance(RepairError):
    """`selected != sum(report.by_disposition.values())`."""


class WriteAffectedNoRows(RepairError):
    def __init__(self, event_id: str, *, report: "Optional[Report]" = None):
        super().__init__(f"UPDATE affected zero rows for event_id={event_id}", report=report)
        self.event_id = event_id


def _assert_date_vocabulary_landed() -> None:
    """Checked dynamically (an attribute lookup at CALL time, not a bare
    module-level `from ... import`) so a test can simulate "the fix has not
    landed" by deleting the attribute from the already-imported module,
    without needing a second process or an old checkout — the SAME technique
    `scripts.backfill_event_venue_links._assert_forward_fix_landed` uses for
    its own prerequisite. `select_date_interpretation_for_reuse` is the
    landed name: without it, `resolve_event_datetime` has neither the
    model's structured `date_interpretation` fallback nor the year-roll
    grace window, and this script's re-resolution would produce a THIRD
    answer, different from both what is stored and what the fixed pipeline
    produces."""
    import app.services.event_date_resolver as _edr

    if not hasattr(_edr, "select_date_interpretation_for_reuse"):
        raise DependencyNotLanded(
            "app.services.event_date_resolver.select_date_interpretation_for_reuse "
            "is missing — the date-interpretation fallback and year-roll grace "
            "window (plans/260812_event-attribution-and-dates.md §C/§D) have not "
            "landed in this build. Refusing to run before reading any item."
        )


def _assert_anchor_coverage(candidates: list, max_null_anchor_share: float) -> None:
    if len(candidates) < _MIN_SAMPLE_FOR_ANCHOR_GUARD:
        return
    null_count = sum(1 for row in candidates if row.get("source_uploaded_at") is None)
    share = null_count / len(candidates)
    if share > max_null_anchor_share:
        raise AnchorCoverageTooLow(
            f"{null_count}/{len(candidates)} selected sources ({share:.1%}) have no "
            f"source_uploaded_at — exceeds the configured {max_null_anchor_share:.0%} "
            "ceiling. The source_uploaded_at back-fill "
            "(plans/260812_crawl-error-visibility.md §C) looks incomplete for this "
            "slice; refusing to run before touching any row."
        )


def _protection_reason(post: dict) -> Optional[str]:
    """plan §C's per-status/per-protection table, in the order that matters:
    a dead (superseded) or contentless (extraction_failed/rejected) row is
    never worth even a disagreement report; a confirmed row's disagreement
    IS worth reporting (the strongest signal in the system deserves the
    fullest report, not a bare skip); an operator's own edited date is
    simply left alone."""
    status = post.get("status")
    if status == STATUS_SUPERSEDED:
        return _PROTECT_SUPERSEDED
    if status in _UNTOUCHED_STATUSES:
        return _PROTECT_STATUS
    if status == STATUS_CONFIRMED:
        return _PROTECT_CONFIRMED
    if "starts_at" in (post.get("operator_edited_fields") or []):
        return _PROTECT_OPERATOR
    return None


def _resolve_source(source_row: dict) -> Optional[ResolvedDate]:
    """`None` when this source has no anchor — never guessed with `now()`.
    Otherwise the SAME call the pipeline itself makes: the source's own
    frozen `raw_extraction.date_text`/`.time_text`, its own
    `source_uploaded_at`, and its own already-stored `date_interpretation`
    (never a fresh model call — there is nothing else to consult)."""
    anchor = source_row.get("source_uploaded_at")
    if anchor is None:
        return None
    raw = source_row.get("raw_extraction")
    raw = raw if isinstance(raw, dict) else {}
    return resolve_event_datetime(
        date_text=raw.get("date_text"), time_text=raw.get("time_text"),
        post_timestamp=anchor,
        is_recurring=raw.get("is_recurring"), recurrence_text=raw.get("recurrence_text"),
        date_interpretation=source_row.get("date_interpretation"),
    )


def _date_reasons_for(resolved: ResolvedDate, *, post_type: str) -> list:
    """The date-resolution component of a fresh `review_reason`, gated by
    `date_required_for_post_type` exactly like `event_extraction_service.
    _extract_one`'s own `missing_date_suppressed` — a promoter-sourced
    promotion/menu/food/other row got NO such gate at extraction time
    (`promoter_crawl_service.py` predates plans/260813_review-gate-and-date-
    vocabulary.md §C and was never updated to add it), so this script is the
    only place many stored rows will ever have "missing_date" actually
    removed for an exempt post_type."""
    reasons: list = []
    missing_date_suppressed = (
        resolved.review_reason == REASON_MISSING_DATE
        and not date_required_for_post_type(post_type)
    )
    if resolved.review_reason and not missing_date_suppressed:
        reasons.append(resolved.review_reason)
    if resolved.year_inferred:
        reasons.append(REASON_YEAR_INFERRED)
    if resolved.date_range:
        reasons.append(REASON_DATE_RANGE)
    return reasons


def _fold_date_reasons(existing_reason: Optional[str], new_date_reasons: list) -> Optional[str]:
    """plan §D: operate on the reason SET. Drops every date-resolution token
    (wherever it sits) and re-appends only the ones THIS resolution actually
    produced — a non-date reason (`unresolved_venue`, `sources_disagree`,
    ...) is never touched, never reordered relative to itself, and a row
    still undecided for a different reason is never un-queued by this
    alone.

    A RETIRED token (`_RETIRED_REASON_TOKENS`) is dropped too, and never
    re-appended: the pipeline stopped writing it, so a stored copy is stale
    by definition and no other path will ever clear it."""
    tokens = [
        token for token in (existing_reason or "").split("; ")
        if token
        and token not in _DATE_REASON_TOKENS
        and token not in _RETIRED_REASON_TOKENS
    ]
    for reason in new_date_reasons:
        if reason not in tokens:
            tokens.append(reason)
    return "; ".join(tokens) if tokens else None


def _candidate_fields(resolved: ResolvedDate) -> dict:
    """One contributing source's resolution, shaped as the partial "event"
    dict `app.services.event_merge.merge_event_fields` expects. `raw_
    extraction={"time_known": ...}` is synthetic and in-memory ONLY — it
    exists purely so `event_reconciliation.event_field_is_absent`'s
    starts_at special case (a date-only, time-unknown value counts as
    "empty" for folding purposes) works correctly during the fold; nothing
    in this dict, including this synthetic key, is ever written to the
    database — see `_fold_candidates` and this module's own "never
    re-extract" boundary."""
    return {
        "starts_at": resolved.starts_at, "ends_at": resolved.ends_at,
        "is_recurring": resolved.is_recurring, "recurrence_text": resolved.recurrence_text,
        "raw_extraction": {"time_known": resolved.time_known},
    }


def _fold_candidates(contributors: list) -> tuple:
    """Fold every contributing `(source_row, ResolvedDate)` pair into the
    post_item's shared starts_at/ends_at/time_known/is_recurring/
    recurrence_text, via `merge_event_fields` — the SAME field-by-field
    precedence a real cross-post merge already uses (plan §A: "reuse
    merge_event_fields ... so this script cannot disagree with the pipeline
    about which source wins"). `merge_event_fields`'s confirmed-row branch
    is never reached here: these synthetic candidate dicts carry no `status`
    key at all, so `canonical.get("status") == STATUS_CONFIRMED` is always
    False — protection is decided by THIS module, one layer up, never by the
    fold itself.

    A disagreement is broken by recency (`last_seen_at`) ONLY when at least
    one side states a real CLOCK TIME — `event_reconciliation.
    event_field_is_absent`'s existing starts_at rule counts a date-only,
    time-UNKNOWN value as "empty" for merge purposes (the same rule a real
    cross-post merge already lives with), so when NEITHER contributor states
    a time, their disagreement is invisible to the empty-check and whichever
    was seeded first — the oldest first-seen contributor — simply stands.
    This is the common case for a flyer that names no clock time; it is
    inherited deliberately, not special-cased around (see this module's test
    suite, `test_two_time_unknown_disagreeing_dates_keep_the_first_seen`,
    for the case made explicit). A value that IS None (no anchor, or the
    text was unresolvable) is unambiguously empty either way and never wins
    over a real answer regardless of order.

    `contributors` must be non-empty, ordered oldest-first-seen-first
    (matching `list_event_sources`'s own convention) — both because that is
    what makes "first seen stands" the correct description of the paragraph
    above, and so a `last_seen_at` tie (when a time IS known on both sides)
    resolves the same way `merge_event_fields`'s own `>` comparison would.

    Returns `(folded_fields, winning_source_id, winning_resolved)` — the
    "winner" is whichever contributor's OWN `starts_at` is reflected in the
    fold's final answer, tracked by value (merge_event_fields returns only
    the winning VALUES, never which side proposed them); ties (two
    contributors independently resolving to the identical instant) keep the
    first one seen, which is never an observable difference since their
    values agree. `winning_resolved` supplies the review_reason/
    year_inferred/date_range flags the caller folds into `review_reason` —
    tied to `starts_at` specifically, since that is the field actually being
    repaired; a DIFFERENT field (say `is_recurring`) winning from a
    different contributor in the same fold does not change whose date the
    row now carries.
    """
    winner_row, winner_resolved = contributors[0]
    canonical = _candidate_fields(winner_resolved)
    canonical["last_seen_at"] = winner_row.get("last_seen_at")

    for source_row, resolved in contributors[1:]:
        duplicate = _candidate_fields(resolved)
        duplicate["last_seen_at"] = source_row.get("last_seen_at")
        changed, _review_reason = merge_event_fields(canonical, duplicate)
        if not changed:
            continue
        canonical = {**canonical, **changed}
        if changed.get("starts_at") == duplicate.get("starts_at"):
            winner_row, winner_resolved = source_row, resolved
            canonical["last_seen_at"] = duplicate["last_seen_at"]

    folded_fields = {
        "starts_at": canonical.get("starts_at"), "ends_at": canonical.get("ends_at"),
        "time_known": bool((canonical.get("raw_extraction") or {}).get("time_known")),
        "is_recurring": bool(canonical.get("is_recurring")),
        "recurrence_text": canonical.get("recurrence_text"),
    }
    return folded_fields, winner_row["id"], winner_resolved


def _seed_key_index(all_rows: list) -> dict:
    """`(source_handle, source_shortcode) -> {source_event_key: source_id}`,
    seeded from the WHOLE corpus's CURRENT stored keys — including protected
    rows, whose key never moves but still occupies its slot for collision
    purposes. A NULL stored key (an extraction_failed placeholder; see
    `scripts.backfill_event_venue_links.insert_event`'s identical rationale)
    never collides with anything and is never registered."""
    index: dict = defaultdict(dict)
    for row in all_rows:
        key = row.get("source_event_key")
        if key is None:
            continue
        index[(row.get("source_handle"), row.get("source_shortcode"))][key] = row["id"]
    return index


def _check_and_register_key(index: dict, source_row: dict, new_key: str) -> Optional[str]:
    """`None` when `new_key` is free (and the index is updated to reflect
    it) — the colliding sibling's own source id otherwise, in which case the
    index is left completely untouched: this script must never register a
    key it is refusing to write (plan §B: "do not force the write")."""
    bucket = index[(source_row.get("source_handle"), source_row.get("source_shortcode"))]
    owner = bucket.get(new_key)
    if owner is not None and owner != source_row["id"]:
        return owner
    old_key = source_row.get("source_event_key")
    if old_key is not None and old_key != new_key and bucket.get(old_key) == source_row["id"]:
        del bucket[old_key]
    bucket[new_key] = source_row["id"]
    return None


@dataclass
class Decision:
    """The per-source-row verdict — one row in the report, one entry in
    `Report.rows`. `write_fields`, when non-empty, is EXACTLY the partial
    update `run_repair` sent to `venue_dao.update_event` for this row's
    action (both event-level and source-level keys, whichever this specific
    call carried)."""

    source_id: str
    event_id: str
    action: str
    source_handle: Optional[str] = None
    source_shortcode: Optional[str] = None
    venue_name: Optional[str] = None
    title: Optional[str] = None
    date_text: Optional[str] = None
    date_text_shape: str = SHAPE_EMPTY
    old_starts_at: Optional[object] = None
    new_starts_at: Optional[object] = None
    old_review_reason: Optional[str] = None
    new_review_reason: Optional[str] = None
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    old_source_event_key: Optional[str] = None
    new_source_event_key: Optional[str] = None
    collides_with: Optional[str] = None
    write_fields: dict = field(default_factory=dict)


def _decision(
    row: dict, action: str, resolved: Optional[ResolvedDate], **overrides,
) -> Decision:
    raw = row.get("raw_extraction")
    raw = raw if isinstance(raw, dict) else {}
    date_text = raw.get("date_text")
    decision = Decision(
        source_id=row["id"], event_id=row["event_id"], action=action,
        source_handle=row.get("source_handle"), source_shortcode=row.get("source_shortcode"),
        venue_name=row.get("venue_name"), title=row.get("title"),
        date_text=date_text, date_text_shape=date_text_shape(date_text),
        old_starts_at=row.get("starts_at"),
        new_starts_at=resolved.starts_at if resolved is not None else row.get("starts_at"),
        old_review_reason=row.get("review_reason"), new_review_reason=row.get("review_reason"),
        old_status=row.get("status"), new_status=row.get("status"),
        old_source_event_key=row.get("source_event_key"),
        new_source_event_key=row.get("source_event_key"),
    )
    for key, value in overrides.items():
        setattr(decision, key, value)
    return decision


@dataclass
class Report:
    selected: int = 0
    by_disposition: Counter = field(default_factory=Counter)
    by_shape: dict = field(default_factory=lambda: defaultdict(Counter))
    rows: list = field(default_factory=list)
    collisions: list = field(default_factory=list)  # [(source_id, colliding_owner_id, key), ...]
    applied: bool = False
    balanced: bool = False

    @property
    def changed_count(self) -> int:
        return self.by_disposition.get(DISPOSITION_REPAIRED, 0)


def _emit(report: Report, decision: Decision) -> None:
    report.by_disposition[decision.action] += 1
    report.by_shape[decision.date_text_shape][decision.action] += 1
    report.rows.append(decision)


def check_balance(report: Report) -> None:
    total = sum(report.by_disposition.values())
    if total != report.selected:
        report.balanced = False
        raise ArithmeticImbalance(
            f"totals did not balance: selected={report.selected} but dispositions "
            f"sum to {total}: {dict(report.by_disposition)}",
            report=report,
        )
    report.balanced = True


def _process_group(
    venue_dao, report: Report, key_index: dict, event_id: str, sources: list, *, apply: bool,
) -> None:
    """Every `post_item_source` row sharing one `event_id`, decided and
    (when `apply`) written together — see the module docstring's "Several
    sources of one event" section for why they cannot be decided
    independently."""
    post = sources[0]
    protection = _protection_reason(post)
    resolved_by_id = {row["id"]: _resolve_source(row) for row in sources}

    if protection in (_PROTECT_SUPERSEDED, _PROTECT_STATUS):
        action = DISPOSITION_SKIPPED_SUPERSEDED if protection == _PROTECT_SUPERSEDED else DISPOSITION_SKIPPED_STATUS
        for row in sources:
            resolved = resolved_by_id[row["id"]]
            _emit(report, _decision(
                row, action if resolved is not None else DISPOSITION_SKIPPED_NO_ANCHOR, resolved,
            ))
        return

    # Collision detection — only for a row this script could actually WRITE
    # a new key for. A protected (confirmed/operator) row's key never moves
    # regardless of what the resolver says, so it is never checked here.
    collided_owner_by_id: dict = {}
    if protection is None:
        for row in sources:
            resolved = resolved_by_id[row["id"]]
            if resolved is None:
                continue
            new_key = compute_source_event_key(post.get("title"), resolved.starts_at)
            owner = _check_and_register_key(key_index, row, new_key)
            if owner is not None:
                collided_owner_by_id[row["id"]] = owner
                report.collisions.append((row["id"], owner, new_key))

    contributors = [
        (row, resolved_by_id[row["id"]]) for row in sources
        if resolved_by_id[row["id"]] is not None and row["id"] not in collided_owner_by_id
    ]

    if protection == _PROTECT_OPERATOR:
        for row in sources:
            resolved = resolved_by_id[row["id"]]
            _emit(report, _decision(
                row, DISPOSITION_SKIPPED_OPERATOR if resolved is not None else DISPOSITION_SKIPPED_NO_ANCHOR,
                resolved,
            ))
        return

    if protection == _PROTECT_CONFIRMED:
        if not contributors:
            for row in sources:
                _emit(report, _decision(row, DISPOSITION_SKIPPED_NO_ANCHOR, None))
            return
        folded_fields, _winner_id, _winner_resolved = _fold_candidates(contributors)
        disagrees = folded_fields["starts_at"] != post.get("starts_at")
        action = DISPOSITION_CONFIRMED_CONFLICT if disagrees else DISPOSITION_SKIPPED_CONFIRMED
        for row in sources:
            resolved = resolved_by_id[row["id"]]
            _emit(report, _decision(
                row, action if resolved is not None else DISPOSITION_SKIPPED_NO_ANCHOR, resolved,
            ))
        return

    # ── unprotected: the normal repair path ──────────────────────────────
    for row in sources:
        owner = collided_owner_by_id.get(row["id"])
        if owner is None:
            continue
        resolved = resolved_by_id[row["id"]]
        new_key = compute_source_event_key(post.get("title"), resolved.starts_at)
        _emit(report, _decision(
            row, DISPOSITION_COLLIDED, resolved,
            new_source_event_key=new_key, collides_with=owner,
        ))

    if not contributors:
        for row in sources:
            if row["id"] in collided_owner_by_id:
                continue
            _emit(report, _decision(row, DISPOSITION_SKIPPED_NO_ANCHOR, None))
        return

    folded_fields, winner_id, winner_resolved = _fold_candidates(contributors)
    new_date_reasons = _date_reasons_for(winner_resolved, post_type=post.get("post_type") or KIND_EVENT)
    new_review_reason = _fold_date_reasons(post.get("review_reason"), new_date_reasons)
    is_clean = is_clean_extraction(
        review_reason=new_review_reason, starts_at=folded_fields["starts_at"],
        venue_id=post.get("venue_id"), confidence=post.get("confidence"),
        min_confidence=settings.event_extraction_min_confidence,
        post_type=post.get("post_type") or KIND_EVENT,
    )
    new_status = STATUS_ACCEPTED if is_clean else STATUS_PENDING_REVIEW
    if new_status == STATUS_PENDING_REVIEW and not new_review_reason:
        # Mirrors event_reconciliation.reconcile_post_events' own residual
        # fallback — a queued row must never carry a null reason.
        new_review_reason = REVIEW_REASON_NEEDS_REVIEW

    post_item_changed = (
        folded_fields["starts_at"] != post.get("starts_at")
        or folded_fields["ends_at"] != post.get("ends_at")
        or folded_fields["time_known"] != bool(post.get("time_known"))
        or folded_fields["is_recurring"] != bool(post.get("is_recurring"))
        or (folded_fields["recurrence_text"] or None) != (post.get("recurrence_text") or None)
        or new_review_reason != post.get("review_reason")
        or new_status != post.get("status")
    )

    for row in sources:
        if row["id"] in collided_owner_by_id:
            continue
        resolved = resolved_by_id[row["id"]]
        if resolved is None:
            _emit(report, _decision(row, DISPOSITION_SKIPPED_NO_ANCHOR, None))
            continue

        new_key = compute_source_event_key(post.get("title"), resolved.starts_at)
        key_changed = new_key != row.get("source_event_key")
        is_winner = row["id"] == winner_id
        triggers_write = key_changed or (is_winner and post_item_changed)

        if not triggers_write:
            _emit(report, _decision(row, DISPOSITION_UNCHANGED, resolved, new_source_event_key=new_key))
            continue

        write_fields: dict = {
            "source_handle": row.get("source_handle"), "source_shortcode": row.get("source_shortcode"),
        }
        if key_changed:
            write_fields["source_event_key"] = new_key
        decision_overrides: dict = {"new_source_event_key": new_key}
        if is_winner and post_item_changed:
            write_fields.update({
                "starts_at": folded_fields["starts_at"], "ends_at": folded_fields["ends_at"],
                "time_known": folded_fields["time_known"],
                "is_recurring": folded_fields["is_recurring"],
                "recurrence_text": folded_fields["recurrence_text"],
                "review_reason": new_review_reason, "status": new_status,
            })
            decision_overrides["new_review_reason"] = new_review_reason
            decision_overrides["new_status"] = new_status

        if apply:
            result = venue_dao.update_event(row["event_id"], write_fields)
            if result is None:
                raise WriteAffectedNoRows(row["event_id"], report=report)

        decision = _decision(row, DISPOSITION_REPAIRED, resolved, **decision_overrides)
        decision.write_fields = write_fields
        _emit(report, decision)


def run_repair(
    venue_dao, *, apply: bool, since_id: Optional[str] = None,
    max_null_anchor_share: float = DEFAULT_MAX_NULL_ANCHOR_SHARE,
) -> Report:
    """The whole repair in one pass: dependency guard, selection, anchor-
    coverage guard, per-post_item decisions, writes (when `apply`), then the
    balance check.

    `since_id` resumes: only source rows with `id > since_id` (plain string
    comparison — real source ids are `evsrc_<ULID>`, chronologically
    ordered) are SELECTED this run; every other row still seeds the
    collision index (see `_seed_key_index`) with its current key, so a
    resumed run can never miss a collision against a row outside its own
    window.
    """
    _assert_date_vocabulary_landed()

    all_rows = venue_dao.list_all_event_sources_with_context()
    candidates = [row for row in all_rows if since_id is None or row["id"] > since_id]

    _assert_anchor_coverage(candidates, max_null_anchor_share)

    report = Report(applied=apply)
    report.selected = len(candidates)

    key_index = _seed_key_index(all_rows)

    groups: dict = {}
    order: list = []
    for row in candidates:
        event_id = row["event_id"]
        if event_id not in groups:
            groups[event_id] = []
            order.append(event_id)
        groups[event_id].append(row)

    for event_id in order:
        sources = groups[event_id]
        try:
            _process_group(venue_dao, report, key_index, event_id, sources, apply=apply)
        except WriteAffectedNoRows:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad group must not abort the run
            logger.exception(
                "repair_event_dates: unexpected error processing event_id=%s: %s", event_id, exc,
            )
            for row in sources:
                _emit(report, _decision(row, DISPOSITION_ERROR, None))

    check_balance(report)
    return report


def _print_report(report: Report) -> None:
    mode = "APPLY" if report.applied else "DRY RUN"
    logger.info("=== repair_event_dates (%s) ===", mode)
    logger.info("selected: %d", report.selected)
    logger.info("by disposition: %s", dict(report.by_disposition))
    logger.info("by date_text shape:")
    for shape, counts in sorted(report.by_shape.items()):
        logger.info("  %s: %s", shape, dict(counts))
    for decision in report.rows:
        logger.info(
            "  %s source_id=%s event_id=%s venue=%r title=%r date_text=%r shape=%s | "
            "starts_at: %r -> %r | key: %s -> %s | status: %r -> %r | reason: %r -> %r",
            decision.action, decision.source_id, decision.event_id,
            decision.venue_name, decision.title, decision.date_text, decision.date_text_shape,
            decision.old_starts_at, decision.new_starts_at,
            decision.old_source_event_key, decision.new_source_event_key,
            decision.old_status, decision.new_status,
            decision.old_review_reason, decision.new_review_reason,
        )
    if report.collisions:
        logger.info("MERGE CANDIDATES (key collisions — reported, never forced):")
        for source_id, owner_id, key in report.collisions:
            logger.info("  %s collides with %s on key=%s", source_id, owner_id, key)
    logger.info("balanced: %s", report.balanced)


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Re-resolve every stored events.post_item_source.raw_extraction date "
        "against the current resolver, using only data already in RDS. Dry-run by default.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="write the repaired dates (default: dry-run report only)",
    )
    ap.add_argument(
        "--since-id", default=None,
        help="resume: only process candidates with source id greater than this id",
    )
    ap.add_argument(
        "--max-null-anchor-share", type=float, default=DEFAULT_MAX_NULL_ANCHOR_SHARE,
        help=(
            "refuse to run if more than this share of selected sources have no "
            f"source_uploaded_at (default: {DEFAULT_MAX_NULL_ANCHOR_SHARE})"
        ),
    )
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))

    try:
        report = run_repair(
            venue_dao, apply=args.apply, since_id=args.since_id,
            max_null_anchor_share=args.max_null_anchor_share,
        )
    except DependencyNotLanded as exc:
        logger.error(str(exc))
        return 2
    except AnchorCoverageTooLow as exc:
        logger.error(str(exc))
        return 3
    except WriteAffectedNoRows as exc:
        if exc.report is not None:
            _print_report(exc.report)
        logger.error(str(exc))
        return 4
    except ArithmeticImbalance as exc:
        if exc.report is not None:
            _print_report(exc.report)
        logger.error(str(exc))
        return 5

    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
