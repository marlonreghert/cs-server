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
    # Which RUNG of the ladder graded this — `handle_mention`,
    # `location_tag`, `neighbourhood_match`, or `venue_not_in_catalog`. An
    # operator reading the report needs it: the first three name a venue we
    # DO carry (accept the candidate on the row), the last is the
    # venue-acquisition backlog (add the venue first).
    method: Optional[str] = None
    resolves_to_venue_id: Optional[str] = None
    resolves_to_venue_name: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source_handle": self.source_handle, "location_text": self.location_text,
            "row_count": self.row_count, "event_ids": list(self.event_ids),
            "venue_ids": list(self.venue_ids), "method": self.method,
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
    dispute_evaluator=None,
) -> DedupBacklog:
    """The whole backlog over `rows` (every `events.post_item` row the
    caller fetched — this function filters to the live event corpus itself
    via `is_live_event_row`, so a caller never has to remember the rule).

    `venue_names` maps `venue_id` -> the venue's own name (the refusal scan
    needs it to strip a venue's own words out of a title's distinctive set).

    `dispute_evaluator(row) -> Optional[DisputeVerdict]` grades one row's
    attribution, and is `app.services.event_attribution_dispute.
    evaluate_attribution_dispute` bound to the catalog — injected rather than
    imported so this module stays pure and so there is exactly ONE definition
    of what a dispute is. `None` (a caller with no catalog to hand) reports
    NO disputes rather than guessing: grading a dispute is the resolution
    ladder's job, and the whole lesson of `260806` §D and `260813` is that a
    cheaper local heuristic gets it confidently wrong.
    """
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
        # §E2, resolved once per venue — the SAME way
        # `event_merge.run_title_similarity_pass` and
        # `scripts.measure_event_dedup.measure` resolve it. Without this the
        # backlog asks a DIFFERENT question from the merge layer: with the
        # catalog-wide single-night scope on, every same-night pair at every
        # venue reaches the auto band via REASON_SINGLE_NIGHT_VENUE, and a
        # scan that omitted the flag would keep filing those under
        # `refused_disjoint_pairs` — reporting as an unactioned backlog the
        # very pairs the pipeline had just merged, in the one gauge the
        # before/after verification and the week-long standing watch are
        # read from. This module's docstring promises it "can never disagree
        # with what the merge layer would actually do about a pair"; this
        # line is most of that promise.
        single_night_venue = config.is_single_night_venue(venue_id)
        for a, b in combinations(members, 2):
            if not event_dedup.in_candidate_window_for_rows(
                a, b, window_hours=config.candidate_window_hours,
                recurring_window_enabled=config.recurring_window_enabled,
            ):
                continue
            if event_dedup.evaluate_pair(
                a, b, venue_name=venue_name, config=config,
                single_night_venue=single_night_venue,
            ) is not None:
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
    # Graded by `event_attribution_dispute.evaluate_attribution_dispute` —
    # THE resolution ladder — and never by a second, cruder notion of
    # disagreement living here.
    #
    # This module previously compared `location_text` against the venue's own
    # name and address by substring containment, and that was wrong in both
    # directions of the very failure `event_attribution_dispute`'s docstring
    # warns about: `"nossa unidade de Boa Viagem"` is not a substring of
    # `"BeerDock Boa Viagem"`, so a venue describing ITSELF in ordinary prose
    # was reported as disputing its own attribution. Measured against the
    # live corpus that is most of the index — Conchittas Bar alone carries 14
    # distinct self-referential spellings of its own address — which is
    # exactly the noise that would bury the real chain mis-attribution
    # (BeerDock's 20 `CASA FORTE` rows) this report exists to surface.
    disputes: dict = {}
    verdicts: dict = {}
    if dispute_evaluator is not None:
        for row in live:
            if not row_location_text(row):
                continue
            try:
                verdict = dispute_evaluator(row)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[EventDedupBacklog] dispute evaluation failed: {e}")
                continue
            if verdict is None:
                continue
            key = (row.get("source_handle"), verdict.location_text)
            disputes.setdefault(key, []).append(row)
            verdicts.setdefault(key, verdict)

    for (source_handle, location_text), members in sorted(
        disputes.items(), key=lambda kv: (-len(kv[1]), str(kv[0][0]), kv[0][1]),
    ):
        verdict = verdicts[(source_handle, location_text)]
        backlog.attribution_disputes.append(AttributionDispute(
            source_handle=source_handle, location_text=location_text,
            row_count=len(members),
            event_ids=tuple(r["event_id"] for r in members),
            venue_ids=tuple(sorted({r["venue_id"] for r in members})),
            method=verdict.method,
            resolves_to_venue_id=verdict.target_venue_id,
            resolves_to_venue_name=verdict.target_venue_name,
        ))

    return backlog


# ── the one adapter (see this module's docstring) ───────────────────────────
def collect_dedup_backlog(venue_dao, *, redis_like=None) -> DedupBacklog:
    """Fetch and delegate. Holds no rules: the corpus filter, the grouping,
    the refusal split and the dispute grading all live above (or, for the
    grading, in `app.services.event_attribution_dispute`), which is what
    every test drives.

    TWO bulk reads, never a `get_venue` per venue: this runs at the end of
    every extraction run AND on every `GET /admin/events/dedup-backlog` hit,
    against a ~3,600-venue catalog — the same reason
    `redis_projection_service.project_events` prefetches with
    `get_venues_by_ids` rather than querying per row.
    """
    from app.services.event_attribution_dispute import evaluate_attribution_dispute
    from app.services.event_venue_resolution import (
        _venue_field, build_handle_index, build_venue_catalog,
    )

    rows = venue_dao.list_events()
    config = event_dedup.load_dedup_config(redis_like)

    venue_ids = sorted({row["venue_id"] for row in rows if row.get("venue_id")})
    bulk = getattr(venue_dao, "get_venues_by_ids", None)
    if bulk is not None:
        venue_rows = bulk(venue_ids) or {}
    else:  # pragma: no cover - every DAO in this repo has the bulk read
        venue_rows = {
            venue_id: venue for venue_id, venue in (
                (vid, venue_dao.get_venue(vid)) for vid in venue_ids
            ) if venue is not None
        }
    venue_names = {
        venue_id: _venue_field(venue, "venue_name")
        for venue_id, venue in venue_rows.items()
    }

    # The dispute rule, bound to the catalog once — the SAME ladder the live
    # extraction path and the disputed-location-text backfill both use, so
    # the report can never grade a row differently from the pipeline. No
    # `location_tag`: a post's Instagram tag is not stored on the row, which
    # is the identical limitation `scripts.backfill_event_venue_links`
    # records for its own re-decision over stored text.
    catalog = build_venue_catalog(venue_dao)
    handle_index = build_handle_index(venue_dao)

    # Memoised on the FULL set of inputs the verdict actually depends on, so
    # it is a pure cache and not an approximation: the catalog, handle index
    # and vocabularies are fixed for this scan, which leaves the mapped
    # venue, the text itself, the posting handle (it decides which mentions
    # are self-links) and whether an operator pinned `venue_id`. Rows
    # repeat those heavily — BeerDock alone has 20 rows all saying "CASA
    # FORTE", and `oquetemhojeemnatal` posts the same handful of handles
    # hundreds of times — so this collapses one ladder run per ROW into one
    # per DISTINCT question.
    verdict_cache: dict = {}

    def _evaluate(row):
        location_text = row_location_text(row)
        key = (
            row.get("venue_id"), location_text, row.get("source_handle"),
            "venue_id" in (row.get("operator_edited_fields") or []),
        )
        if key not in verdict_cache:
            verdict_cache[key] = evaluate_attribution_dispute(
                mapped_venue_id=row.get("venue_id"),
                location_text=location_text,
                venues=catalog, handle_index=handle_index,
                promoter_handle=row.get("source_handle"),
                operator_edited_fields=row.get("operator_edited_fields"),
                generic_vocabulary=config.generic_vocabulary,
                stopwords=config.stopwords,
            )
        return verdict_cache[key]

    pending = len(venue_dao.list_event_merge_suggestions(decision="pending"))
    return compute_dedup_backlog(
        rows, venue_names=venue_names, config=config,
        pending_suggestions=pending, dispute_evaluator=_evaluate,
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
