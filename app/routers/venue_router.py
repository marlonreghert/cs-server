"""FastAPI routes for venue endpoints."""
import logging
from typing import Union

from fastapi import APIRouter, HTTPException, Query

from app.models import VenueWithLive, MinifiedVenue

logger = logging.getLogger(__name__)

# Create router at module level
router = APIRouter()

# Global handler reference - set during startup
_venue_handler = None


def set_venue_handler(handler):
    """Set the venue handler instance (called during startup)."""
    global _venue_handler
    _venue_handler = handler
    logger.info("[VenueRouter] Handler injected successfully")


def get_handler():
    """Get the venue handler, raising error if not initialized."""
    if _venue_handler is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return _venue_handler


@router.get(
    "/v1/venues/nearby",
    response_model=Union[list[VenueWithLive], list[MinifiedVenue]],
    summary="Get nearby venues",
    description="Get venues within a radius of a location with live and weekly forecasts",
)
def get_venues_nearby(
    lat: float = Query(..., description="Latitude", ge=-90, le=90),
    lon: float = Query(..., description="Longitude", ge=-180, le=180),
    radius: float = Query(..., description="Radius in kilometers", gt=0),
    verbose: bool = Query(
        False,
        description="If true, return full VenueWithLive; if false, return MinifiedVenue",
    ),
) -> Union[list[VenueWithLive], list[MinifiedVenue]]:
    """Get nearby venues with live and weekly forecasts."""
    try:
        handler = get_handler()
        return handler.get_venues_nearby(lat, lon, radius, verbose)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[VenueRouter] Error in get_venues_nearby: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/ping",
    summary="Health check",
    description="Health check endpoint",
)
def ping() -> dict[str, str]:
    """Health check endpoint."""
    handler = get_handler()
    return handler.ping()
