"""Unit tests for plans/260807_review-queue-completeness-and-venue-names.md.

Two defects, two things to prove:

1. `VenueRepository.list_events_awaiting_decision` (renamed from
   `list_events_pending_location`) must return the union of
   `status='pending_review'` and `source_kind='promoter_post' AND
   location_resolution IS NULL AND status NOT IN ('rejected', 'superseded')`
   — proven here across the full (source_kind, status, location_resolution)
   matrix so a future narrowing (like the one this plan fixes) fails loudly.

2. `_EVENT_SELECT`'s venue-name join must be LEFT, not INNER — proven both
   textually (the SQL RdsVenueStore actually sends) and behaviourally (the
   in-memory fake, which mirrors that join, still returns a venue-less or
   dangling-reference event rather than dropping it).

Exercised via tests.rds_fake.InMemoryRdsVenueStore — the proven behaviour
contract for the SQLAlchemy RdsVenueStore (see tests/test_rds_venue_store.py's
own docstring; there is no local Postgres in CI/dev, see tests/README.md).
"""
from __future__ import annotations

import pytest

from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.models import Venue
from tests.rds_fake import InMemoryRdsVenueStore


def _dao() -> VenueRepository:
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def _event(event_id: str, *, source_kind: str, status: str, location_resolution=None,
           venue_id=None) -> dict:
    return {
        "event_id": event_id, "venue_id": venue_id, "source_kind": source_kind,
        "source_handle": "h", "source_shortcode": event_id, "status": status,
        "location_resolution": location_resolution, "starts_at": None,
    }


# ── Defect 1: the predicate matrix ───────────────────────────────────────────
# (source_kind, status, location_resolution) -> expected membership in the
# review queue. Every row documents WHY, so a future edit that "simplifies"
# the predicate has to argue with a specific case, not just a diff.
MATRIX = [
    # venue-post events never touch location_resolution (constant
    # attribution) — only `status` can ever put one in the queue.
    ("venue_post", "pending_review", None, True, "pending_review is the whole queue's main clause"),
    ("venue_post", "confirmed", None, False, "confirmed and no promoter-link clause applies"),
    ("venue_post", "rejected", None, False, "operator finished with it"),
    ("venue_post", "superseded", None, False, "reconciliation finished with it"),
    # promoter-post events: status='pending_review' alone is already
    # sufficient, REGARDLESS of location_resolution — an unresolved
    # (below-floor) promoter event is `pending_review` too (every persisted
    # event is), so it now surfaces instead of being silently hidden.
    ("promoter_post", "pending_review", None, True, "queued for a venue AND unconfirmed"),
    ("promoter_post", "pending_review", "auto", True, "auto-linked but not yet confirmed"),
    ("promoter_post", "pending_review", "manual", True, "manually linked but not yet confirmed"),
    ("promoter_post", "pending_review", "unresolved", True, "no venue found, but still unconfirmed"),
    # The second clause's reason to exist: confirmed data, unresolved venue.
    ("promoter_post", "confirmed", None, True, "confirmed data, venue still undecided (clause 2, non-redundant)"),
    ("promoter_post", "confirmed", "auto", False, "confirmed AND the venue is settled"),
    ("promoter_post", "confirmed", "manual", False, "confirmed AND the venue is settled"),
    ("promoter_post", "confirmed", "unresolved", False, "confirmed AND the venue question is settled (no match)"),
    # The clause-2 guard: rejected/superseded must not resurrect via the
    # "location still NULL" branch just because nobody ever linked it.
    ("promoter_post", "rejected", None, False, "operator rejected it even though it was never linked"),
    ("promoter_post", "rejected", "auto", False, "operator rejected it"),
    ("promoter_post", "superseded", None, False, "reconciliation superseded it even though it was never linked"),
    ("promoter_post", "superseded", "manual", False, "reconciliation superseded it"),
]


class TestReviewQueuePredicateMatrix:
    @pytest.mark.parametrize(
        "source_kind,status,location_resolution,expected,reason", MATRIX,
        ids=[f"{sk}/{st}/{lr}" for sk, st, lr, _, _ in MATRIX],
    )
    def test_membership(self, source_kind, status, location_resolution, expected, reason):
        dao = _dao()
        dao.insert_event(_event(
            "evt_1", source_kind=source_kind, status=status,
            location_resolution=location_resolution,
        ))
        ids = [r["event_id"] for r in dao.list_events_awaiting_decision()]
        if expected:
            assert "evt_1" in ids, reason
        else:
            assert "evt_1" not in ids, reason

    def test_the_matrix_covers_both_directions(self):
        """Guard against a matrix that accidentally asserts only one outcome
        (the "enumerated assertions create a false green" trap) — there must
        be at least one included AND one excluded case."""
        included = [row for row in MATRIX if row[3]]
        excluded = [row for row in MATRIX if not row[3]]
        assert included and excluded

    def test_oldest_first_ordering_spans_both_source_kinds(self):
        dao = _dao()
        dao.insert_event({
            **_event("evt_new", source_kind="promoter_post", status="pending_review"),
            "first_seen_at": "2026-08-01T00:00:00+00:00",
        })
        dao.insert_event({
            **_event("evt_old", source_kind="venue_post", status="pending_review"),
            "first_seen_at": "2026-07-01T00:00:00+00:00",
        })
        ids = [r["event_id"] for r in dao.list_events_awaiting_decision()]
        assert ids == ["evt_old", "evt_new"]


# ── Defect 2: LEFT JOIN, not INNER ───────────────────────────────────────────
class TestVenueNameLeftJoin:
    def test_event_select_joins_venues_venue_with_left_not_inner(self):
        """The likely accidental edit (per the plan) is swapping LEFT for a
        plain/INNER join — assert the real SQL text directly rather than only
        the fake's behavior, since the fake's join semantics are hand-mirrored
        and would not, by themselves, catch a mistake in the real SQL."""
        sql = RdsVenueStore._EVENT_SELECT
        assert "LEFT JOIN venues.venue" in sql
        assert "INNER JOIN" not in sql
        # Exactly one join against venues.venue — this is not a self-check
        # that a LEFT JOIN happens to also contain the substring "JOIN".
        assert sql.count("JOIN venues.venue") == 1
        assert "venue_name" in sql

    def test_unresolved_promoter_event_has_no_venue_and_null_name(self):
        dao = _dao()
        dao.insert_event(_event(
            "evt_1", source_kind="promoter_post", status="pending_review", venue_id=None,
        ))
        rows = dao.list_events_awaiting_decision()
        assert len(rows) == 1
        assert rows[0]["venue_id"] is None
        assert rows[0]["venue_name"] is None

    def test_dangling_venue_id_returns_null_name_without_dropping_the_event(self):
        """An INNER join would drop this row entirely; a LEFT join must
        still return it, with venue_name null."""
        dao = _dao()
        dao.insert_event(_event(
            "evt_1", source_kind="venue_post", status="pending_review",
            venue_id="ven_does_not_exist",
        ))
        rows = dao.list_events_awaiting_decision()
        assert len(rows) == 1, "an INNER join would have dropped this row"
        assert rows[0]["venue_name"] is None

    def test_linked_event_carries_its_venues_name(self):
        dao = _dao()
        dao.upsert_venue(Venue(
            venue_id="ven_1", venue_name="Métropole Recife",
            venue_address="addr", venue_lat=-8.05, venue_lng=-34.88,
        ))
        dao.insert_event(_event(
            "evt_1", source_kind="venue_post", status="pending_review", venue_id="ven_1",
        ))
        rows = dao.list_events_awaiting_decision()
        assert rows[0]["venue_name"] == "Métropole Recife"

    def test_venue_name_present_on_get_event(self):
        dao = _dao()
        dao.upsert_venue(Venue(
            venue_id="ven_1", venue_name="Métropole Recife",
            venue_address="addr", venue_lat=-8.05, venue_lng=-34.88,
        ))
        dao.insert_event(_event(
            "evt_1", source_kind="venue_post", status="confirmed", venue_id="ven_1",
        ))
        row = dao.get_event("evt_1")
        assert row["venue_name"] == "Métropole Recife"

    def test_venue_name_present_on_list_events(self):
        dao = _dao()
        dao.upsert_venue(Venue(
            venue_id="ven_1", venue_name="Métropole Recife",
            venue_address="addr", venue_lat=-8.05, venue_lng=-34.88,
        ))
        dao.insert_event(_event(
            "evt_1", source_kind="venue_post", status="confirmed", venue_id="ven_1",
        ))
        rows = dao.list_events(venue_id="ven_1")
        assert len(rows) == 1
        assert rows[0]["venue_name"] == "Métropole Recife"

    def test_venue_name_null_when_venue_id_is_null_on_list_events(self):
        dao = _dao()
        dao.insert_event(_event(
            "evt_1", source_kind="promoter_post", status="pending_review", venue_id=None,
        ))
        rows = dao.list_events()
        assert rows[0]["venue_name"] is None
