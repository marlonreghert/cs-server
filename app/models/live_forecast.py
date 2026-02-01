"""Live forecast data models using Pydantic."""
from pydantic import BaseModel


class Analysis(BaseModel):
    """Live busyness analysis with availability flags."""
    venue_forecasted_busyness: int = 0
    venue_live_busyness: int = 0
    venue_live_busyness_available: bool = False
    venue_forecast_busyness_available: bool = False
    venue_live_forecasted_delta: int = 0


class VenueInfo(BaseModel):
    """Venue metadata with timezone and dwell time information."""
    venue_current_gmttime: str = ""
    venue_current_localtime: str = ""
    venue_id: str = ""
    venue_name: str = ""
    venue_timezone: str = ""
    venue_dwell_time_min: int = 0
    venue_dwell_time_max: int = 0
    venue_dwell_time_avg: int = 0


class LiveForecastResponse(BaseModel):
    """Response from POST /forecasts/live endpoint."""
    analysis: Analysis
    status: str
    venue_info: VenueInfo
