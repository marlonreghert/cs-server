"""Venue data models using Pydantic."""
from datetime import datetime
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


class PriceRange(BaseModel):
    """Objective money range from Google `priceRange` (the served structured range).

    `currency` is the ISO code (Google `currencyCode`, e.g. "BRL" — not a symbol).
    `min`/`max` are the start/end amounts in whole currency units. `max` is null
    when Google returns an unbounded upper bound ("more than X").
    """
    currency: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None

    model_config = ConfigDict(populate_by_name=True)


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
    # Served price tier: int 1..4 or NULL (unknown). NEVER 0 — `0` was the legacy
    # "unknown rendered as cheapest" bug, eliminated by migration 0013 + the shared
    # derivation helper (app/services/price_signal.py).
    price_level: Optional[int] = None
    # Raw, auditable price signals backing the served tier (promoted RDS columns):
    price_range: Optional[PriceRange] = None       # raw structured Google range
    google_price_level: Optional[str] = None       # raw Google priceLevel enum string
    besttime_price_level: Optional[int] = None      # raw BestTime price (derivation step 3)
    price_level_source: Optional[str] = None        # google_enum | google_range | besttime | null
    rating: Optional[float] = None
    reviews: Optional[int] = None

    # Refresh-selection priority (0 = most important … 5 = least). Bounded
    # live/weekly refresh selects the top-X active venues by priority ascending.
    # Read from RDS only; intentionally NOT projected to Redis (serving ignores it).
    priority: int = 5

    # Forecast data (optional)
    venue_foot_traffic_forecast: Optional[list[FootTrafficForecast]] = None

    # Lifecycle metadata. Missing fields in legacy Redis JSON are active.
    lifecycle_status: str = "active"
    deprecated_at: Optional[datetime] = None
    deprecated_reason: Optional[str] = None
    deprecated_source: Optional[str] = None
    google_business_status: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)

    def is_deprecated(self) -> bool:
        """Return True when this venue should be hidden from active flows."""
        return self.lifecycle_status == "deprecated"

    def is_active(self) -> bool:
        """Return True when this venue should be used by serving/enrichment."""
        return not self.is_deprecated()

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
    venue_id: str = ""  # Venue identifier for client-side navigation
    venue_type: Optional[str] = None          # BestTime primary type (BAR, CLUBS, OTHER, etc.)
    google_places_type: Optional[str] = None  # Google Places granular type (bar, night_club, cocktail_bar, etc.)
    category: Optional[str] = None            # VibeSense display category (BAR, NIGHTCLUB, RESTAURANT, etc.)
    granular_type: Optional[str] = None       # Most specific type (cocktail_bar, japanese_restaurant, etc.)
    granular_label: Optional[str] = None      # PT-BR granular label ("Restaurante Japonês", "Pub Irlandês")
    label: Optional[str] = None               # PT-BR category label ("Bar", "Balada", "Restaurante")
    emoji: Optional[str] = None               # Category emoji
    color: Optional[str] = None               # Category color hex
    price_level: Optional[int] = None         # 1..4 or null (never 0)
    price_range: Optional[PriceRange] = None  # structured money range for the detail view
    rating: Optional[float] = None
    reviews: Optional[int] = None
    venue_foot_traffic_forecast: Optional[list[FootTrafficForecast]] = None
    venue_live_busyness: Optional[int] = None
    weekly_forecast: Optional[Any] = None

    # Vibe attributes (atmosphere labels)
    vibe_labels: Optional[list[str]] = None

    # Venue photos with author attribution (from Google Places API)
    venue_photos: Optional[list[dict]] = None  # [{url: str, author_name: str | None}, ...]

    # Opening hours (from Google Places API - in Portuguese)
    opening_hours: Optional[list[str]] = None  # ["Domingo: Fechado", "Segunda-feira: 20:00 – 03:00", ...]
    special_days: Optional[list[str]] = None   # Holiday hours: ["25 de dezembro: Fechado", ...]
    is_open_now: Optional[bool] = None         # Current open status
    hours_source: Optional[str] = None         # "google" (reliable) or "besttime" (estimated from foot traffic)

    # Instagram (from Apify enrichment)
    instagram_handle: Optional[str] = None
    instagram_url: Optional[str] = None

    # AI/Editorial summary (from Google Places API)
    venue_summary: Optional[str] = None

    # Reviews (from Google Places API)
    venue_reviews: Optional[list[dict]] = None

    # AI Vibe Profile (from Vibe Classifier pipeline)
    vibe_profile: Optional[dict] = None

    # Menu data (extracted from photos via GPT-4o-mini)
    venue_menu: Optional[dict] = None  # {sections: [...], currency_detected: str}

    model_config = ConfigDict(populate_by_name=True)
