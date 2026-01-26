"""Venue filter models for BestTime API."""
from typing import Optional
from pydantic import BaseModel
from app.models.venue import DayInfo


class FilterWindow(BaseModel):
    """Time window metadata for venue filter response."""
    day_window: str = ""
    day_window_end_int: int = 0
    day_window_end_txt: str = ""
    day_window_start_int: int = 0
    day_window_start_txt: str = ""
    time_local: int = 0
    time_local_12: str = ""
    time_local_index: int = 0
    time_window_end: int = 0
    time_window_end_12: str = ""
    time_window_end_ix: int = 0
    time_window_start: int = 0
    time_window_start_12: str = ""
    time_window_start_ix: int = 0


class VenueFilterVenue(BaseModel):
    """Single venue from /venues/filter API response."""
    # Foot traffic fields
    day_int: int
    day_raw: list[int]
    day_raw_whole: Optional[list[int]] = None
    day_info: Optional[DayInfo] = None

    # Core venue info
    venue_address: str
    venue_lat: float
    venue_lng: float
    venue_id: str
    venue_name: str

    # Extra fields
    venue_type: Optional[str] = None
    venue_dwell_time_min: Optional[int] = None
    venue_dwell_time_max: Optional[int] = None
    price_level: Optional[int] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None


class VenueFilterResponse(BaseModel):
    """Response from /venues/filter API endpoint."""
    status: str
    venues: list[VenueFilterVenue]
    venues_n: int
    window: Optional[FilterWindow] = None


class VenueFilterParams(BaseModel):
    """Parameters for /venues/filter API request."""
    collection_id: Optional[str] = None
    busy_min: Optional[int] = None
    busy_max: Optional[int] = None
    busy_conf: Optional[str] = None  # "any" (default) | "all"
    foot_traffic: Optional[str] = None  # "limited"(default) | "day" | "both"
    hour_min: Optional[int] = None  # 0..24
    hour_max: Optional[int] = None  # 0..24
    day_int: Optional[int] = None  # 0..6
    now: Optional[bool] = None
    live: Optional[bool] = None
    types: Optional[list[str]] = None  # ["BAR", "CAFE", "RESTAURANT"]
    lat: Optional[float] = None  # Must be paired with lng & radius
    lng: Optional[float] = None
    radius: Optional[int] = None  # Meters
    lat_min: Optional[float] = None  # Bounding box (all 4 required)
    lng_min: Optional[float] = None
    lat_max: Optional[float] = None
    lng_max: Optional[float] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    rating_min: Optional[float] = None  # 2.0, 2.5, 3.0, 3.5, 4.0, 4.5
    rating_max: Optional[float] = None  # 2.0 .. 5.0
    reviews_min: Optional[int] = None
    reviews_max: Optional[int] = None
    day_rank_min: Optional[int] = None
    day_rank_max: Optional[int] = None
    own_venues_only: Optional[bool] = None
    order_by: Optional[str] = None  # e.g., "day_rank_max,reviews"
    order: Optional[str] = None  # e.g., "desc,desc"
    limit: Optional[int] = None  # Default 5000
    page: Optional[int] = None  # Default 0

    def to_query_params(self) -> dict[str, str]:
        """Convert parameters to query string dict, omitting None values."""
        params = {}

        if self.collection_id is not None:
            params["collection_id"] = self.collection_id
        if self.busy_min is not None:
            params["busy_min"] = str(self.busy_min)
        if self.busy_max is not None:
            params["busy_max"] = str(self.busy_max)
        if self.busy_conf is not None:
            params["busy_conf"] = self.busy_conf
        if self.foot_traffic is not None:
            params["foot_traffic"] = self.foot_traffic
        if self.hour_min is not None:
            params["hour_min"] = str(self.hour_min)
        if self.hour_max is not None:
            params["hour_max"] = str(self.hour_max)
        if self.day_int is not None:
            params["day_int"] = str(self.day_int)
        if self.now is not None:
            params["now"] = "true" if self.now else "false"
        if self.live is not None:
            params["live"] = "true" if self.live else "false"
        if self.types is not None and len(self.types) > 0:
            params["types"] = ",".join(self.types)
        if self.lat is not None:
            params["lat"] = str(self.lat)
        if self.lng is not None:
            params["lng"] = str(self.lng)
        if self.radius is not None:
            params["radius"] = str(self.radius)
        if self.lat_min is not None:
            params["lat_min"] = str(self.lat_min)
        if self.lng_min is not None:
            params["lng_min"] = str(self.lng_min)
        if self.lat_max is not None:
            params["lat_max"] = str(self.lat_max)
        if self.lng_max is not None:
            params["lng_max"] = str(self.lng_max)
        if self.price_min is not None:
            params["price_min"] = str(self.price_min)
        if self.price_max is not None:
            params["price_max"] = str(self.price_max)
        if self.rating_min is not None:
            params["rating_min"] = str(self.rating_min)
        if self.rating_max is not None:
            params["rating_max"] = str(self.rating_max)
        if self.reviews_min is not None:
            params["reviews_min"] = str(self.reviews_min)
        if self.reviews_max is not None:
            params["reviews_max"] = str(self.reviews_max)
        if self.day_rank_min is not None:
            params["day_rank_min"] = str(self.day_rank_min)
        if self.day_rank_max is not None:
            params["day_rank_max"] = str(self.day_rank_max)
        if self.own_venues_only is not None:
            params["own_venues_only"] = "true" if self.own_venues_only else "false"
        if self.order_by is not None:
            params["order_by"] = self.order_by
        if self.order is not None:
            params["order"] = self.order
        if self.limit is not None:
            params["limit"] = str(self.limit)
        if self.page is not None:
            params["page"] = str(self.page)

        return params
