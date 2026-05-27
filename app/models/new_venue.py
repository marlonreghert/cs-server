"""Pydantic models for BestTime venue registration and account-inventory listing."""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.week_raw import WeekRawDay


class NewVenueInfo(BaseModel):
    """venue_info block returned by BestTime POST /forecasts.

    BestTime uses `venue_lon` here (vs `venue_lng` in /api/v1/venues). We
    accept both spellings via alias and always expose `venue_lng` to the
    rest of the codebase.
    """

    venue_id: str
    venue_name: Optional[str] = None
    venue_address: Optional[str] = None
    venue_lat: Optional[float] = None
    venue_lng: Optional[float] = Field(default=None, alias="venue_lon")
    venue_timezone: Optional[str] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None
    price_level: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True)


class NewVenueResponse(BaseModel):
    """POST /forecasts response envelope.

    BestTime returns `status="OK"` on success and `status="Error"` (with a
    `message` field) on geocoder rejection or monthly cap exceeded.
    `analysis` may be partial or empty on fresh creates while BestTime
    still computes the foot-traffic forecast.
    """

    status: str
    message: Optional[str] = None
    venue_info: Optional[NewVenueInfo] = None
    analysis: list[WeekRawDay] = Field(default_factory=list)
    epoch_analysis: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)

    def is_ok(self) -> bool:
        return self.status == "OK" and self.venue_info is not None and bool(self.venue_info.venue_id)


class AccountInventoryVenue(BaseModel):
    """A single row from BestTime GET /api/v1/venues."""

    venue_id: str
    venue_name: Optional[str] = None
    venue_address: Optional[str] = None
    venue_lat: Optional[float] = None
    venue_lng: Optional[float] = None
    venue_forecasted: bool = False
    forecast_updated_on: Optional[str] = None
