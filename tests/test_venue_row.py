"""Unit tests for the Ex1 venue ⇄ row split/reconstruct contract.

The lynchpin invariant: the column set and the residual set together cover EVERY
Venue field, disjointly. If a Venue field is added without assigning it to a
column or the residual, reconstruction would silently drop it — this test fails
first.
"""
from app.dao.venue_row import (
    COLUMN_FIELDS,
    RESIDUAL_FIELDS,
    split_venue_for_storage,
    venue_from_row,
)
from app.models import Venue
from app.models.venue import FootTrafficForecast


def _full_venue(vid="v1") -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=f"Bar {vid}",
        venue_address=f"{vid} Rua X, 100", venue_lat=-8.05, venue_lng=-34.88,
        venue_type="BAR", price_level=2, rating=4.5, reviews=321, priority=4,
        venue_dwell_time_min=30, venue_dwell_time_max=90,
        venue_foot_traffic_forecast=[FootTrafficForecast(day_int=0, day_raw=[10] * 24)],
    )


def test_columns_and_residual_partition_every_venue_field():
    model_field_keys = {
        (f.alias or name) for name, f in Venue.model_fields.items()
    }
    assert set(COLUMN_FIELDS) | set(RESIDUAL_FIELDS) == model_field_keys
    assert set(COLUMN_FIELDS).isdisjoint(set(RESIDUAL_FIELDS))


def test_residual_holds_only_nested_fields():
    _, residual = split_venue_for_storage(_full_venue())
    assert set(residual.keys()) == set(RESIDUAL_FIELDS)
    assert set(residual.keys()).isdisjoint(set(COLUMN_FIELDS))


def test_split_then_reconstruct_round_trips():
    venue = _full_venue()
    columns, residual = split_venue_for_storage(venue)
    row = dict(columns)
    row["extra"] = residual
    reconstructed = venue_from_row(row)
    assert reconstructed.model_dump(by_alias=True, mode="json") == venue.model_dump(
        by_alias=True, mode="json"
    )


def test_reconstruct_ignores_payload_uses_columns():
    venue = _full_venue()
    columns, residual = split_venue_for_storage(venue)
    row = dict(columns)
    row["extra"] = residual
    # A stale/wrong payload must NOT influence reconstruction (columns are truth).
    row["payload"] = {"venue_id": "v1", "venue_lat": 0.0, "venue_lng": 0.0,
                      "venue_name": "WRONG"}
    assert venue_from_row(row).venue_name == "Bar v1"


def test_reconstruct_handles_minimal_residual():
    venue = Venue(venue_id="v2", venue_name="Bare", venue_lat=-8.0, venue_lng=-34.0)
    columns, residual = split_venue_for_storage(venue)
    row = dict(columns)
    row["extra"] = residual
    out = venue_from_row(row)
    assert out.venue_foot_traffic_forecast is None
    assert out.venue_dwell_time_min is None
    assert out.venue_id == "v2"
