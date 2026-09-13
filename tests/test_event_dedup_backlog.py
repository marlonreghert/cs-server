"""Unit tests for app/services/event_dedup_backlog.py.

See plans/260912_events-venue-night-duplication.md §A. Pins the PURE backlog
function: the group and excess-row arithmetic across a mixed corpus, every
exclusion rule (superseded, non-event, venue-less, dateless), the refusal
split by reason, the dispute index keyed by account, and an empty corpus
yielding zeros rather than an error.

Division of labour against the BDD sibling
(`tests/bdd/observability/events-venue-night-duplication-backlog.feature`):
the scenarios there prove an OPERATOR can read these numbers — through the
real admin route, and published as gauges by a real extraction run. This
file pins the arithmetic itself, including the shapes a scenario would have
to contrive a whole fixture for (a row with no date, a dispute whose text IS
the venue's own address).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_attribution_dispute import evaluate_attribution_dispute
from app.services.event_venue_resolution import VenueLite
from app.services.event_dedup_backlog import (
    BACKLOG_MEASURES,
    MEASURE_ATTRIBUTION_DISPUTED_ROWS,
    MEASURE_PENDING_SUGGESTIONS,
    MEASURE_REFUSED_DISJOINT_PAIRS,
    MEASURE_REFUSED_NO_DISTINCTIVE_TOKENS_PAIRS,
    MEASURE_VENUE_NIGHT_EXCESS_ROWS,
    MEASURE_VENUE_NIGHT_GROUPS,
    collect_dedup_backlog,
    compute_dedup_backlog,
    is_live_event_row,
    row_location_text,
)
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")

_CONFIG = event_dedup.DedupConfig(
    generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
    stopwords=event_dedup.DEFAULT_STOPWORDS,
    lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
    candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
    undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
    auto_merge_enabled=False,
)

_VENUE_NAMES = {"v1": "Club Metrópole", "v2": "BeerDock Boa Viagem"}
_VENUE_ADDRESSES = {
    "v1": "R. das Ninfas, 125 - Boa Vista, Recife - PE",
    "v2": "Av. Cons. Aguiar, 1000 - Boa Viagem, Recife - PE",
}

# The crawl moment every seeded source shares — these fixtures are about
# grouping and grading, never about recency.
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

_SEQ = {"n": 0}
# A sentinel, never `None`: `starts_at=None` is a REAL fixture shape here
# (a dateless row belongs to no night), so it cannot double as "use the
# default date".
_UNSET = object()


def _local(date_str: str, time_str: str = "21:00") -> datetime:
    hour, minute = (int(p) for p in time_str.split(":"))
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=RECIFE)


def _row(title, venue_id="v1", *, starts_at=_UNSET, post_type="event",
         status="accepted", superseded_by=None, location_text=None,
         source_handle="a_handle", raw_location_text=None, event_id=None):
    _SEQ["n"] += 1
    return {
        "event_id": event_id or f"evt_{_SEQ['n']:04d}",
        "venue_id": venue_id,
        "starts_at": _local("2026-09-12") if starts_at is _UNSET else starts_at,
        "title": title, "post_type": post_type, "status": status,
        "superseded_by": superseded_by, "lineup": [],
        "location_text": location_text, "source_handle": source_handle,
        "raw_extraction": {"location_text": raw_location_text} if raw_location_text else {},
    }


# The BeerDock chain, as `VenueLite`s — the real ladder's own input shape.
_CATALOG = [
    VenueLite(venue_id="v1", venue_name="Club Metrópole",
              address=_VENUE_ADDRESSES["v1"], lat=-8.05, lng=-34.88),
    VenueLite(venue_id="v2", venue_name="BeerDock Boa Viagem",
              address=_VENUE_ADDRESSES["v2"], lat=-8.12, lng=-34.90),
    VenueLite(venue_id="v3", venue_name="BeerDock Casa Forte",
              address="Av. Rui Barbosa, 500 - Casa Forte, Recife - PE",
              lat=-8.03, lng=-34.91),
]
_HANDLE_INDEX = {"beerdock_recife": "v2", "beerdock.casaforte": "v3"}


def _real_evaluator(row):
    """The PRODUCTION dispute rule, bound to `_CATALOG` — exactly what
    `collect_dedup_backlog` injects. Using the real ladder here rather than a
    stub is the point: the bug this replaced was a second, cruder notion of
    disagreement living in the backlog module."""
    return evaluate_attribution_dispute(
        mapped_venue_id=row.get("venue_id"),
        location_text=row_location_text(row),
        venues=_CATALOG, handle_index=_HANDLE_INDEX,
        promoter_handle=row.get("source_handle"),
        operator_edited_fields=row.get("operator_edited_fields"),
    )


def _backlog(rows, **kwargs):
    kwargs.setdefault("venue_names", _VENUE_NAMES)
    kwargs.setdefault("config", _CONFIG)
    return compute_dedup_backlog(rows, **kwargs)


def _backlog_with_ladder(rows, **kwargs):
    kwargs.setdefault("dispute_evaluator", _real_evaluator)
    return _backlog(rows, **kwargs)


class TestCorpusFilter:
    def test_a_live_event_row_counts(self):
        assert is_live_event_row(_row("ROWKA")) is True

    def test_a_menu_row_is_excluded(self):
        assert is_live_event_row(_row("Especial do dia", post_type="menu")) is False

    def test_a_superseded_row_is_excluded_by_status(self):
        assert is_live_event_row(_row("ROWKA", status="superseded")) is False

    def test_a_row_carrying_superseded_by_is_excluded_even_when_its_status_is_not(self):
        # The two are written by different paths — `_finish_absorption`
        # writes both, a re-extraction supersede writes status first — so
        # either alone must disqualify a row from the duplicate population.
        assert is_live_event_row(_row("ROWKA", superseded_by="evt_other")) is False

    def test_a_venue_less_row_is_excluded(self):
        assert is_live_event_row(_row("ROWKA", venue_id=None)) is False


class TestVenueNightGroups:
    def test_three_rows_on_one_night_are_one_group_with_two_excess_rows(self):
        backlog = _backlog([_row("ROWKA"), _row("VITINHO"), _row("SÁBADO VAI FERVER")])
        assert backlog.venue_night_group_count == 1
        assert backlog.venue_night_excess_rows == 2
        assert backlog.venue_night_groups[0].row_count == 3

    def test_excess_rows_sum_across_groups(self):
        rows = [
            _row("ROWKA"), _row("VITINHO"), _row("SÁBADO"),
            _row("DOMINGO", venue_id="v2", starts_at=_local("2026-09-13")),
            _row("TARDEZINHA", venue_id="v2", starts_at=_local("2026-09-13")),
        ]
        backlog = _backlog(rows)
        assert backlog.venue_night_group_count == 2
        assert backlog.venue_night_excess_rows == 3

    def test_one_row_per_night_is_no_group_at_all(self):
        rows = [_row("ROWKA"), _row("DOMINGO", venue_id="v2", starts_at=_local("2026-09-13"))]
        backlog = _backlog(rows)
        assert backlog.venue_night_groups == []
        assert backlog.venue_night_excess_rows == 0

    def test_two_rows_at_the_same_venue_on_different_nights_are_not_a_group(self):
        rows = [_row("ROWKA"), _row("VITINHO", starts_at=_local("2026-09-13"))]
        assert _backlog(rows).venue_night_groups == []

    def test_two_rows_on_one_night_at_different_venues_are_not_a_group(self):
        rows = [_row("ROWKA"), _row("VITINHO", venue_id="v2")]
        assert _backlog(rows).venue_night_groups == []

    def test_a_row_with_no_date_belongs_to_no_night(self):
        # No date is no night: `in_candidate_window` refuses such a pair for
        # exactly this reason, and reporting it as a same-night duplicate
        # would be inventing a night the row never stated.
        rows = [_row("ROWKA"), _row("ROWKA TEASER", starts_at=None)]
        assert _backlog(rows).venue_night_groups == []

    def test_the_group_is_dated_in_recife_local_time_not_utc(self):
        # A 21:00 Recife start is 00:00 UTC the NEXT day; the group must be
        # reported on the night the venue actually ran it.
        rows = [_row("ROWKA", starts_at=_local("2026-09-12", "21:00")),
                _row("VITINHO", starts_at=_local("2026-09-12", "23:30"))]
        assert _backlog(rows).venue_night_groups[0].local_date == "2026-09-12"

    def test_the_group_names_the_venue_and_lists_every_title(self):
        backlog = _backlog([_row("ROWKA"), _row("VITINHO")])
        group = backlog.venue_night_groups[0]
        assert group.venue_name == "Club Metrópole"
        assert set(group.titles) == {"ROWKA", "VITINHO"}

    def test_a_superseded_member_leaves_the_group_below_two(self):
        rows = [_row("ROWKA"), _row("VITINHO", status="superseded")]
        assert _backlog(rows).venue_night_groups == []


class TestRefusalSplit:
    def test_two_disjoint_titles_on_one_night_are_one_disjoint_refusal(self):
        backlog = _backlog([_row("ROWKA"), _row("VITINHO POLÊMICO")])
        assert backlog.refused_disjoint_pairs == 1
        assert backlog.refused_no_distinctive_tokens_pairs == 0

    def test_two_entirely_generic_titles_are_a_no_distinctive_tokens_refusal(self):
        backlog = _backlog([_row("Sextou"), _row("Festa")])
        assert backlog.refused_no_distinctive_tokens_pairs == 1
        assert backlog.refused_disjoint_pairs == 0

    def test_a_pair_the_merge_layer_would_act_on_is_not_a_refusal(self):
        backlog = _backlog([_row("Rodolpho"), _row("Rodolpho Produções")])
        assert backlog.refused_disjoint_pairs == 0
        assert backlog.refused_no_distinctive_tokens_pairs == 0

    def test_three_mutually_disjoint_titles_are_three_refused_pairs(self):
        backlog = _backlog([_row("ROWKA"), _row("VITINHO"), _row("SÁBADO VAI FERVER")])
        assert backlog.refused_disjoint_pairs == 3

    def test_pairs_outside_the_candidate_window_are_never_counted(self):
        rows = [_row("ROWKA"), _row("VITINHO", starts_at=_local("2026-10-12"))]
        backlog = _backlog(rows)
        assert backlog.refused_disjoint_pairs == 0


class TestAttributionDisputes:
    """Graded by the REAL resolution ladder, never by a local heuristic.

    The bug this class replaced compared `location_text` against the venue's
    own name/address by substring containment, in the wrong direction: a
    venue describing ITSELF in ordinary prose ("nossa unidade de Boa Viagem",
    or any of the 14 distinct spellings of its own address that Conchittas
    Bar carries in production) was reported as disputing its own attribution.
    That is precisely the naive string comparison
    `event_attribution_dispute`'s own docstring exists to refuse, and the
    noise would have buried the real chain mis-attribution the report is for.
    """

    def test_a_handle_naming_a_sibling_branch_is_a_dispute_with_a_target(self):
        rows = [
            _row("SAMBINHA", venue_id="v2", raw_location_text="@beerdock.casaforte",
                 source_handle="beerdock_recife"),
            _row("ROCK NIGHT", venue_id="v2", raw_location_text="@beerdock.casaforte",
                 source_handle="beerdock_recife"),
        ]
        backlog = _backlog_with_ladder(rows)
        assert len(backlog.attribution_disputes) == 1
        dispute = backlog.attribution_disputes[0]
        assert dispute.source_handle == "beerdock_recife"
        assert dispute.row_count == 2
        assert dispute.method == "handle_mention"
        # The field that was ALWAYS null before: an operator needs to know
        # which venue to accept.
        assert dispute.resolves_to_venue_id == "v3"
        assert dispute.resolves_to_venue_name == "BeerDock Casa Forte"
        assert backlog.attribution_disputed_rows == 2

    def test_a_sibling_branch_s_neighbourhood_is_a_dispute(self):
        # The production string: 20 of 44 live rows at BeerDock Boa Viagem.
        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE",
                     source_handle="beerdock_recife")]
        dispute = _backlog_with_ladder(rows).attribution_disputes[0]
        assert dispute.method == "neighbourhood_match"
        assert dispute.resolves_to_venue_id == "v3"

    def test_an_unknown_branch_is_a_dispute_with_no_target(self):
        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="@beerdock.madalena",
                     source_handle="beerdock_recife")]
        dispute = _backlog_with_ladder(rows).attribution_disputes[0]
        assert dispute.method == "venue_not_in_catalog"
        assert dispute.resolves_to_venue_id is None

    def test_a_venue_describing_itself_in_prose_is_never_a_dispute(self):
        """The regression this class exists for. None of these is a substring
        of the venue's own name, and every one of them is a venue talking
        about itself."""
        for text in (
            "nossa unidade de Boa Viagem",
            "BeerDock Boa Viagem — Av. Cons. Aguiar, 1000",
            "Av. Cons. Aguiar, 1000 — Boa Viagem, Recife",
            "Boa Viagem",
            "BeerDock",
        ):
            rows = [_row("SAMBINHA", venue_id="v2", raw_location_text=text,
                         source_handle="beerdock_recife")]
            assert _backlog_with_ladder(rows).attribution_disputes == [], text

    def test_a_spelling_variant_of_the_venue_s_own_address_is_never_a_dispute(self):
        # Conchittas Bar carries 14 of these in production; under the old
        # substring check nearly all of them were counted as disputes.
        rows = [_row("SEXTOU", venue_id="v1",
                     raw_location_text="Rua da Imperatriz Teresa Cristina, nº 218",
                     source_handle="conchittasbar")]
        assert _backlog_with_ladder(rows).attribution_disputes == []

    def test_a_fuzzy_name_match_is_never_a_dispute(self):
        # Rung 4 is excluded by the ladder itself — `260806` §D's ban.
        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="nosso clube",
                     source_handle="beerdock_recife")]
        assert _backlog_with_ladder(rows).attribution_disputes == []

    def test_distinct_location_texts_are_reported_separately_with_their_own_counts(self):
        rows = [
            _row("A", venue_id="v2", raw_location_text="CASA FORTE", source_handle="h"),
            _row("B", venue_id="v2", raw_location_text="CASA FORTE", source_handle="h"),
            _row("C", venue_id="v2", raw_location_text="@beerdock.madalena", source_handle="h"),
        ]
        by_text = {
            d.location_text: d.row_count
            for d in _backlog_with_ladder(rows).attribution_disputes
        }
        assert by_text == {"CASA FORTE": 2, "@beerdock.madalena": 1}

    def test_an_operator_edited_venue_is_never_a_dispute(self):
        row = _row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE")
        row["operator_edited_fields"] = ["venue_id"]
        assert _backlog_with_ladder([row]).attribution_disputes == []

    def test_an_operator_edited_location_text_column_outranks_the_frozen_extraction(self):
        row = _row("SAMBINHA", venue_id="v2", location_text="Boa Viagem",
                   raw_location_text="CASA FORTE")
        assert row_location_text(row) == "Boa Viagem"
        assert _backlog_with_ladder([row]).attribution_disputes == []

    def test_a_row_with_no_location_text_is_never_a_dispute(self):
        assert _backlog_with_ladder([_row("SAMBINHA", venue_id="v2")]).attribution_disputes == []

    def test_no_evaluator_reports_no_disputes_rather_than_guessing(self):
        """Grading a dispute is the ladder's job. A caller with no catalog to
        hand gets zero, never a cheaper local answer — the whole lesson of
        `260806` §D and `260813`."""
        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE")]
        assert _backlog(rows).attribution_disputes == []
        assert _backlog(rows).attribution_disputed_rows == 0

    def test_a_failing_evaluator_never_fails_the_backlog(self):
        def _boom(row):
            raise RuntimeError("ladder exploded")

        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE")]
        backlog = _backlog(rows, dispute_evaluator=_boom)
        assert backlog.attribution_disputes == []
        # The rest of the report still computes.
        assert backlog.live_rows == 1


class TestMeasures:
    def test_an_empty_corpus_yields_zeros_not_an_error(self):
        measures = _backlog([]).measures()
        assert set(measures) == set(BACKLOG_MEASURES)
        assert all(value == 0 for value in measures.values())

    def test_every_measure_is_always_present(self):
        # `event_dedup_backlog{measure="..."}` must report an honest 0 from
        # the first run rather than no data point at all — the same reason
        # ALL_STATUSES exists for EVENTS_TOTAL.
        rows = [_row("ROWKA"), _row("VITINHO")]
        assert set(_backlog(rows).measures()) == set(BACKLOG_MEASURES)

    def test_the_measures_carry_the_computed_values(self):
        rows = [
            _row("ROWKA"), _row("VITINHO"),
            _row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE"),
        ]
        measures = _backlog_with_ladder(rows, pending_suggestions=4).measures()
        assert measures[MEASURE_VENUE_NIGHT_GROUPS] == 1
        assert measures[MEASURE_VENUE_NIGHT_EXCESS_ROWS] == 1
        assert measures[MEASURE_REFUSED_DISJOINT_PAIRS] == 1
        assert measures[MEASURE_REFUSED_NO_DISTINCTIVE_TOKENS_PAIRS] == 0
        assert measures[MEASURE_PENDING_SUGGESTIONS] == 4
        assert measures[MEASURE_ATTRIBUTION_DISPUTED_ROWS] == 1

    def test_a_naive_starts_at_is_read_as_utc_rather_than_raising(self):
        rows = [
            _row("ROWKA", starts_at=datetime(2026, 9, 13, 0, 0)),
            _row("VITINHO", starts_at=datetime(2026, 9, 13, 1, 0)),
        ]
        assert _backlog(rows).venue_night_groups[0].local_date == "2026-09-12"

    def test_computing_the_backlog_never_mutates_the_rows_it_reads(self):
        rows = [_row("ROWKA"), _row("VITINHO")]
        before = [dict(r) for r in rows]
        _backlog(rows)
        assert rows == before


def test_the_backlog_reports_an_aware_utc_row_on_its_recife_night():
    rows = [
        _row("ROWKA", starts_at=datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)),
        _row("VITINHO", starts_at=datetime(2026, 9, 13, 2, 30, tzinfo=timezone.utc)),
    ]
    assert _backlog(rows).venue_night_groups[0].local_date == "2026-09-12"


class TestTheRefusalScanAsksTheMergeLayerSQuestion:
    """The backlog's refusal scan must evaluate a pair with the SAME context
    the merge layer evaluates it with — `event_merge._run_pairwise_pass` and
    `scripts.measure_event_dedup.measure` both resolve `single_night_venue`
    per venue and pass it into `evaluate_pair`.

    The bug this class exists for: the scan omitted it. With the catalog-wide
    single-night scope on — which is the scope the operator chose, so this is
    the NORMAL configuration, not an edge case — every same-night pair at
    every venue reaches the auto band via `REASON_SINGLE_NIGHT_VENUE` in the
    real merge path, while the backlog kept filing those very pairs under
    `refused_disjoint_pairs`. That is the gauge the before/after verification
    and the week-long standing watch are read from: it would have reported a
    growing backlog of refusals for pairs the pipeline had just merged.
    """

    def _catalog_wide(self):
        return event_dedup.DedupConfig(
            generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
            stopwords=event_dedup.DEFAULT_STOPWORDS,
            lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
            candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
            undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
            auto_merge_enabled=True, single_night_default_enabled=True,
        )

    def _listed(self, *venue_ids):
        return event_dedup.DedupConfig(
            generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
            stopwords=event_dedup.DEFAULT_STOPWORDS,
            lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
            candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
            undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
            auto_merge_enabled=True, single_night_venues=tuple(venue_ids),
        )

    def test_a_disjoint_pair_is_refused_while_the_scope_is_off(self):
        rows = [_row("ROWKA"), _row("VITINHO POLÊMICO")]
        backlog = _backlog(rows)
        assert backlog.refused_disjoint_pairs == 1

    def test_the_same_pair_is_not_refused_under_the_catalog_wide_scope(self):
        rows = [_row("ROWKA"), _row("VITINHO POLÊMICO")]
        backlog = _backlog(rows, config=self._catalog_wide())
        assert backlog.refused_disjoint_pairs == 0
        assert backlog.refused_no_distinctive_tokens_pairs == 0

    def test_an_all_generic_pair_is_not_refused_either(self):
        rows = [_row("Sextou"), _row("Festa")]
        assert _backlog(rows).refused_no_distinctive_tokens_pairs == 1
        assert _backlog(
            rows, config=self._catalog_wide(),
        ).refused_no_distinctive_tokens_pairs == 0

    def test_a_listed_venue_is_not_refused_while_an_unlisted_one_still_is(self):
        rows = [
            _row("ROWKA", venue_id="v1"), _row("VITINHO POLÊMICO", venue_id="v1"),
            _row("ROWKA", venue_id="v2"), _row("VITINHO POLÊMICO", venue_id="v2"),
        ]
        backlog = _backlog(rows, config=self._listed("v1"))
        assert backlog.refused_disjoint_pairs == 1, (
            "only the UNLISTED venue's pair should still count as refused"
        )

    def test_the_backlog_agrees_with_the_merge_layer_pair_for_pair(self):
        """The module docstring's promise, asserted directly: for every pair
        the scan considers, "refused" here must mean `evaluate_pair` returned
        None there, under the SAME config — including its single-night
        context."""
        rows = [
            _row("ROWKA", venue_id="v1"), _row("VITINHO POLÊMICO", venue_id="v1"),
            _row("Sextou", venue_id="v2"), _row("Festa", venue_id="v2"),
        ]
        for config in (_CONFIG, self._catalog_wide(), self._listed("v1")):
            backlog = _backlog(rows, config=config)
            expected = 0
            for venue_id in ("v1", "v2"):
                pair = [r for r in rows if r["venue_id"] == venue_id]
                decision = event_dedup.evaluate_pair(
                    pair[0], pair[1], venue_name=_VENUE_NAMES[venue_id], config=config,
                    single_night_venue=config.is_single_night_venue(venue_id),
                )
                if decision is None:
                    expected += 1
            actual = (
                backlog.refused_disjoint_pairs
                + backlog.refused_no_distinctive_tokens_pairs
            )
            assert actual == expected, (config.single_night_default_enabled, actual, expected)

    def test_the_venue_night_groups_are_unaffected_by_the_scope(self):
        # The duplicate POPULATION is a fact about stored rows, not about the
        # merge bar — only the refusal split moves with the config.
        rows = [_row("ROWKA"), _row("VITINHO POLÊMICO")]
        assert _backlog(rows).venue_night_excess_rows == 1
        assert _backlog(rows, config=self._catalog_wide()).venue_night_excess_rows == 1


class TestTheDisputeScanIsNotQuadratic:
    """`collect_dedup_backlog` runs the resolution ladder over the WHOLE live
    corpus — at the end of every extraction run, and on every
    `GET /admin/events/dedup-backlog` hit. One ladder run per ROW over a
    ~3,600-venue catalog made that take 277s at prod scale, at 85% CPU, and
    it starved the Redis-projection job ("maximum number of running
    instances reached").

    Two things fix it, and this class pins both by COUNT rather than by
    clock, so it cannot rot into a flaky timing test:
      - the verdict is memoised on the full set of inputs it depends on, so
        the ladder runs once per DISTINCT question, not once per row;
      - the ladder's one superlinear rung is skipped where its answer is
        unobservable (pinned in `tests/test_event_attribution_dispute.py`).
    """

    def _store(self, *, n_venues, n_rows, distinct_texts):
        store = InMemoryRdsVenueStore()
        for i in range(n_venues):
            store.upsert_venue(Venue(
                venue_id=f"pv_{i:04d}", venue_name=f"Bar Exemplo {i}",
                venue_address=f"Rua {i}, {i} - Boa Viagem, Recife - PE",
                venue_lat=-8.05, venue_lng=-34.88,
            ))
        texts = [f"Rua {i} do Exemplo, {i} - Recife - PE" for i in range(distinct_texts)]
        for i in range(n_rows):
            store.insert_event({
                "event_id": new_event_id(), "venue_id": "pv_0000",
                "starts_at": _local("2026-09-12"), "title": f"Show {i}",
                "post_type": "event", "status": "accepted", "lineup": [],
                "source_kind": "venue_post", "source_handle": "one_handle",
                "source_shortcode": f"perf_sc_{i}",
                "first_seen_at": _NOW, "last_seen_at": _NOW,
                "raw_extraction": {"location_text": texts[i % distinct_texts]},
            })
        return store

    def test_the_ladder_runs_once_per_distinct_question_not_once_per_row(self, monkeypatch):
        import app.services.event_dedup_backlog as backlog_mod

        calls = []
        real = backlog_mod.__dict__.get("evaluate_attribution_dispute")

        import app.services.event_attribution_dispute as dispute_mod

        real_fn = dispute_mod.evaluate_attribution_dispute

        def _counted(**kwargs):
            calls.append(kwargs.get("location_text"))
            return real_fn(**kwargs)

        monkeypatch.setattr(dispute_mod, "evaluate_attribution_dispute", _counted)

        store = self._store(n_venues=40, n_rows=120, distinct_texts=8)
        collect_dedup_backlog(store)

        assert len(calls) == 8, (
            f"120 rows with 8 distinct location texts should cost 8 ladder runs, "
            f"not {len(calls)}"
        )
        assert len(set(calls)) == 8

    def test_the_cache_key_separates_rows_that_ask_different_questions(self, monkeypatch):
        """The memo is only sound if the key carries everything the verdict
        depends on. Same text, DIFFERENT mapped venue / posting handle /
        operator pin must each be asked separately."""
        import app.services.event_attribution_dispute as dispute_mod

        calls = []
        real_fn = dispute_mod.evaluate_attribution_dispute

        def _counted(**kwargs):
            calls.append((
                kwargs.get("mapped_venue_id"), kwargs.get("location_text"),
                kwargs.get("promoter_handle"),
            ))
            return real_fn(**kwargs)

        monkeypatch.setattr(dispute_mod, "evaluate_attribution_dispute", _counted)

        store = InMemoryRdsVenueStore()
        for vid, name in (("pv_a", "Bar A"), ("pv_b", "Bar B")):
            store.upsert_venue(Venue(
                venue_id=vid, venue_name=name, venue_address=f"Rua {name}, 1 - Recife",
                venue_lat=-8.05, venue_lng=-34.88,
            ))
        variants = [
            ("pv_a", "handle_one", None),
            ("pv_a", "handle_one", None),      # identical -> cached
            ("pv_b", "handle_one", None),      # other venue
            ("pv_a", "handle_two", None),      # other handle
            ("pv_a", "handle_one", ["venue_id"]),  # operator pinned the venue
        ]
        for i, (vid, handle, edited) in enumerate(variants):
            store.insert_event({
                "event_id": new_event_id(), "venue_id": vid,
                "starts_at": _local("2026-09-12"), "title": f"Show {i}",
                "post_type": "event", "status": "accepted", "lineup": [],
                "operator_edited_fields": edited,
                "source_kind": "venue_post", "source_handle": handle,
                "source_shortcode": f"key_sc_{i}",
                "first_seen_at": _NOW, "last_seen_at": _NOW,
                "raw_extraction": {"location_text": "CASA FORTE"},
            })
        collect_dedup_backlog(store)
        assert len(calls) == 4, f"expected 4 distinct questions, got {len(calls)}: {calls}"

    def test_the_fuzzy_scan_does_not_grow_with_the_corpus(self, monkeypatch):
        """The complexity claim itself: `name_similarity` is the ladder's
        superlinear step, and for a corpus whose texts name no unrecognized
        handle it must not run at all — so the cost stops being
        rows x catalog."""
        import app.services.event_venue_resolution as evr

        calls = []
        real = evr.name_similarity
        monkeypatch.setattr(
            evr, "name_similarity",
            lambda *a, **kw: (calls.append(a), real(*a, **kw))[1],
        )
        store = self._store(n_venues=200, n_rows=200, distinct_texts=20)
        collect_dedup_backlog(store)
        assert calls == [], (
            f"{len(calls)} fuzzy comparisons ran for a corpus that needs none"
        )

    def test_the_report_is_identical_with_and_without_the_cache(self):
        """A performance fix must change no answer. Same corpus, once
        through `collect_dedup_backlog` (memoised) and once through
        `compute_dedup_backlog` with an evaluator that refuses to cache."""
        from app.services.event_attribution_dispute import evaluate_attribution_dispute
        from app.services.event_venue_resolution import (
            build_handle_index, build_venue_catalog,
        )

        store = InMemoryRdsVenueStore()
        for vid, name, addr in (
            ("v_bv", "BeerDock Boa Viagem", "Av. Cons. Aguiar, 1000 - Boa Viagem, Recife"),
            ("v_cf", "BeerDock Casa Forte", "Av. Rui Barbosa, 500 - Casa Forte, Recife"),
            ("v_mc", "Maria Café", "R. do Sol, 7 - Santo Antônio, Recife"),
        ):
            store.upsert_venue(Venue(
                venue_id=vid, venue_name=name, venue_address=addr,
                venue_lat=-8.05, venue_lng=-34.88,
            ))
        for i, text in enumerate([
            "CASA FORTE", "CASA FORTE", "@beerdock.madalena", "nossa unidade de Boa Viagem",
            "Maria Café", "@naoexiste Maria Café", None,
        ]):
            store.insert_event({
                "event_id": new_event_id(), "venue_id": "v_bv",
                "starts_at": _local("2026-09-12"), "title": f"Show {i}",
                "post_type": "event", "status": "accepted", "lineup": [],
                "source_kind": "venue_post", "source_handle": "beerdock_recife",
                "source_shortcode": f"cmp_sc_{i}",
                "first_seen_at": _NOW, "last_seen_at": _NOW,
                "raw_extraction": {"location_text": text} if text else {},
            })

        cached = collect_dedup_backlog(store)

        catalog = build_venue_catalog(store)
        handle_index = build_handle_index(store)
        uncached = compute_dedup_backlog(
            store.list_events(),
            venue_names={v.venue_id: v.venue_name for v in catalog},
            config=event_dedup.load_dedup_config(None),
            dispute_evaluator=lambda row: evaluate_attribution_dispute(
                mapped_venue_id=row.get("venue_id"),
                location_text=row_location_text(row),
                venues=catalog, handle_index=handle_index,
                promoter_handle=row.get("source_handle"),
                operator_edited_fields=row.get("operator_edited_fields"),
            ),
        )
        assert cached.measures() == uncached.measures()
        assert [d.to_dict() for d in cached.attribution_disputes] == [
            d.to_dict() for d in uncached.attribution_disputes
        ]
