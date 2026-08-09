"""Unit tests for the events.crawl_target DAO surface: the in-memory fake's
constraint enforcement (tests/rds_fake.py) and VenueRepository's pass-through
to it. CLAUDE.md flags this file has twice let a real Postgres defect
through by modeling only the happy path — these tests exist so the NOT NULL
and CHECK constraints migration 0030_crawl_target declares are enforced
here too, not just documented.
"""
from __future__ import annotations

import pytest

from app.dao.venue_repository import VenueRepository
from tests.rds_fake import InMemoryRdsVenueStore


def _dao():
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def test_insert_requires_kind():
    dao = _dao()
    with pytest.raises(ValueError):
        dao.upsert_crawl_target("nokinds", {"cron": "0 22 * * *"})


def test_insert_requires_cron():
    dao = _dao()
    with pytest.raises(ValueError):
        dao.upsert_crawl_target("nocron", {"kind": "venue"})


def test_kind_must_be_venue_or_promoter():
    dao = _dao()
    with pytest.raises(ValueError):
        dao.upsert_crawl_target("badkind", {"kind": "sponsor", "cron": "0 22 * * *"})


def test_kind_check_also_enforced_on_update():
    dao = _dao()
    dao.upsert_crawl_target("goodkind", {"kind": "venue", "cron": "0 22 * * *"})
    with pytest.raises(ValueError):
        dao.update_crawl_target("goodkind", {"kind": "not_a_real_kind"})


def test_upsert_is_create_only_and_refuses_a_partial_write_against_an_existing_row():
    """The production incident (2026-08-09): a partial `upsert_crawl_target`
    call against a row that already exists reaches a real Postgres
    NotNullViolation on `kind`/`cron`, because Postgres validates NOT NULL on
    the INSERT attempt itself, before ON CONFLICT ever runs. `upsert_
    crawl_target` is now CREATE-only and refuses this here too, in Python,
    rather than letting a real database be the first place it's caught."""
    dao = _dao()
    dao.upsert_crawl_target("existing", {"kind": "venue", "cron": "0 22 * * *"})
    with pytest.raises(ValueError):
        dao.upsert_crawl_target("existing", {"enabled": False})


def test_a_valid_insert_gets_real_column_defaults():
    dao = _dao()
    row = dao.upsert_crawl_target("defaults", {"kind": "venue", "cron": "0 22 * * *"})
    assert row["enabled"] is True
    assert row["timezone"] == "America/Recife"
    assert row["crawl_reels"] is False
    assert row["classify_images"] is True
    assert row["results_limit"] is None
    assert row["seed_results_limit"] is None
    assert row["reels_results_limit"] is None
    assert row["reels_seed_results_limit"] is None
    assert row["cursor_posts_at"] is None
    assert row["cursor_reels_at"] is None
    assert row["last_run_results"] == 0
    assert row["consecutive_failures"] == 0


def test_update_is_partial_and_leaves_other_columns_untouched():
    dao = _dao()
    dao.upsert_crawl_target("partial", {"kind": "venue", "cron": "0 22 * * *", "notes": "keep me"})
    row = dao.update_crawl_target("partial", {"enabled": False})
    assert row["enabled"] is False
    assert row["notes"] == "keep me"
    assert row["cron"] == "0 22 * * *"


def test_update_on_a_missing_handle_is_a_safe_no_op():
    dao = _dao()
    assert dao.update_crawl_target("ghost", {"enabled": False}) is None


def test_update_with_no_fields_returns_the_row_unchanged():
    dao = _dao()
    dao.upsert_crawl_target("asis", {"kind": "venue", "cron": "0 22 * * *"})
    before = dao.get_crawl_target("asis")
    after = dao.update_crawl_target("asis", {})
    assert after["kind"] == before["kind"]
    assert after["cron"] == before["cron"]


def test_get_list_and_delete_round_trip():
    dao = _dao()
    dao.upsert_crawl_target("a", {"kind": "venue", "cron": "0 22 * * *"})
    dao.upsert_crawl_target("b", {"kind": "promoter", "cron": "0 6 1 * *"})

    assert dao.get_crawl_target("a")["kind"] == "venue"
    assert {r["handle"] for r in dao.list_crawl_targets()} == {"a", "b"}
    assert {r["handle"] for r in dao.list_crawl_targets(kind="promoter")} == {"b"}

    assert dao.delete_crawl_target("a") is True
    assert dao.get_crawl_target("a") is None
    assert dao.delete_crawl_target("a") is False  # idempotent: already gone
