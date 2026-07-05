"""Request models for the server-side batch venue-add endpoint.

One request ships a whole curated list; the server runs each row through the
same AddVenueHandler as the single by-address endpoint (see
app/services/batch_add_service.py and plans/260705_batch-add-venues-endpoint.md).
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class BatchAddRow(BaseModel):
    """One venue in a batch. lat/lng optional — resolved server-side from
    place_id/name+address when absent and resolve_coords is set."""

    venue_name: str = Field(..., min_length=1, max_length=256)
    venue_address: str = Field(..., min_length=1, max_length=1024)
    venue_lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    venue_lng: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    place_id: Optional[str] = None
    # Optional bias center for Text Search when coords must be resolved (the
    # curated lists group by capital; pass its center to disambiguate).
    bias_lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    bias_lng: Optional[float] = Field(default=None, ge=-180.0, le=180.0)

    model_config = ConfigDict(extra="ignore")


class BatchAddRequest(BaseModel):
    """Body for POST /admin/venues/batch-add."""

    venues: list[BatchAddRow] = Field(..., min_length=1, max_length=1000)
    # When true (default), rows missing coords are resolved via the Google
    # client; when false, such rows are skipped as skipped_unresolved_coords.
    resolve_coords: bool = True
    # Optional label to identify the run in the job doc / logs.
    label: Optional[str] = Field(default=None, max_length=120)

    model_config = ConfigDict(extra="ignore")
