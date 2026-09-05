"""Parity guard for the events serving projection's selection query
(SQL <-> app.services.event_projection_selection.is_selectable()).

plans/260905_events-serving-projection.md's `list_events_for_projection`
docstring is explicit that the two are "kept in lockstep by hand" — this test
is what makes that checked rather than assumed, the same way
test_eligibility_serving_view_parity.py already does for
venue_eligibility.evaluate() <-> serving.eligible_venue. It runs one fixture
table through BOTH `is_selectable()` directly AND `store.
list_events_for_projection()`, for the fake store (always) AND the real SQL
store (only when RDS_TEST_URL points at a migrated scratch Postgres — there
is no local Postgres in CI). Offline this pins the fake mirror to
is_selectable(); the real-store run is what actually validates the SQL.

Fixtures deliberately concentrate on the NEW recurring source-freshness bound
(plans/260905's phantom-recurring-event fix) — the rest of is_selectable's
branches are already exhaustively covered, predicate-only, by
test_event_projection_selection.py.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models import Venue
from app.services.event_projection_selection import is_selectable
from tests.rds_fake import InMemoryRdsVenueStore

_LAT, _LNG = -8.05, -34.88  # inside Recife's default 40km circle
NOW = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)


def _store_kinds():
    kinds = ["fake"]
    if os.environ.get("RDS_TEST_URL"):
        kinds.append("rds")
    return kinds


@pytest.fixture(params=_store_kinds())
def store(request):
    if request.param == "fake":
        return InMemoryRdsVenueStore()
    from app.dao.rds_venue_store import RdsVenueStore
    return RdsVenueStore(os.environ["RDS_TEST_URL"])


def _vid() -> str:
    return f"evp_{uuid.uuid4().hex[:12]}"


def _seed_venue(store) -> str:
    vid = _vid()
    store.upsert_venue(
        Venue(venue_id=vid, venue_name=f"Venue {vid}", venue_lat=_LAT, venue_lng=_LNG)
    )
    return vid


# (label, is_recurring, starts_at, last_seen_at, status, superseded, expected)
# `superseded` is a bool (a genuine other event is created and referenced —
# see _insert helper) rather than a raw value, since superseded_by must name
# a real row against the real store. `last_seen_at=None` means "not
# threaded through insert_event at all" (both stores then default it to
# the insertion instant — fresh), never the NULL-source-rows edge case
# (fake-only; see test_fake_treats_an_event_with_no_source_rows_as_
# maximally_stale below — real Postgres's post_item_source.last_seen_at is
# NOT NULL, migration 0026, so that fixture cannot be represented for real).
_FIXTURES = [
    ("non_recurring_future", False, NOW + timedelta(hours=1), None, "accepted", False, True),
    ("non_recurring_past_within_grace", False, NOW - timedelta(hours=20), None, "accepted", False, True),
    ("non_recurring_past_beyond_grace", False, NOW - timedelta(days=4), None, "accepted", False, False),
    ("non_recurring_null_starts_at", False, None, None, "accepted", False, True),
    ("non_recurring_rejected", False, NOW + timedelta(hours=1), None, "rejected", False, False),
    ("recurring_fresh_source", True, NOW - timedelta(days=200), NOW - timedelta(days=2), "accepted", False, True),
    ("recurring_stale_source", True, NOW - timedelta(days=200), NOW - timedelta(days=90), "accepted", False, False),
    ("recurring_pending_review_ignores_freshness", True, NOW - timedelta(days=200), NOW - timedelta(days=2), "pending_review", False, False),
    ("recurring_superseded_ignores_freshness", True, NOW - timedelta(days=200), NOW - timedelta(days=2), "accepted", True, False),
]


def _insert(store, vid, *, is_recurring, starts_at, last_seen_at, status, superseded) -> str:
    event_id = uuid.uuid4().hex
    superseded_by = None
    if superseded:
        other_id = uuid.uuid4().hex
        store.insert_event({
            "event_id": other_id, "venue_id": vid, "status": "accepted", "post_type": "event",
            "is_recurring": False, "starts_at": NOW + timedelta(hours=1),
            "source_handle": "parity_handle", "source_shortcode": other_id,
        })
        superseded_by = other_id
    fields = {
        "event_id": event_id, "venue_id": vid, "status": status, "post_type": "event",
        "is_recurring": is_recurring, "starts_at": starts_at, "superseded_by": superseded_by,
        "source_handle": "parity_handle", "source_shortcode": event_id,
        "source_permalink": f"https://instagram.com/p/{event_id}",
    }
    if last_seen_at is not None:
        fields["last_seen_at"] = last_seen_at
    store.insert_event(fields)
    return event_id


@pytest.mark.parametrize(
    "label,is_recurring,starts_at,last_seen_at,status,superseded,expected",
    _FIXTURES, ids=[f[0] for f in _FIXTURES],
)
def test_sql_and_python_predicate_agree(
    store, label, is_recurring, starts_at, last_seen_at, status, superseded, expected,
):
    # Reference: is_selectable, fed the SAME field values through the SAME
    # keyword-only contract every production caller uses (the live setting,
    # never this module's own fallback default).
    reference_row = {
        "post_type": "event", "status": status, "venue_id": "placeholder-vid",
        "superseded_by": "other" if superseded else None,
        "starts_at": starts_at, "is_recurring": is_recurring, "last_seen_at": last_seen_at,
    }
    max_age = timedelta(days=settings.events_recurring_max_source_age_days)
    reference = is_selectable(reference_row, now=NOW, max_recurring_source_age=max_age)
    assert reference is expected, f"fixture {label} mislabeled vs is_selectable()"

    vid = _seed_venue(store)
    event_id = _insert(
        store, vid, is_recurring=is_recurring, starts_at=starts_at,
        last_seen_at=last_seen_at, status=status, superseded=superseded,
    )

    projected_ids = {r["event_id"] for r in store.list_events_for_projection(now=NOW)}
    assert (event_id in projected_ids) is expected, (
        f"{label}: store says selected={event_id in projected_ids}, expected {expected}"
    )


def test_fake_treats_an_event_with_no_source_rows_as_maximally_stale():
    """`insert_event` always attaches exactly one source row (see its own
    docstring on both stores), so an event with genuinely ZERO source rows
    cannot be produced through either store's public API. This gets as
    close as the fake's API allows: an explicit `last_seen_at=None` in
    `fields` (unlike this file's `_insert` helper, which treats None as
    "omit the key, let it default fresh" for fixtures that do not care)
    propagates all the way to the one created source row's own
    `last_seen_at` column, and `_seen_bounds` filters out a None
    last_seen_at exactly like it would filter out a nonexistent row (MAX()
    over zero-or-all-NULL input is NULL either way — see is_selectable's
    own docstring). Fake-store only: real Postgres's
    post_item_source.last_seen_at is NOT NULL (migration 0026), so this
    fixture cannot be represented against the real store at all."""
    store = InMemoryRdsVenueStore()
    vid = _seed_venue(store)
    event_id = uuid.uuid4().hex
    store.insert_event({
        "event_id": event_id, "venue_id": vid, "status": "accepted", "post_type": "event",
        "is_recurring": True, "starts_at": NOW - timedelta(days=200), "last_seen_at": None,
        "source_handle": "parity_handle", "source_shortcode": event_id,
    })

    projected_ids = {r["event_id"] for r in store.list_events_for_projection(now=NOW)}
    assert event_id not in projected_ids
