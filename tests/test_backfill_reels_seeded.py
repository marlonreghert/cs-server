"""Unit tests for scripts/backfill_reels_seeded.py.

See plans/260814_seeded-state-and-config-validation.md §B.

Never run against production (the execute-feature workflow that produced
this script forbids it) — `TestMatchesThePlansEvidenceTable` proves the
classification is correct by reconstructing the plan's own Evidence table
(measured in production, 2026-08-13) as SYNTHETIC rows and asserting
`classify_target` reaches exactly the outcome the plan names for each, not
by ever touching a real database.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.dao.venue_repository import VenueRepository
from scripts.backfill_reels_seeded import (
    DISPOSITION_AMBIGUOUS,
    DISPOSITION_LEAVE_UNSEEDED,
    DISPOSITION_MARK_SEEDED,
    DISPOSITION_NOT_CANDIDATE,
    Report,
    check_balance,
    classify_target,
    run_backfill,
)
from tests.rds_fake import InMemoryRdsVenueStore

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
RAN_AT = datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc)


def _dao() -> VenueRepository:
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def _target(handle: str, **overrides) -> dict:
    base = {
        "handle": handle, "kind": "venue", "crawl_reels": True,
        "cursor_reels_at": None, "reels_seeded_at": None,
        "last_run_reels_fetched": None, "last_run_at": None, "last_failure_kind": None,
    }
    base.update(overrides)
    return base


# ── classify_target: the pure per-row verdict ────────────────────────────────
class TestClassifyTarget:
    def test_reels_disabled_is_not_a_candidate(self):
        d = classify_target(_target("h", crawl_reels=False))
        assert d.disposition == DISPOSITION_NOT_CANDIDATE

    def test_a_cursor_already_set_is_not_a_candidate(self):
        """Already correctly seeded even under the OLD gate -- nothing for
        this script to do."""
        d = classify_target(_target("h", cursor_reels_at=RAN_AT))
        assert d.disposition == DISPOSITION_NOT_CANDIDATE

    def test_an_already_marked_target_is_not_a_candidate(self):
        """Idempotency: a target this script (or a real run) already
        marked is left alone on a re-run."""
        d = classify_target(_target("h", reels_seeded_at=RAN_AT))
        assert d.disposition == DISPOSITION_NOT_CANDIDATE

    def test_a_trustworthy_reels_answer_on_record_marks_seeded(self):
        d = classify_target(_target("h", last_run_reels_fetched=0, last_run_at=RAN_AT))
        assert d.disposition == DISPOSITION_MARK_SEEDED

    def test_a_nonzero_trustworthy_reels_answer_also_marks_seeded(self):
        """`last_run_reels_fetched` need not be zero -- ANY recorded value
        (even a non-empty one whose items all deduped against posts, or
        whose pinned-and-dropped items left nothing new) proves the stream
        reached a trustworthy conclusion. A target with a genuinely
        non-empty NEW result would also have `cursor_reels_at` set, which
        already excludes it at the NOT_CANDIDATE gate -- this covers the
        residual case where it does not."""
        d = classify_target(_target("h", last_run_reels_fetched=3, last_run_at=RAN_AT))
        assert d.disposition == DISPOSITION_MARK_SEEDED

    def test_never_run_leaves_it_unseeded(self):
        d = classify_target(_target("h"))  # last_run_at defaults to None
        assert d.disposition == DISPOSITION_LEAVE_UNSEEDED

    def test_a_recorded_failure_leaves_it_unseeded(self):
        d = classify_target(
            _target("h", last_run_at=RAN_AT, last_failure_kind="blocked"),
        )
        assert d.disposition == DISPOSITION_LEAVE_UNSEEDED

    def test_has_run_no_failure_no_reels_answer_is_ambiguous(self):
        """The genuinely confusing shape: ran before, nothing on record
        says why reels was never answered. Reported, never guessed."""
        d = classify_target(_target("h", last_run_at=RAN_AT))
        assert d.disposition == DISPOSITION_AMBIGUOUS

    def test_every_decision_carries_a_human_readable_reason(self):
        for target in (
            _target("h", crawl_reels=False),
            _target("h", cursor_reels_at=RAN_AT),
            _target("h", reels_seeded_at=RAN_AT),
            _target("h", last_run_reels_fetched=0, last_run_at=RAN_AT),
            _target("h"),
            _target("h", last_run_at=RAN_AT, last_failure_kind="blocked"),
            _target("h", last_run_at=RAN_AT),
        ):
            d = classify_target(target)
            assert d.reason and isinstance(d.reason, str)


# ── the plan's own Evidence table, reconstructed as synthetic rows ─────────
class TestMatchesThePlansEvidenceTable:
    """plans/260814_seeded-state-and-config-validation.md's Evidence
    section, measured in production 2026-08-13:

        burburinhobar           completed, 0 reels  -> mark seeded
        downtownrecife          dormant, 0 reels    -> mark seeded
        armazem14.recifeantigo  BLOCKED              -> leave unseeded
        champagne.clubrecife    never ran            -> leave unseeded

    Each row below is built from ONLY the facts the plan's table and the
    coordinator's own one-line summary give for that target — never
    invented, never read from a real database."""

    def test_burburinhobar_completed_empty_marks_seeded(self):
        row = _target(
            "burburinhobar", last_run_reels_fetched=0, last_run_at=RAN_AT,
            last_failure_kind=None,
        )
        d = classify_target(row)
        assert d.disposition == DISPOSITION_MARK_SEEDED, d.reason

    def test_downtownrecife_dormant_empty_marks_seeded(self):
        row = _target(
            "downtownrecife", last_run_reels_fetched=0, last_run_at=RAN_AT,
            last_failure_kind=None, posts_dormant=True,
        )
        d = classify_target(row)
        assert d.disposition == DISPOSITION_MARK_SEEDED, d.reason

    def test_armazem14_blocked_leaves_unseeded(self):
        row = _target(
            "armazem14.recifeantigo", last_run_reels_fetched=None, last_run_at=RAN_AT,
            last_failure_kind="blocked",
        )
        d = classify_target(row)
        assert d.disposition == DISPOSITION_LEAVE_UNSEEDED, d.reason

    def test_champagne_never_ran_leaves_unseeded(self):
        row = _target(
            "champagne.clubrecife", last_run_reels_fetched=None, last_run_at=None,
            last_failure_kind=None,
        )
        d = classify_target(row)
        assert d.disposition == DISPOSITION_LEAVE_UNSEEDED, d.reason

    def test_the_full_four_target_population_end_to_end(self):
        """The same four, run through `run_backfill` end to end (not just
        the pure predicate) against the in-memory RDS fake, dry-run —
        confirms the report names exactly the right two as marked and
        leaves the other two out of it."""
        dao = _dao()
        dao.upsert_crawl_target("burburinhobar", {
            "kind": "venue", "cron": "0 3 * * 5,6,0", "crawl_reels": True,
        })
        dao.update_crawl_target("burburinhobar", {
            "last_run_reels_fetched": 0, "last_run_at": RAN_AT,
        })
        dao.upsert_crawl_target("downtownrecife", {
            "kind": "venue", "cron": "0 3 * * 5,6,0", "crawl_reels": True,
        })
        dao.update_crawl_target("downtownrecife", {
            "last_run_reels_fetched": 0, "last_run_at": RAN_AT, "posts_dormant": True,
        })
        dao.upsert_crawl_target("armazem14.recifeantigo", {
            "kind": "venue", "cron": "0 3 * * 5,6,0", "crawl_reels": True,
        })
        dao.update_crawl_target("armazem14.recifeantigo", {
            "last_run_at": RAN_AT, "last_failure_kind": "blocked",
        })
        dao.upsert_crawl_target("champagne.clubrecife", {
            "kind": "venue", "cron": "0 3 * * 5,6,0", "crawl_reels": True,
        })

        report = run_backfill(dao, apply=False, now_provider=lambda: NOW)

        assert set(report.marked_handles) == {"burburinhobar", "downtownrecife"}
        assert report.ambiguous_handles == []
        untouched = {"armazem14.recifeantigo", "champagne.clubrecife"}
        marked_or_ambiguous = set(report.marked_handles) | set(report.ambiguous_handles)
        assert untouched.isdisjoint(marked_or_ambiguous)
        # Dry run: nothing actually written.
        for handle in ("burburinhobar", "downtownrecife"):
            assert dao.get_crawl_target(handle)["reels_seeded_at"] is None


# ── run_backfill: dry-run vs apply, idempotency, balance ────────────────────
class TestRunBackfill:
    def test_dry_run_never_writes(self):
        dao = _dao()
        dao.upsert_crawl_target("dryrun1", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": True})
        dao.update_crawl_target("dryrun1", {"last_run_reels_fetched": 0, "last_run_at": RAN_AT})

        report = run_backfill(dao, apply=False, now_provider=lambda: NOW)

        assert report.marked_handles == ["dryrun1"]
        assert dao.get_crawl_target("dryrun1")["reels_seeded_at"] is None

    def test_apply_writes_reels_seeded_at(self):
        dao = _dao()
        dao.upsert_crawl_target("applyme", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": True})
        dao.update_crawl_target("applyme", {"last_run_reels_fetched": 0, "last_run_at": RAN_AT})

        report = run_backfill(dao, apply=True, now_provider=lambda: NOW)

        assert report.marked_handles == ["applyme"]
        assert dao.get_crawl_target("applyme")["reels_seeded_at"] == NOW

    def test_apply_never_touches_cursor_reels_at(self):
        """§A's own separation of concerns: this script writes ONLY
        `reels_seeded_at` — `cursor_reels_at` keeps its own, unchanged
        meaning and must stay exactly as it was (null, for a genuinely
        empty seed)."""
        dao = _dao()
        dao.upsert_crawl_target("cursoruntouched", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": True})
        dao.update_crawl_target("cursoruntouched", {"last_run_reels_fetched": 0, "last_run_at": RAN_AT})

        run_backfill(dao, apply=True, now_provider=lambda: NOW)

        assert dao.get_crawl_target("cursoruntouched")["cursor_reels_at"] is None

    def test_apply_is_idempotent(self):
        dao = _dao()
        dao.upsert_crawl_target("idempotent1", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": True})
        dao.update_crawl_target("idempotent1", {"last_run_reels_fetched": 0, "last_run_at": RAN_AT})

        first = run_backfill(dao, apply=True, now_provider=lambda: NOW)
        assert first.marked_handles == ["idempotent1"]

        second = run_backfill(dao, apply=True, now_provider=lambda: NOW)
        assert second.marked_handles == []
        assert second.dispositions[DISPOSITION_NOT_CANDIDATE] == 1

    def test_a_leave_unseeded_target_is_never_written_even_with_apply(self):
        dao = _dao()
        dao.upsert_crawl_target("stillblocked", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": True})
        dao.update_crawl_target("stillblocked", {"last_run_at": RAN_AT, "last_failure_kind": "blocked"})

        run_backfill(dao, apply=True, now_provider=lambda: NOW)

        assert dao.get_crawl_target("stillblocked")["reels_seeded_at"] is None

    def test_an_ambiguous_target_is_never_written_even_with_apply(self):
        dao = _dao()
        dao.upsert_crawl_target("confusing", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": True})
        dao.update_crawl_target("confusing", {"last_run_at": RAN_AT})  # ran, no failure, no reels answer

        report = run_backfill(dao, apply=True, now_provider=lambda: NOW)

        assert report.ambiguous_handles == ["confusing"]
        assert dao.get_crawl_target("confusing")["reels_seeded_at"] is None

    def test_unaffected_targets_are_not_candidates_and_untouched(self):
        dao = _dao()
        dao.upsert_crawl_target("reelsoff", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": False})
        dao.upsert_crawl_target("alreadygood", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": True})
        dao.update_crawl_target("alreadygood", {"cursor_reels_at": RAN_AT})

        report = run_backfill(dao, apply=True, now_provider=lambda: NOW)

        assert report.marked_handles == []
        assert report.ambiguous_handles == []
        assert report.dispositions[DISPOSITION_NOT_CANDIDATE] == 2

    def test_report_is_balanced_across_every_target(self):
        dao = _dao()
        for i in range(5):
            dao.upsert_crawl_target(f"h{i}", {"kind": "venue", "cron": "0 3 * * *", "crawl_reels": i % 2 == 0})
        report = run_backfill(dao, apply=False, now_provider=lambda: NOW)
        assert report.selected == 5
        assert sum(report.dispositions.values()) == 5
        assert report.balanced is True

    def test_check_balance_raises_on_a_hand_built_mismatch(self):
        report = Report(selected=3)
        report.dispositions[DISPOSITION_NOT_CANDIDATE] = 2  # short by one on purpose
        import pytest

        with pytest.raises(AssertionError):
            check_balance(report)
