"""Weekly raw forecast data models using Pydantic."""
from typing import Optional
from pydantic import BaseModel
from app.models.venue import DayInfo


class WeekRawDay(BaseModel):
    """Single day in the weekly forecast with hourly busyness data."""
    day_raw: list[int]  # 24 hourly values
    day_int: int  # 0=Monday to 6=Sunday
    day_info: Optional[DayInfo] = None


class WeekRawAnalysis(BaseModel):
    """Analysis block containing 7 days of weekly forecast data."""
    week_raw: list[WeekRawDay]


class RawWindow(BaseModel):
    """Time window metadata describing the forecast scope."""
    time_window_start: int = 0
    time_window_start_12h: str = ""
    day_window_start_int: int = 0
    day_window_start_txt: str = ""
    day_window_end_int: int = 0
    day_window_end_txt: str = ""
    time_window_end: int = 0
    time_window_end_12h: str = ""
    week_window: str = ""


class WeekRawResponse(BaseModel):
    """Response from GET /forecasts/week/raw2 endpoint."""
    venue_address: str = ""
    window: RawWindow
    status: str
    analysis: WeekRawAnalysis
    venue_name: str = ""
    venue_id: str = ""
