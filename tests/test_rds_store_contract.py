"""Contract test shared by the in-memory fake AND the real RdsVenueStore.

The fake runs always (proves the contract the repository relies on). The real
SQLAlchemy store runs ONLY when RDS_TEST_URL points at a scratch Postgres whose
schema has been migrated (`alembic upgrade head`) — there is no local Postgres
in CI/dev. This is the post-provisioning validation step:

    RDS_TEST_URL=postgresql+psycopg://user:pass@host:5432/db \
        .venv/bin/python -m pytest tests/test_rds_store_contract.py -v

Run it against the scratch DB BEFORE the production backfill so the real store's
SQL (upserts, ON CONFLICT, jsonb cast, composite weekly key) is proven first.
"""
import os
import uuid

import pytest

from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo
from app.models.vibe_attributes import VibeAttributes
from tests.rds_fake import InMemoryRdsVenueStore

_VA = "google_places.vibe_attributes"
_WEEKLY = "besttime.weekly_forecast"


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
    return f"ct_{uuid.uuid4().hex[:12]}"


def _venue(vid, name="Bar X", **kw):
    return Venue(venue_id=vid, venue_name=name, venue_address="a",
                 venue_lat=-8.05, venue_lng=-34.88, venue_type="BAR", **kw)


def test_venue_upsert_and_soft_delete(store):
    vid = _vid()
    store.upsert_venue(_venue(vid, "Boteco"))
    row = store.get_venue(vid)
    assert row is not None and row["payload"]["venue_name"] == "Boteco"
    assert row["lifecycle_status"] == "active"
    assert vid in store.list_active_venue_ids()

    store.soft_delete_venue(vid, "ineligible_google_type", "eligibility_filter")
    row = store.get_venue(vid)
    assert row["lifecycle_status"] == "deprecated"
    assert row["deprecated_reason"] == "ineligible_google_type"
    assert vid not in store.list_active_venue_ids()


def test_enrichment_upsert_history_and_soft_delete(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))  # FK parent
    attrs = VibeAttributes(venue_id=vid, google_place_id="p", google_primary_type="bar")
    store.upsert_enrichment(_VA, vid, attrs.model_dump(mode="json"),
                            history=True, promoted={"google_primary_type": "bar",
                                                    "google_place_id": "p"})
    rec = store.get_enrichment(_VA, vid)
    assert rec is not None and rec["deleted_at"] is None
    assert rec["payload"]["google_primary_type"] == "bar"

    store.soft_delete_enrichment(_VA, vid, history=True)
    assert store.get_enrichment(_VA, vid)["deleted_at"] is not None


def test_weekly_composite_key(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_enrichment(_WEEKLY, f"{vid}#0", {"day_int": 0, "day_raw": [1] * 24},
                            history=False)
    rec = store.get_enrichment(_WEEKLY, f"{vid}#0")
    assert rec is not None and rec["payload"]["day_int"] == 0


def test_live_forecast_current_state(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    lf = LiveForecastResponse(status="OK", venue_info=VenueInfo(venue_id=vid),
                              analysis=Analysis(venue_live_busyness=7, venue_live_busyness_available=True))
    store.upsert_live_forecast(vid, lf.model_dump(mode="json"))
    assert store.get_live_forecast(vid) is not None


def test_favorite_and_hot_like_event(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_favorite("pseudo-abc", vid)
    store.soft_delete_favorite("pseudo-abc", vid)  # un-favorite -> soft delete
    store.add_hot_like_event("pseudo-abc", vid)    # append-only, no error
