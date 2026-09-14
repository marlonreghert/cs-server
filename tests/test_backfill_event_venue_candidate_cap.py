"""Unit tests for scripts/backfill_event_venue_candidate_cap.py.

See plans/260913_candidate-cap-and-handle-time-merge.md Part A. Pins the
one-off repair for events written before the cap
(`app.services.event_venue_resolution.DEFAULT_NAME_MATCH_TOP_K`) shipped:
dry-run reports without writing, `--apply` truncates to `top_k`, a second
`--apply` is a no-op, and `venue_id`/`location_resolution`/`review_reason`
are never touched — only `events.event_venue_link_candidate` changes.

Driven against `tests.rds_fake.InMemoryRdsVenueStore` directly through
`app.dao.venue_repository.VenueRepository`, never a real database.
"""
from __future__ import annotations

import json

import scripts.backfill_event_venue_candidate_cap as backfill
from app.dao.venue_repository import VenueRepository
from app.models.venue import Venue
from tests.rds_fake import InMemoryRdsVenueStore


def _dao():
    store = InMemoryRdsVenueStore()
    return VenueRepository(client=None, rds_store=store)


def _seed_event(dao, event_id: str, venue_id: str = "v1") -> None:
    dao.rds_store.upsert_venue(Venue(venue_id=venue_id, venue_name="Some Venue", venue_lat=0.0, venue_lng=0.0))
    dao.insert_event({
        "event_id": event_id, "venue_id": venue_id, "title": "Some Event",
        "post_type": "event", "status": "pending_review",
        "source_kind": "venue_post", "source_handle": "some_handle",
        "source_shortcode": event_id, "location_resolution": None,
        "review_reason": None,
    })


def _seed_candidates(dao, event_id: str, n: int) -> None:
    dao.replace_event_venue_link_candidates(event_id, [
        {"venue_id": f"v{rank}", "rank": rank, "score": round(1.0 - rank * 0.01, 4), "method": "name_match", "evidence": {}}
        for rank in range(1, n + 1)
    ])


class TestMeasure:
    def test_reports_every_event_over_top_k(self):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)
        _seed_event(dao, "evt_small")
        _seed_candidates(dao, "evt_small", 3)

        report = backfill.measure(dao, top_k=20)

        assert report.oversized_count == 1
        assert report.oversized_events[0].event_id == "evt_big"
        assert report.oversized_events[0].current_count == 40
        assert report.oversized_events[0].would_be_count == 20
        assert report.rows_that_would_be_removed == 20

    def test_never_writes_anything(self):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)

        backfill.measure(dao, top_k=20)

        assert len(dao.list_event_venue_link_candidates("evt_big")) == 40

    def test_empty_when_nothing_is_oversized(self):
        dao = _dao()
        _seed_event(dao, "evt_small")
        _seed_candidates(dao, "evt_small", 3)

        report = backfill.measure(dao, top_k=20)
        assert report.oversized_count == 0
        assert report.rows_that_would_be_removed == 0


class TestApply:
    def test_shrinks_the_oversized_event_to_top_k(self):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)

        backfill.apply_cap(dao, top_k=20)

        remaining = dao.list_event_venue_link_candidates("evt_big")
        assert len(remaining) == 20
        assert [c["rank"] for c in remaining] == list(range(1, 21))

    def test_keeps_the_best_scored_candidates(self):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)

        backfill.apply_cap(dao, top_k=5)

        remaining = dao.list_event_venue_link_candidates("evt_big")
        assert [c["venue_id"] for c in remaining] == ["v1", "v2", "v3", "v4", "v5"]

    def test_never_touches_venue_id_location_resolution_or_review_reason(self):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)
        before = dao.get_event("evt_big")

        backfill.apply_cap(dao, top_k=20)

        after = dao.get_event("evt_big")
        assert after["venue_id"] == before["venue_id"]
        assert after["location_resolution"] == before["location_resolution"]
        assert after["review_reason"] == before["review_reason"]

    def test_leaves_an_already_small_event_untouched(self):
        dao = _dao()
        _seed_event(dao, "evt_small")
        _seed_candidates(dao, "evt_small", 3)

        backfill.apply_cap(dao, top_k=20)

        assert len(dao.list_event_venue_link_candidates("evt_small")) == 3

    def test_idempotent_a_second_apply_changes_nothing(self):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)

        backfill.apply_cap(dao, top_k=20)
        after_first = dao.list_event_venue_link_candidates("evt_big")

        backfill.apply_cap(dao, top_k=20)
        after_second = dao.list_event_venue_link_candidates("evt_big")

        assert after_first == after_second


class TestSinceEventId:
    def test_resumes_after_the_given_event_id(self):
        dao = _dao()
        _seed_event(dao, "evt_a")
        _seed_candidates(dao, "evt_a", 40)
        _seed_event(dao, "evt_b")
        _seed_candidates(dao, "evt_b", 40)

        report = backfill.measure(dao, top_k=20, since_event_id="evt_a")

        assert [e.event_id for e in report.oversized_events] == ["evt_b"]


class TestCLI:
    def test_dry_run_writes_a_report_and_touches_nothing(self, tmp_path, monkeypatch):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)
        report_path = tmp_path / "report.json"

        monkeypatch.setattr(backfill, "RdsVenueStore", lambda url: dao.rds_store)
        monkeypatch.setattr(backfill, "VenueRepository", lambda client=None, rds_store=None: rds_store)

        exit_code = backfill.main(["--top-k", "20", "--report-json", str(report_path)])

        assert exit_code == 0
        assert len(dao.list_event_venue_link_candidates("evt_big")) == 40
        payload = json.loads(report_path.read_text())
        assert payload["mode"] == "dry_run"
        assert payload["oversized_count"] == 1

    def test_apply_writes_and_reports_success(self, tmp_path, monkeypatch):
        dao = _dao()
        _seed_event(dao, "evt_big")
        _seed_candidates(dao, "evt_big", 40)
        report_path = tmp_path / "report.json"

        monkeypatch.setattr(backfill, "RdsVenueStore", lambda url: dao.rds_store)
        monkeypatch.setattr(backfill, "VenueRepository", lambda client=None, rds_store=None: rds_store)

        exit_code = backfill.main(["--top-k", "20", "--apply", "--report-json", str(report_path)])

        assert exit_code == 0
        assert len(dao.list_event_venue_link_candidates("evt_big")) == 20
