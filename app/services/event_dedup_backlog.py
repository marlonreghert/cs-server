"""The duplicate/refusal/attribution backlog an operator cannot see today.

See plans/260912_events-venue-night-duplication.md §A. A refused merge pair
records NOTHING — `app.services.event_merge._observe_title_refusal` only
increments a cumulative counter and returns — so production accumulated
`refused_disjoint=2793` against `merged=21` with no row, no queue entry and
no report behind it, next to a venue-night duplicate population nobody could
count (55 groups / 93 excess rows, measured once by hand during the RCA).

This module turns both into standing numbers.

`compute_dedup_backlog` is PURE — it takes ALREADY-FETCHED rows and answers
over them, exactly the posture `app.services.event_dedup`'s own module
docstring takes ("every function here is PURE ... but never touches a DAO").
`collect_dedup_backlog` below it is the ONE thin adapter that reads the DAO
and delegates; it holds no rules of its own, so the thing under test is
always the pure function.

Reading the backlog never changes a row: nothing here writes, and nothing
here calls `app.services.event_merge`'s absorbing paths. The refusal scan
re-uses `event_dedup.evaluate_pair`/`in_candidate_window_for_rows` — the
SAME predicate the runtime merge and `scripts/measure_event_dedup.py` use —
so the backlog can never disagree with what the merge layer would actually
do about a pair. (§D widened the window function; this module follows it, so
the reported refusal population is the one the merge layer would really
evaluate, not a narrower approximation of it.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Optional

from app.models.event_kind import KIND_EVENT
from app.services import event_dedup
from app.services.event_date_resolver import RECIFE_TZ

logger = logging.getLogger(__name__)

# ── the six `EVENT_DEDUP_BACKLOG{measure}` label values (plan §A: "a small
# fixed set ... bounded cardinality, per app/metrics.py's own stated rule").
MEASURE_VENUE_NIGHT_GROUPS = "venue_night_groups"
MEASURE_VENUE_NIGHT_EXCESS_ROWS = "venue_night_excess_rows"
MEASURE_REFUSED_DISJOINT_PAIRS = "refused_disjoint_pairs"
MEASURE_REFUSED_NO_DISTINCTIVE_TOKENS_PAIRS = "refused_no_distinctive_tokens_pairs"
MEASURE_PENDING_SUGGESTIONS = "pending_suggestions"
MEASURE_ATTRIBUTION_DISPUTED_ROWS = "attribution_disputed_rows"

BACKLOG_MEASURES = (
    MEASURE_VENUE_NIGHT_GROUPS,
    MEASURE_VENUE_NIGHT_EXCESS_ROWS,
    MEASURE_REFUSED_DISJOINT_PAIRS,
    MEASURE_REFUSED_NO_DISTINCTIVE_TOKENS_PAIRS,
    MEASURE_PENDING_SUGGESTIONS,
    MEASURE_ATTRIBUTION_DISPUTED_ROWS,
)

STATUS_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class VenueNightGroup:
    """One `(venue_id, Recife local date)` holding more than one live row —
    the RCA's own headline measure, per group, with the titles and
    `location_text` values an operator needs to tell a genuine duplicate
    apart from a programme."""

    venue_id: str
    venue_name: Optional[str]
    local_date: str  # Recife YYYY-MM-DD
    event_ids: tuple
    titles: tuple
    location_texts: tuple

    @property
    def row_count(self) -> int:
        return len(self.event_ids)

    @property
    def excess_rows(self) -> int:
        return max(0, len(self.event_ids) - 1)

    def to_dict(self) -> dict:
        return {
            "venue_id": self.venue_id, "venue_name": self.venue_name,
            "local_date": self.local_date, "row_count": self.row_count,
            "excess_rows": self.excess_rows, "event_ids": list(self.event_ids),
            "titles": list(self.titles), "location_texts": list(self.location_texts),
        }


@dataclass(frozen=True)
class AttributionDispute:
    """One `(source_handle, location_text)` an account's live rows carry that
    is NOT evidence for the venue those rows are filed at — the venue-
    acquisition backlog (plan §A: "the accounts whose branches we do not
    carry")."""

    source_handle: Optional[str]
    location_text: str
    row_count: int
    event_ids: tuple
    venue_ids: tuple
    resolves_to_venue_id: Optional[str] = None
    resolves_to_venue_name: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source_handle": self.source_handle, "location_text": self.location_text,
            "row_count": self.row_count, "event_ids": list(self.event_ids),
            "venue_ids": list(self.venue_ids),
            "resolves_to_venue_id": self.resolves_to_venue_id,
            "resolves_to_venue_name": self.resolves_to_venue_name,
        }


@dataclass
class DedupBacklog:
    venue_night_groups: list = field(default_factory=list)  # [VenueNightGroup]
    refused_disjoint_pairs: int = 0
    refused_no_distinctive_tokens_pairs: int = 0
    pending_suggestions: int = 0
    attribution_disputes: list = field(default_factory=list)  # [AttributionDispute]
    live_rows: int = 0

    @property
    def venue_night_group_count(self) -> int:
        return len(self.venue_night_groups)

    @property
    def venue_night_excess_rows(self) -> int:
        return sum(g.excess_rows for g in self.venue_night_groups)

    @property
    def attribution_disputed_rows(self) -> int:
        return sum(d.row_count for d in self.attribution_disputes)

    def measures(self) -> dict:
        """The six gauge values, by `measure` label — every one of them
        always present, so `event_dedup_backlog{measure="..."}` reports an
        honest 0 from the very first run rather than no data point at all
        (the same reason `ALL_STATUSES` exists for `EVENTS_TOTAL`)."""
        return {
            MEASURE_VENUE_NIGHT_GROUPS: self.venue_night_group_count,
            MEASURE_VENUE_NIGHT_EXCESS_ROWS: self.venue_night_excess_rows,
            MEASURE_REFUSED_DISJOINT_PAIRS: self.refused_disjoint_pairs,
            MEASURE_REFUSED_NO_DISTINCTIVE_TOKENS_PAIRS: self.refused_no_distinctive_tokens_pairs,
            MEASURE_PENDING_SUGGESTIONS: self.pending_suggestions,
            MEASURE_ATTRIBUTION_DISPUTED_ROWS: self.attribution_disputed_rows,
        }


def is_live_event_row(row: dict) -> bool:
    """The backlog's corpus: a `post_type == "event"` row that is still
    standing and still attributed. Excludes, deliberately and in this order:
    a non-event post type (a menu item/promotion/greeting is not a duplicate
    listing), a row already absorbed (`status == 'superseded'` OR a non-null
    `superseded_by` — both are checked, because the two are written by
    different paths and a row that carries either is not serving), and a row
    with no venue (there is no venue-night to belong to)."""
    if row.get("post_type") != KIND_EVENT:
        return False
    if row.get("status") == STATUS_SUPERSEDED:
        return False
    if row.get("superseded_by") is not None:
        return False
    if not row.get("venue_id"):
        return False
    return True


def _recife_date_str(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(RECIFE_TZ).date().isoformat()


def row_location_text(row: dict) -> Optional[str]:
    """The event's own stated location, read the SAME way
    `scripts/backfill_event_venue_links._location_text_input` reads it: the
    row's own `location_text` column when present (where an operator PATCH
    writes), else the frozen model answer on the primary source's
    `raw_extraction`. Never a fresh model call, never a sibling's text."""
    value = row.get("location_text")
    if value:
        return value
    raw = row.get("raw_extraction")
    if isinstance(raw, dict):
        return raw.get("location_text") or None
    return None


def compute_dedup_backlog(
    rows: list,
    *,
    venue_names: dict,
    config: "event_dedup.DedupConfig",
    pending_suggestions: int = 0,
    venue_addresses: Optional[dict] = None,
    location_text_resolver=None,
) -> DedupBacklog:
    """The whole backlog over `rows` (every `events.post_item` row the
    caller fetched — this function filters to the live event corpus itself
    via `is_live_event_row`, so a caller never has to remember the rule).

    `venue_names`/`venue_addresses` map `venue_id` -> the venue's own name
    and address. `location_text_resolver(location_text, venue_id)` is an
    optional callable returning `(venue_id, venue_name)` for a location text
    that names a DIFFERENT catalog venue, or `None` — supplied by the caller
    (Phase C's dispute rule) so this module never grows a second notion of
    "which venue does this text name".
    """
    venue_addresses = venue_addresses or {}
    live = [row for row in rows if is_live_event_row(row)]

    backlog = DedupBacklog(pending_suggestions=pending_suggestions, live_rows=len(live))

    # ── venue-night groups ───────────────────────────────────────────────
    by_night: dict = {}
    for row in live:
        local_date = _recife_date_str(row.get("starts_at"))
        if local_date is None:
            # No date is no night — such a row cannot be a same-night
            # duplicate of anything (the same reason `in_candidate_window`
            # refuses a pair where either side has no `starts_at`).
            continue
        by_night.setdefault((row["venue_id"], local_date), []).append(row)

    for (venue_id, local_date), members in sorted(by_night.items()):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda r: r["event_id"])
        backlog.venue_night_groups.append(VenueNightGroup(
            venue_id=venue_id, venue_name=venue_names.get(venue_id), local_date=local_date,
            event_ids=tuple(r["event_id"] for r in members),
            titles=tuple(r.get("title") for r in members),
            location_texts=tuple(
                t for t in (row_location_text(r) for r in members) if t
            ),
        ))

    # ── refused same-night pairs, split by reason ────────────────────────
    by_venue: dict = {}
    for row in live:
        by_venue.setdefault(row["venue_id"], []).append(row)

    for venue_id, members in by_venue.items():
        if len(members) < 2:
            continue
        venue_name = venue_names.get(venue_id)
        venue_tokens = event_dedup.venue_name_tokens(venue_name)
        for a, b in combinations(members, 2):
            if not event_dedup.in_candidate_window(
                a.get("starts_at"), b.get("starts_at"),
                window_hours=config.candidate_window_hours,
            ):
                continue
            if event_dedup.evaluate_pair(a, b, venue_name=venue_name, config=config) is not None:
                continue
            set_a = event_dedup.distinctive_set(
                a.get("title"), venue_tokens=venue_tokens,
                generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
            )
            set_b = event_dedup.distinctive_set(
                b.get("title"), venue_tokens=venue_tokens,
                generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
            )
            if not set_a or not set_b:
                backlog.refused_no_distinctive_tokens_pairs += 1
            else:
                backlog.refused_disjoint_pairs += 1

    # ── attribution disputes, keyed by account ───────────────────────────
    from app.services.event_venue_resolution import _fold_for_containment

    disputes: dict = {}
    for row in live:
        location_text = row_location_text(row)
        if not location_text:
            continue
        venue_id = row["venue_id"]
        folded = _fold_for_containment(location_text)
        if not folded:
            continue
        # A location text that IS the venue it is filed at — its own name or
        # a fragment of its own address — is evidence FOR the attribution,
        # never against it, and must not be reported as a dispute (the
        # majority of correct rows would otherwise "mismatch" — plan §C's own
        # warning about naive string comparison).
        own_name = _fold_for_containment(venue_names.get(venue_id))
        own_address = _fold_for_containment(venue_addresses.get(venue_id))
        if (own_name and folded in own_name) or (own_address and folded in own_address):
            continue
        key = (row.get("source_handle"), location_text)
        disputes.setdefault(key, []).append(row)

    for (source_handle, location_text), members in sorted(
        disputes.items(), key=lambda kv: (-len(kv[1]), str(kv[0][0]), kv[0][1]),
    ):
        resolved_id = resolved_name = None
        if location_text_resolver is not None:
            try:
                resolved = location_text_resolver(location_text, members[0]["venue_id"])
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[EventDedupBacklog] location_text resolver failed: {e}")
                resolved = None
            if resolved:
                resolved_id, resolved_name = resolved
        backlog.attribution_disputes.append(AttributionDispute(
            source_handle=source_handle, location_text=location_text,
            row_count=len(members),
            event_ids=tuple(r["event_id"] for r in members),
            venue_ids=tuple(sorted({r["venue_id"] for r in members})),
            resolves_to_venue_id=resolved_id, resolves_to_venue_name=resolved_name,
        ))

    return backlog


# ── the one adapter (see this module's docstring) ───────────────────────────
def collect_dedup_backlog(venue_dao, *, redis_like=None) -> DedupBacklog:
    """Fetch and delegate. Holds no rules: the corpus filter, the grouping
    and the refusal split all live in `compute_dedup_backlog` above, which is
    what every test drives."""
    rows = venue_dao.list_events()
    config = event_dedup.load_dedup_config(redis_like)
    venue_ids = {row["venue_id"] for row in rows if row.get("venue_id")}
    venue_names: dict = {}
    venue_addresses: dict = {}
    for venue_id in venue_ids:
        venue = venue_dao.get_venue(venue_id)
        if venue is None:
            continue
        if isinstance(venue, dict):
            venue_names[venue_id] = venue.get("venue_name")
            venue_addresses[venue_id] = venue.get("venue_address")
        else:
            venue_names[venue_id] = getattr(venue, "venue_name", None)
            venue_addresses[venue_id] = getattr(venue, "venue_address", None)
    pending = len(venue_dao.list_event_merge_suggestions(decision="pending"))
    return compute_dedup_backlog(
        rows, venue_names=venue_names, venue_addresses=venue_addresses,
        config=config, pending_suggestions=pending,
    )


def publish_dedup_backlog_gauges(venue_dao, *, redis_like=None) -> Optional[DedupBacklog]:
    """Push the six measures onto `EVENT_DEDUP_BACKLOG{measure}` — the exact
    shape `app.services.event_reconciliation.update_events_gauge` already
    establishes (a full scan at the end of a run, pushed from the job; there
    is no collect-on-scrape collector in this repo).

    NEVER fails its caller: a backlog-scan failure is an observability
    problem, never a reason to fail the crawl that triggered it (plan's own
    Failure posture). Returns the backlog it published, or `None` on failure.
    """
    from app.metrics import EVENT_DEDUP_BACKLOG

    try:
        backlog = collect_dedup_backlog(venue_dao, redis_like=redis_like)
    except Exception as e:
        logger.warning(f"[EventDedupBacklog] backlog scan failed, gauges left stale: {e}")
        return None
    for measure, value in backlog.measures().items():
        EVENT_DEDUP_BACKLOG.labels(measure=measure).set(value)
    logger.info(
        "[EventDedupBacklog] venue_night_groups=%d venue_night_excess_rows=%d "
        "refused_disjoint=%d refused_no_distinctive_tokens=%d pending_suggestions=%d "
        "attribution_disputed_rows=%d",
        *(backlog.measures()[m] for m in BACKLOG_MEASURES),
    )
    return backlog


__all__ = [
    "BACKLOG_MEASURES",
    "MEASURE_VENUE_NIGHT_GROUPS", "MEASURE_VENUE_NIGHT_EXCESS_ROWS",
    "MEASURE_REFUSED_DISJOINT_PAIRS", "MEASURE_REFUSED_NO_DISTINCTIVE_TOKENS_PAIRS",
    "MEASURE_PENDING_SUGGESTIONS", "MEASURE_ATTRIBUTION_DISPUTED_ROWS",
    "VenueNightGroup", "AttributionDispute", "DedupBacklog",
    "is_live_event_row", "row_location_text", "compute_dedup_backlog",
    "collect_dedup_backlog", "publish_dedup_backlog_gauges",
]
