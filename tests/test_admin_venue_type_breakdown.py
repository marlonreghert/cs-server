"""Unit tests for GET /admin/venue-type-breakdown (app/routers/admin_trigger_router.py).

Pins two things the BDD feature (tests/bdd/api/admin-breakdown-and-addvenue-fold.feature)
covers at the HTTP layer:

- The handler resolves its DAO through `_get_venue_dao_from_container()`, which
  falls back to `redis_venue_dao` when `venue_dao` is absent — the actual shape
  of the production `Container` (app/container.py only ever sets
  `redis_venue_dao`). A regression back to a direct `_container.venue_dao`
  access would AttributeError here exactly as it did in production.
- The per-type/per-Google-type count maps are correct and sorted by descending
  count, and a venue with no BestTime `venue_type` is bucketed as "unknown".
"""
import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Venue
from app.models.vibe_attributes import VibeAttributes

admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")


def _venue(venue_id, venue_type=None):
    return Venue(
        venue_id=venue_id,
        venue_name=f"Venue {venue_id}",
        venue_address="Some Address",
        venue_lat=-8.05,
        venue_lng=-34.88,
        venue_type=venue_type,
    )


class _BreakdownDao:
    def __init__(self, venues, vibe_attrs_by_id=None):
        self._venues = venues
        self._vibe_attrs_by_id = vibe_attrs_by_id or {}

    def list_all_venues(self):
        return self._venues

    def get_vibe_attributes(self, venue_id):
        return self._vibe_attrs_by_id.get(venue_id)


@pytest.fixture(autouse=True)
def _reset_container():
    """Isolate the module-level `_container` across tests in this file."""
    yield
    admin_trigger_router.set_container(None)


# P4: venue_type_breakdown is now a plain `def` (FastAPI threadpool), not a
# coroutine — call it directly rather than awaiting it.
def test_breakdown_counts_and_sorts_descending_via_redis_venue_dao_fallback():
    # Production container shape: only `redis_venue_dao` is set, never
    # `venue_dao`. A regression to the old `_container.venue_dao` access would
    # AttributeError here (SimpleNamespace has no such attribute).
    dao = _BreakdownDao(
        venues=[
            _venue("ven_bar_0", venue_type="BAR"),
            _venue("ven_bar_1", venue_type="BAR"),
            _venue("ven_bar_2", venue_type="BAR"),
            _venue("ven_restaurant_0", venue_type="RESTAURANT"),
        ],
        vibe_attrs_by_id={
            "ven_bar_0": VibeAttributes(venue_id="ven_bar_0", google_primary_type="bar"),
            "ven_bar_1": VibeAttributes(venue_id="ven_bar_1", google_primary_type="bar"),
        },
    )
    admin_trigger_router.set_container(SimpleNamespace(redis_venue_dao=dao))

    response = admin_trigger_router.venue_type_breakdown()

    assert response["total_venues"] == 4
    assert response["with_google_type"] == 2
    # Descending count: BAR (3) before RESTAURANT (1).
    assert list(response["besttime_types"].keys()) == ["BAR", "RESTAURANT"]
    assert response["besttime_types"] == {"BAR": 3, "RESTAURANT": 1}
    assert response["google_places_types"] == {"bar": 2}


def test_breakdown_buckets_missing_venue_type_as_unknown():
    dao = _BreakdownDao(venues=[_venue("ven_untyped", venue_type=None)])
    admin_trigger_router.set_container(SimpleNamespace(redis_venue_dao=dao))

    response = admin_trigger_router.venue_type_breakdown()

    assert response["besttime_types"] == {"unknown": 1}
    assert response["with_google_type"] == 0
    assert response["google_places_types"] == {}


def test_breakdown_raises_503_when_container_not_initialized():
    admin_trigger_router.set_container(None)

    with pytest.raises(HTTPException) as exc_info:
        admin_trigger_router.venue_type_breakdown()

    assert exc_info.value.status_code == 503
