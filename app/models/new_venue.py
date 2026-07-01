"""Pydantic models for BestTime venue registration and account-inventory listing."""

import logging
from typing import Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from app.metrics import BESTTIME_ADD_VENUE_ANALYSIS_DROPPED_TOTAL
from app.models.week_raw import WeekRawDay

logger = logging.getLogger(__name__)


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


def _coerce_analysis_day(entry) -> Optional[WeekRawDay]:
    """Normalize one POST /forecasts analysis entry to a WeekRawDay.

    Accepts both the flat shape (top-level `day_int` + `day_raw`, as served
    by GET /forecasts/week/raw2) and the real create-endpoint shape where the
    day number lives inside `day_info` with the hourly `day_raw` list
    alongside. Returns None when the entry cannot represent a day.
    """
    if isinstance(entry, WeekRawDay):
        return entry
    if not isinstance(entry, dict):
        return None
    data = dict(entry)
    day_info = data.get("day_info")
    if "day_int" not in data and isinstance(day_info, dict) and "day_int" in day_info:
        data["day_int"] = day_info["day_int"]
    try:
        return WeekRawDay.model_validate(data)
    except ValidationError:
        return None


class NewVenueResponse(BaseModel):
    """POST /forecasts response envelope.

    BestTime returns `status="OK"` on success and `status="Error"` (with a
    `message` field) on geocoder rejection or monthly cap exceeded.
    `analysis` may be partial or empty on fresh creates while BestTime
    still computes the foot-traffic forecast, and is parsed best-effort:
    entries that cannot be normalized to a WeekRawDay are dropped (WARNING +
    metric) and never fail the envelope. Only `status`/`message`/`venue_info`
    decide the outcome of a create.
    """

    status: str
    message: Optional[str] = None
    venue_info: Optional[NewVenueInfo] = None
    analysis: list[WeekRawDay] = Field(default_factory=list)
    epoch_analysis: Optional[Union[int, str]] = None

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("analysis", mode="before")
    @classmethod
    def _parse_analysis_tolerantly(cls, value, info: ValidationInfo):
        """Keep the analysis days that parse; drop the rest, never raise."""
        if value is None:
            return []
        venue_info = info.data.get("venue_info")
        venue_id = getattr(venue_info, "venue_id", None) or "unknown"
        if not isinstance(value, list):
            BESTTIME_ADD_VENUE_ANALYSIS_DROPPED_TOTAL.inc()
            logger.warning(
                f"[NewVenueResponse] analysis for venue {venue_id} is not a "
                f"list ({type(value).__name__}); ignoring it"
            )
            return []
        days = []
        for index, entry in enumerate(value):
            day = _coerce_analysis_day(entry)
            if day is None:
                BESTTIME_ADD_VENUE_ANALYSIS_DROPPED_TOTAL.inc()
                logger.warning(
                    f"[NewVenueResponse] dropping unparseable analysis entry "
                    f"index={index} for venue {venue_id}"
                )
                continue
            days.append(day)
        return days

    def is_ok(self) -> bool:
        return (
            self.status == "OK"
            and self.venue_info is not None
            and bool(self.venue_info.venue_id)
        )


class AccountInventoryVenue(BaseModel):
    """A single row from BestTime GET /api/v1/venues."""

    venue_id: str
    venue_name: Optional[str] = None
    venue_address: Optional[str] = None
    venue_lat: Optional[float] = None
    venue_lng: Optional[float] = None
    venue_forecasted: bool = False
    forecast_updated_on: Optional[str] = None
