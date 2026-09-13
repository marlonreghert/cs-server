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

from app.services import event_dedup
from app.services.event_dedup_backlog import (
    BACKLOG_MEASURES,
    MEASURE_ATTRIBUTION_DISPUTED_ROWS,
    MEASURE_PENDING_SUGGESTIONS,
    MEASURE_REFUSED_DISJOINT_PAIRS,
    MEASURE_REFUSED_NO_DISTINCTIVE_TOKENS_PAIRS,
    MEASURE_VENUE_NIGHT_EXCESS_ROWS,
    MEASURE_VENUE_NIGHT_GROUPS,
    compute_dedup_backlog,
    is_live_event_row,
    row_location_text,
)

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


def _backlog(rows, **kwargs):
    kwargs.setdefault("venue_names", _VENUE_NAMES)
    kwargs.setdefault("venue_addresses", _VENUE_ADDRESSES)
    kwargs.setdefault("config", _CONFIG)
    return compute_dedup_backlog(rows, **kwargs)


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
    def test_a_location_text_naming_another_branch_is_a_dispute_keyed_by_account(self):
        rows = [
            _row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE",
                 source_handle="beerdock_recife"),
            _row("ROCK NIGHT", venue_id="v2", raw_location_text="CASA FORTE",
                 source_handle="beerdock_recife"),
        ]
        backlog = _backlog(rows)
        assert len(backlog.attribution_disputes) == 1
        dispute = backlog.attribution_disputes[0]
        assert dispute.source_handle == "beerdock_recife"
        assert dispute.location_text == "CASA FORTE"
        assert dispute.row_count == 2
        assert backlog.attribution_disputed_rows == 2

    def test_distinct_location_texts_are_reported_separately_with_their_own_counts(self):
        rows = [
            _row("A", venue_id="v2", raw_location_text="CASA FORTE", source_handle="h"),
            _row("B", venue_id="v2", raw_location_text="CASA FORTE", source_handle="h"),
            _row("C", venue_id="v2", raw_location_text="MADALENA", source_handle="h"),
        ]
        by_text = {d.location_text: d.row_count for d in _backlog(rows).attribution_disputes}
        assert by_text == {"CASA FORTE": 2, "MADALENA": 1}

    def test_a_location_text_that_is_the_venue_s_own_neighbourhood_is_not_a_dispute(self):
        # The decisive guard against plan §C's own warning: the majority of
        # CORRECT rows name their own venue's neighbourhood, and reporting
        # those as disputes would bury the real backlog.
        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="Boa Viagem")]
        assert _backlog(rows).attribution_disputes == []

    def test_a_location_text_that_is_the_venue_s_own_name_is_not_a_dispute(self):
        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="BeerDock Boa Viagem")]
        assert _backlog(rows).attribution_disputes == []

    def test_an_operator_edited_location_text_column_outranks_the_frozen_extraction(self):
        row = _row("SAMBINHA", venue_id="v2", location_text="Boa Viagem",
                   raw_location_text="CASA FORTE")
        assert row_location_text(row) == "Boa Viagem"
        assert _backlog([row]).attribution_disputes == []

    def test_a_row_with_no_location_text_is_never_a_dispute(self):
        assert _backlog([_row("SAMBINHA", venue_id="v2")]).attribution_disputes == []

    def test_a_resolver_names_the_venue_the_text_points_at(self):
        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE")]
        backlog = _backlog(
            rows, location_text_resolver=lambda text, venue_id: ("v9", "BeerDock Casa Forte"),
        )
        assert backlog.attribution_disputes[0].resolves_to_venue_id == "v9"
        assert backlog.attribution_disputes[0].resolves_to_venue_name == "BeerDock Casa Forte"

    def test_a_failing_resolver_never_fails_the_backlog(self):
        def _boom(text, venue_id):
            raise RuntimeError("resolver exploded")

        rows = [_row("SAMBINHA", venue_id="v2", raw_location_text="CASA FORTE")]
        backlog = _backlog(rows, location_text_resolver=_boom)
        assert backlog.attribution_disputes[0].resolves_to_venue_id is None


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
        measures = _backlog(rows, pending_suggestions=4).measures()
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
