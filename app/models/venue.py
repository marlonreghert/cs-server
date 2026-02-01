"""Venue data models using Pydantic."""
from typing import Optional, Any, Union
from pydantic import BaseModel, Field, field_validator, ConfigDict


class OpenCloseDetail(BaseModel):
    """Open/close time detail with hour and minute precision.

    Handles both scenarios:
    - Full data: {"opens": 7, "closes": 21, "opens_minutes": 0, "closes_minutes": 0}
    - Minimal data: {"opens": 7, "closes": 21}
    """
    opens: int
    closes: int
    opens_minutes: Optional[int] = None  # Present in some responses, absent in others
    closes_minutes: Optional[int] = None  # Present in some responses, absent in others


class DayInfoV2(BaseModel):
    """Extended opening hours information with multiple time windows.

    Handles both scenarios:
    - Full data with all fields present
    - Minimal data: {"24h": [{"opens": 7, "closes": 21}], "12h": ["7am-9pm"]}
    """
    open_24h: Optional[bool] = Field(default=None, alias="open_24h")
    crosses_midnight: Optional[bool] = None
    day_text: Optional[str] = None
    special_day: Optional[Any] = None
    h24: list[OpenCloseDetail] = Field(default_factory=list, alias="24h")
    h12: list[str] = Field(default_factory=list, alias="12h")

    model_config = ConfigDict(populate_by_name=True)


class DayInfo(BaseModel):
    """Detailed information for a single day's forecast.

    Handles both scenarios:
    - Open day: all fields present including day_max, day_mean
    - Closed day: day_max and day_mean may be absent
    """
    day_int: int
    day_max: Optional[int] = None  # Absent for closed days
    day_mean: Optional[int] = None  # Absent for closed days
    day_rank_max: Optional[int] = None
    day_rank_mean: Optional[int] = None
    day_text: str = ""
    venue_open: str = ""
    venue_closed: str = ""
    venue_open_close_v2: Optional[DayInfoV2] = None
    note: Optional[str] = None  # API sometimes includes notes

    @field_validator("venue_open", "venue_closed", mode="before")
    @classmethod
    def convert_open_closed_to_string(cls, v: Any) -> str:
        """Convert int or float to string for venue_open/venue_closed fields.

        The BestTime API sometimes returns these as integers, sometimes as strings.
        We normalize to string to match the Go implementation.
        """
        if isinstance(v, (int, float)):
            return str(int(v))
        elif isinstance(v, str):
            return v
        else:
            return ""


class FootTrafficForecast(BaseModel):
    """Forecast data for a specific day with hourly busyness values."""
    day_int: int
    day_raw: list[int]  # 24 hourly busyness values (0-100 scale)
    day_info: Optional[DayInfo] = None


class Venue(BaseModel):
    """Venue with location, metadata, and forecast data."""

    # Core flags
    forecast: bool = False
    processed: bool = False

    # Location data
    venue_address: str = ""
    venue_lat: float
    venue_lng: float = Field(alias="venue_lng")  # Note: Go uses venue_lng
    venue_name: str = ""
    venue_id: str = ""

    # Extra details (optional)
    venue_type: Optional[str] = None
    venue_dwell_time_min: Optional[int] = None
    venue_dwell_time_max: Optional[int] = None
    price_level: Optional[int] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None

    # Forecast data (optional)
    venue_foot_traffic_forecast: Optional[list[FootTrafficForecast]] = None

    model_config = ConfigDict(populate_by_name=True)

    def __str__(self) -> str:
        """String representation matching Go's ToString method."""
        return (
            f"Venue(name={self.venue_name}, address={self.venue_address}, "
            f"lat={self.venue_lat}, lon={self.venue_lng})"
        )


class VenueWithLive(BaseModel):
    """Venue with merged live and weekly forecast data (for verbose API responses).

    Matches Go implementation: server/handlers/venue_handler.go lines 26-31
    """

    venue: Venue
    live_forecast: Optional[Any] = None  # LiveForecastResponse type (defined in live_forecast.py)
    weekly_forecast: Optional[Any] = None  # WeekRawDay type (defined in week_raw.py)

    model_config = ConfigDict(populate_by_name=True)


class MinifiedVenue(BaseModel):
    """Minified venue response for non-verbose API mode."""

    forecast: bool
    processed: bool
    venue_address: str
    venue_lat: float
    venue_lng: float
    venue_name: str
    price_level: Optional[int] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None
    venue_foot_traffic_forecast: Optional[list[FootTrafficForecast]] = None
    venue_live_busyness: Optional[int] = None
    weekly_forecast: Optional[Any] = None

    model_config = ConfigDict(populate_by_name=True)
