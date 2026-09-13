"""Unit tests for scripts/measure_event_dedup.py.

See plans/260912_events-venue-night-duplication.md §B. Pins the measurement
knobs that turn this script from "a restatement of its own defaults" into
the instrument §E1's threshold decision is made with: `--lineup-threshold`
reaching `DedupConfig`, `--max-auto-pairs` refusing to write, `--report-json`
round-tripping, and — the one that matters most — the DEFAULT invocation
behaving exactly as it did before any of them existed.

Division of labour against the BDD siblings: the scenarios in
`tests/bdd/enrichment/events-venue-night-duplication.feature` prove what the
SWEEP does to a corpus; this file pins the CLI's own contract, including the
refusal path (an exit code and an untouched corpus), which a scenario can
only observe indirectly.

Nothing here touches a network client, Redis or a real database: the script
is driven against `tests.rds_fake.InMemoryRdsVenueStore` with its two
production constructors monkeypatched out, the same way
`tests/test_backfill_event_venue_links.py` drives its own script.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import scripts.measure_event_dedup as med
from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_SATURDAY = datetime(2026, 9, 12, 21, 0, tzinfo=RECIFE)


@pytest.fixture
def dao(monkeypatch):
    store = InMemoryRdsVenueStore()
    monkeypatch.setattr(med, "RdsVenueStore", lambda url: store)
    monkeypatch.setattr(med, "VenueRepository", lambda client=None, rds_store=None: rds_store)
    store.upsert_venue(Venue(
        venue_id="v1", venue_name="Club Metrópole", venue_lat=-8.05, venue_lng=-34.88,
    ))
    return store


_SEQ = {"n": 0}


def _seed(store, title, *, lineup=None, venue_id="v1", starts_at=_SATURDAY) -> str:
    _SEQ["n"] += 1
    event_id = new_event_id()
    store.insert_event({
        "event_id": event_id, "venue_id": venue_id, "starts_at": starts_at,
        "title": title, "post_type": "event", "status": "accepted",
        "lineup": lineup or [], "source_kind": "venue_post",
        "source_handle": "club_handle", "source_shortcode": f"med_sc_{_SEQ['n']}",
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    })
    return event_id


def _live(store) -> list:
    return [
        row for row in store.list_events()
        if row.get("status") != "superseded"
    ]


class TestBuildConfig:
    def test_the_default_config_is_the_shipped_defaults_with_the_auto_band_on(self):
        config = med.build_config()
        assert config.lineup_threshold == event_dedup.DEFAULT_LINEUP_THRESHOLD
        assert config.candidate_window_hours == event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS
        assert config.undated_window_days == event_dedup.DEFAULT_UNDATED_WINDOW_DAYS
        # The report always reflects what WOULD happen with the auto band on
        # — that is the entire point of measuring before flipping the flag.
        assert config.auto_merge_enabled is True

    def test_the_default_config_widens_nothing(self):
        config = med.build_config()
        assert config.recurring_window_enabled is False
        assert config.single_night_venues == ()

    def test_an_explicit_threshold_reaches_the_config(self):
        assert med.build_config(lineup_threshold=1).lineup_threshold == 1

    def test_explicit_single_night_venues_reach_the_config_as_a_tuple(self):
        config = med.build_config(single_night_venue_ids=["v1", "v2"])
        assert config.single_night_venues == ("v1", "v2")

    def test_the_recurring_window_flag_reaches_the_config(self):
        assert med.build_config(recurring_window_enabled=True).recurring_window_enabled is True


class TestLineupThresholdFlag:
    def test_a_pair_sharing_one_performer_is_not_auto_at_the_shipped_default(self, dao):
        # Two DIFFERENT generic-only titles, so only the lineup signal can
        # decide the band (see event_dedup_fuzzy_title_steps' own note on
        # why the two titles must differ: an identical normalized title
        # would collide on the EXACT identity first).
        _seed(dao, "Sextou", lineup=["DJ Uno"])
        _seed(dao, "Festa", lineup=["DJ Uno"])
        assert med.measure(dao, config=med.build_config()).auto_count == 0

    def test_the_same_pair_is_auto_at_threshold_one(self, dao):
        _seed(dao, "Sextou", lineup=["DJ Uno"])
        _seed(dao, "Festa", lineup=["DJ Uno"])
        report = med.measure(dao, config=med.build_config(lineup_threshold=1))
        assert report.auto_count == 1

    def test_the_cli_flag_reaches_the_measurement(self, dao, caplog):
        _seed(dao, "Sextou", lineup=["DJ Uno"])
        _seed(dao, "Festa", lineup=["DJ Uno"])
        path = None
        with caplog.at_level("INFO"):
            assert med.main(["--lineup-threshold", "1"]) == 0
        assert "lineup_threshold=1" in caplog.text
        assert path is None  # nothing written without --report-json


class TestMaxAutoPairsCeiling:
    def test_apply_writes_nothing_and_exits_non_zero_above_the_ceiling(self, dao):
        a = _seed(dao, "Rodolpho")
        b = _seed(dao, "Rodolpho Produções")
        before = {row["event_id"]: dict(row) for row in dao.list_events()}

        assert med.main(["--apply", "--max-auto-pairs", "0"]) == med.EXIT_CEILING_EXCEEDED

        after = {row["event_id"]: dict(row) for row in dao.list_events()}
        assert after == before, "the ceiling refusal must leave every row untouched"
        assert {a, b} <= set(after)
        assert dao.list_event_merge_suggestions() == []

    def test_apply_proceeds_when_the_ceiling_is_not_exceeded(self, dao):
        _seed(dao, "Rodolpho")
        _seed(dao, "Rodolpho Produções")
        assert med.main(["--apply", "--max-auto-pairs", "5"]) == 0
        assert len(_live(dao)) == 1

    def test_no_ceiling_means_no_ceiling(self, dao):
        _seed(dao, "Rodolpho")
        _seed(dao, "Rodolpho Produções")
        assert med.main(["--apply"]) == 0
        assert len(_live(dao)) == 1


class TestReportJson:
    def test_the_report_round_trips_counts_pairs_and_the_config_it_used(self, dao, tmp_path):
        _seed(dao, "Rodolpho")
        _seed(dao, "Rodolpho Produções")
        _seed(dao, "Ação Leitura: Bate-papo com Marcelino Freire")
        _seed(dao, "Ação Leitura: Bate-papo com Jeferson Tenório")
        target = tmp_path / "reports" / "dedup_t1.json"

        assert med.main(["--lineup-threshold", "1", "--report-json", str(target)]) == 0

        payload = json.loads(target.read_text())
        assert payload["mode"] == "dry_run"
        # A threshold comparison whose files cannot say which threshold
        # produced them is not evidence, it is two numbers.
        assert payload["config"]["lineup_threshold"] == 1
        assert payload["counts"]["auto_pairs"] == 1
        assert payload["counts"]["suggest_pairs"] == 1
        titles = {
            (p["surviving_title"], p["absorbed_title"]) for p in payload["auto_pairs"]
        }
        assert titles == {("Rodolpho", "Rodolpho Produções")}
        assert all(p["surviving_event_id"] and p["absorbed_event_id"] for p in payload["auto_pairs"])

    def test_apply_also_writes_the_post_sweep_re_measurement(self, dao, tmp_path):
        _seed(dao, "Rodolpho")
        _seed(dao, "Rodolpho Produções")
        target = tmp_path / "sweep.json"

        assert med.main(["--apply", "--report-json", str(target)]) == 0

        before = json.loads(target.read_text())
        after = json.loads((tmp_path / "sweep.after.json").read_text())
        assert before["counts"]["auto_pairs"] == 1
        assert after["mode"] == "apply"
        assert after["counts"]["auto_pairs"] == 0

    def test_a_refused_apply_still_writes_the_dry_run_report(self, dao, tmp_path):
        _seed(dao, "Rodolpho")
        _seed(dao, "Rodolpho Produções")
        target = tmp_path / "refused.json"

        assert med.main([
            "--apply", "--max-auto-pairs", "0", "--report-json", str(target),
        ]) == med.EXIT_CEILING_EXCEEDED

        assert json.loads(target.read_text())["counts"]["auto_pairs"] == 1
        assert not (tmp_path / "refused.after.json").exists()


class TestTheDefaultInvocationIsUnchanged:
    def test_a_bare_run_writes_nothing_at_all(self, dao):
        _seed(dao, "Rodolpho")
        _seed(dao, "Rodolpho Produções")
        _seed(dao, "Ação Leitura: Bate-papo com Marcelino Freire")
        _seed(dao, "Ação Leitura: Bate-papo com Jeferson Tenório")
        before = {row["event_id"]: dict(row) for row in dao.list_events()}

        assert med.main([]) == 0

        assert {row["event_id"]: dict(row) for row in dao.list_events()} == before
        # Not even a suggestion row: the dry run is READ-ONLY (plan §B).
        assert dao.list_event_merge_suggestions() == []

    def test_a_bare_measure_reports_the_shipped_bar(self, dao):
        _seed(dao, "Sextou", lineup=["DJ Uno"])
        _seed(dao, "Festa", lineup=["DJ Uno"])
        report = med.measure(dao)
        assert report.auto_count == 0
        assert report.refused_no_distinctive_tokens == 1

    def test_the_sweep_is_idempotent(self, dao):
        _seed(dao, "Rodolpho")
        _seed(dao, "Rodolpho Produções")
        assert med.main(["--apply"]) == 0
        survivors = {row["event_id"] for row in _live(dao)}
        assert med.main(["--apply"]) == 0
        assert {row["event_id"] for row in _live(dao)} == survivors


class TestSingleNightVenueFlag:
    """plans/260912_events-venue-night-duplication.md §E2 through the CLI.

    The regression this class exists for was real and was caught by a BDD
    scenario, not by reading the code: `measure()` originally did not pass
    `single_night_venue` into `evaluate_pair`, so the dry-run report said "0
    auto pairs" for a listed venue whose night `--apply` then collapsed. That
    is the measurement and the sweep disagreeing about a pair — the one thing
    §C2 says must be structurally impossible — and it also made
    `--max-auto-pairs` guard against a number that did not describe the run
    it was guarding.
    """

    def _five_acts(self, dao):
        return [
            _seed(dao, title)
            for title in (
                "ROWKA", "VITINHO POLÊMICO", "SÁBADO VAI FERVER",
                "SECRET CLUB com @neguindabasersv", "estreia de @neguindabasersv",
            )
        ]

    def test_the_dry_run_report_sees_what_the_sweep_would_do(self, dao):
        self._five_acts(dao)
        report = med.measure(dao, config=med.build_config(single_night_venue_ids=["v1"]))
        # Five rows, ten pairs, every one of them an auto pair at a
        # single-night venue.
        assert report.auto_count == 10, report.auto_pairs

    def test_an_unlisted_venue_reports_no_auto_pair(self, dao):
        self._five_acts(dao)
        assert med.measure(dao, config=med.build_config()).auto_count == 0

    def test_the_cli_flag_reaches_the_sweep(self, dao):
        ids = self._five_acts(dao)
        assert med.main(["--apply", "--single-night-venue", "v1"]) == 0
        survivors = [row["event_id"] for row in _live(dao)]
        assert len(survivors) == 1, survivors
        assert set(survivors) <= set(ids)

    def test_the_ceiling_is_measured_against_the_same_bar_the_sweep_uses(self, dao):
        self._five_acts(dao)
        before = {row["event_id"]: dict(row) for row in dao.list_events()}

        assert med.main([
            "--apply", "--single-night-venue", "v1", "--max-auto-pairs", "3",
        ]) == med.EXIT_CEILING_EXCEEDED

        assert {row["event_id"]: dict(row) for row in dao.list_events()} == before

    def test_the_script_never_reads_or_writes_an_admin_config_key(self, dao):
        # The whole point of the flag: measure §E2's policy for named venues
        # WITHOUT committing to it. The script builds its config from the
        # shipped defaults plus this invocation's overrides and never reads
        # the live keys — structural, not a promise, so a future edit that
        # reaches for one has to break this test first.
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(med))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert "load_dedup_config" not in called
        assert "load_attribution_dispute_config" not in called
        # No Redis client of any kind is constructed or imported.
        assert "redis" not in inspect.getsource(med).lower().replace("fakeredis", "")


class TestRecurringWindowFlag:
    def _weekly_pair(self, dao):
        return [
            _seed(dao, "Aula de FORRÓ na Sala de Reboco",
                  starts_at=datetime(2026, 8, 5, 20, 0, tzinfo=RECIFE)),
            _seed(dao, "Aula de FORRÓ na Sala de Reboco",
                  starts_at=datetime(2026, 8, 26, 20, 0, tzinfo=RECIFE)),
        ]

    def _make_recurring(self, dao, ids):
        for event_id in ids:
            dao.update_event(event_id, {
                "is_recurring": True, "recurrence_text": "Toda QUARTA",
            })

    def test_the_two_live_forro_rows_are_invisible_without_the_flag(self, dao):
        self._make_recurring(dao, self._weekly_pair(dao))
        assert med.measure(dao, config=med.build_config()).auto_count == 0

    def test_the_flag_makes_them_a_candidate_pair(self, dao):
        self._make_recurring(dao, self._weekly_pair(dao))
        report = med.measure(dao, config=med.build_config(recurring_window_enabled=True))
        assert report.auto_count == 1, report.auto_pairs

    def test_the_cli_flag_reaches_the_measurement(self, dao, tmp_path):
        self._make_recurring(dao, self._weekly_pair(dao))
        target = tmp_path / "recurring.json"
        assert med.main(["--recurring-window", "--report-json", str(target)]) == 0
        payload = json.loads(target.read_text())
        assert payload["config"]["recurring_window_enabled"] is True
        assert payload["counts"]["auto_pairs"] == 1

    def test_no_recurring_window_is_the_default(self, dao, tmp_path):
        self._make_recurring(dao, self._weekly_pair(dao))
        target = tmp_path / "default.json"
        assert med.main(["--report-json", str(target)]) == 0
        payload = json.loads(target.read_text())
        assert payload["config"]["recurring_window_enabled"] is False
        assert payload["counts"]["auto_pairs"] == 0
