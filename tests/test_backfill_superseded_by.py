"""Unit tests for scripts/backfill_superseded_by.py.

See plans/260814_record-what-superseded-a-row.md §C's Test Plan.

`tests/bdd/enrichment/record-what-superseded-a-row.feature` covers the same
behavior end-to-end through the real `run_backfill` against the in-memory RDS
fake; this file adds the same predicate coverage at a more granular level —
every disposition, the never-cross-posts guarantee, and idempotency across
two `--apply` runs — plus a bare argument-parsing check for `main`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.dao.venue_repository import VenueRepository
from app.models.venue import Venue
from app.services.event_reconciliation import STATUS_SUPERSEDED, new_event_id
from scripts.backfill_superseded_by import (
    DISPOSITION_AMBIGUOUS,
    DISPOSITION_LINKED,
    DISPOSITION_NO_CANDIDATE,
    DISPOSITION_NO_SOURCE,
    DISPOSITION_WOULD_LINK,
    main,
    run_backfill,
)
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
_VENUE_ID = "bf_venue"


def _dao() -> VenueRepository:
    dao = VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())
    dao.upsert_venue(Venue(
        venue_id=_VENUE_ID, venue_name="Backfill Test Venue",
        venue_lat=-8.05, venue_lng=-34.88, venue_address="",
    ))
    return dao


def _insert(
    dao, title, *, starts_at, status="pending_review", handle="h1", shortcode="s1",
) -> str:
    event_id = new_event_id()
    dao.insert_event({
        "event_id": event_id, "venue_id": _VENUE_ID, "starts_at": starts_at,
        "title": title, "post_type": "event", "lineup": [], "status": status,
        "source_kind": "venue_post", "source_handle": handle, "source_shortcode": shortcode,
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    })
    return event_id


def _decision_for(report, event_id):
    return next(d for d in report.rows if d.event_id == event_id)


class TestLinkedDisposition:
    def test_apply_links_the_unambiguous_orphan(self):
        dao = _dao()
        orphan = _insert(dao, "Secret Club", starts_at=None, status=STATUS_SUPERSEDED)
        successor = _insert(dao, "Secret Club", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc))

        report = run_backfill(dao, apply=True)

        assert report.selected == 1
        decision = _decision_for(report, orphan)
        assert decision.action == DISPOSITION_LINKED
        assert decision.successor_id == successor
        row = dao.get_event(orphan)
        assert row.get("superseded_by") == successor, row

    def test_dry_run_never_writes_but_says_it_would_link(self):
        dao = _dao()
        orphan = _insert(dao, "Secret Club", starts_at=None, status=STATUS_SUPERSEDED)
        _insert(dao, "Secret Club", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc))

        report = run_backfill(dao, apply=False)

        decision = _decision_for(report, orphan)
        assert decision.action == DISPOSITION_WOULD_LINK
        row = dao.get_event(orphan)
        assert row.get("superseded_by") is None, row


class TestAmbiguousAndNoCandidate:
    def test_two_same_titled_live_siblings_is_ambiguous_and_never_written(self):
        dao = _dao()
        orphan = _insert(dao, "Oficina De Sorvete", starts_at=None, status=STATUS_SUPERSEDED)
        _insert(dao, "Oficina De Sorvete", starts_at=datetime(2026, 7, 8, tzinfo=timezone.utc))
        _insert(dao, "oficina de sorvete", starts_at=datetime(2026, 7, 10, tzinfo=timezone.utc))

        report = run_backfill(dao, apply=True)

        decision = _decision_for(report, orphan)
        assert decision.action == DISPOSITION_AMBIGUOUS
        assert set(decision.candidate_ids) == set(
            r["event_id"] for r in dao.list_events_by_source("h1", "s1")
            if r["event_id"] != orphan
        )
        row = dao.get_event(orphan)
        assert row.get("superseded_by") is None, row

    def test_no_matching_title_is_no_candidate_and_never_written(self):
        dao = _dao()
        orphan = _insert(dao, "Old Night", starts_at=None, status=STATUS_SUPERSEDED)
        _insert(dao, "Unrelated Night", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc))

        report = run_backfill(dao, apply=True)

        decision = _decision_for(report, orphan)
        assert decision.action == DISPOSITION_NO_CANDIDATE
        row = dao.get_event(orphan)
        assert row.get("superseded_by") is None, row

    def test_a_superseded_only_candidate_pool_is_also_no_candidate(self):
        """A candidate that is ITSELF superseded must never count — only
        currently-LIVE siblings are eligible successors."""
        dao = _dao()
        orphan = _insert(dao, "Old Night", starts_at=None, status=STATUS_SUPERSEDED)
        _insert(dao, "Old Night", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc), status=STATUS_SUPERSEDED)

        report = run_backfill(dao, apply=True)

        decision = _decision_for(report, orphan)
        assert decision.action == DISPOSITION_NO_CANDIDATE


class TestNeverCrossesPosts:
    def test_a_same_titled_event_from_a_different_post_is_never_a_candidate(self):
        dao = _dao()
        orphan = _insert(dao, "Shared Title", starts_at=None, status=STATUS_SUPERSEDED, handle="h1", shortcode="s1")
        _insert(
            dao, "Shared Title", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            handle="h2", shortcode="s2",
        )

        report = run_backfill(dao, apply=True)

        decision = _decision_for(report, orphan)
        assert decision.action == DISPOSITION_NO_CANDIDATE
        assert decision.candidate_ids == []
        row = dao.get_event(orphan)
        assert row.get("superseded_by") is None, row

    def test_no_source_row_is_reported_and_never_written(self):
        dao = _dao()
        orphan = _insert(dao, "No Source", starts_at=None, status=STATUS_SUPERSEDED)
        dao.update_event(orphan, {"source_handle": None, "source_shortcode": None})

        report = run_backfill(dao, apply=True)

        decision = _decision_for(report, orphan)
        assert decision.action == DISPOSITION_NO_SOURCE


class TestSelectionAndIdempotency:
    def test_a_row_that_already_has_superseded_by_is_never_reselected(self):
        dao = _dao()
        successor = _insert(dao, "Already Linked", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc))
        already_linked = _insert(dao, "Already Linked", starts_at=None, status=STATUS_SUPERSEDED)
        dao.update_event(already_linked, {"superseded_by": successor})

        report = run_backfill(dao, apply=True)

        assert report.selected == 0
        assert report.rows == []

    def test_a_live_row_is_never_selected(self):
        dao = _dao()
        _insert(dao, "Still Live", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc))

        report = run_backfill(dao, apply=True)

        assert report.selected == 0

    def test_a_second_apply_run_changes_nothing(self):
        dao = _dao()
        orphan = _insert(dao, "Secret Club", starts_at=None, status=STATUS_SUPERSEDED)
        successor = _insert(dao, "Secret Club", starts_at=datetime(2026, 9, 5, tzinfo=timezone.utc))

        first = run_backfill(dao, apply=True)
        assert _decision_for(first, orphan).action == DISPOSITION_LINKED

        second = run_backfill(dao, apply=True)

        # The now-linked row is excluded from selection entirely -- nothing
        # left to reconsider, let alone change.
        assert second.selected == 0
        row = dao.get_event(orphan)
        assert row.get("superseded_by") == successor, row

    def test_an_ambiguous_orphan_is_reevaluated_identically_on_a_second_run(self):
        """Idempotency for the case that stays UNRESOLVED, not only the
        case that gets linked: an ambiguous orphan is not excluded from
        selection (superseded_by is still NULL), so it must be re-decided
        the same way every time, never drift toward a guess."""
        dao = _dao()
        orphan = _insert(dao, "Oficina De Sorvete", starts_at=None, status=STATUS_SUPERSEDED)
        _insert(dao, "Oficina De Sorvete", starts_at=datetime(2026, 7, 8, tzinfo=timezone.utc))
        _insert(dao, "Oficina De Sorvete", starts_at=datetime(2026, 7, 10, tzinfo=timezone.utc))

        first = run_backfill(dao, apply=True)
        second = run_backfill(dao, apply=True)

        assert _decision_for(first, orphan).action == DISPOSITION_AMBIGUOUS
        assert _decision_for(second, orphan).action == DISPOSITION_AMBIGUOUS
        row = dao.get_event(orphan)
        assert row.get("superseded_by") is None, row

    def test_finding_nothing_to_do_is_not_a_failure(self):
        dao = _dao()
        report = run_backfill(dao, apply=True)
        assert report.selected == 0
        assert report.rows == []
        assert report.by_disposition == {}


class TestMainArgParsing:
    def test_apply_flag_defaults_to_false(self, monkeypatch):
        captured = {}

        def fake_run_backfill(venue_dao, *, apply):
            captured["apply"] = apply
            from scripts.backfill_superseded_by import Report

            return Report(applied=apply)

        monkeypatch.setattr("scripts.backfill_superseded_by.run_backfill", fake_run_backfill)
        monkeypatch.setattr(
            "scripts.backfill_superseded_by.VenueRepository",
            lambda *a, **k: object(),
        )
        monkeypatch.setattr(
            "scripts.backfill_superseded_by.RdsVenueStore",
            lambda *a, **k: object(),
        )

        assert main([]) == 0
        assert captured["apply"] is False

        assert main(["--apply"]) == 0
        assert captured["apply"] is True
