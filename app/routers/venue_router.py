"""FastAPI routes for venue endpoints."""
import logging
from typing import Union

from fastapi import APIRouter, HTTPException, Query

from app.handlers import VenueHandler
from app.models import VenueWithLive, MinifiedVenue

logger = logging.getLogger(__name__)

# Create router
router = APIRouter()


def create_venue_router(venue_handler: VenueHandler) -> APIRouter:
    """Create venue router with injected handler.

    Args:
        venue_handler: VenueHandler instance

    Returns:
        Configured APIRouter
    """

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
        """Get nearby venues with live and weekly forecasts.

        Args:
            lat: Latitude
            lon: Longitude
            radius: Radius in kilometers
            verbose: Response format (default: false)

        Returns:
            List of venues (full or minified based on verbose flag)
        """
        try:
            return venue_handler.get_venues_nearby(lat, lon, radius, verbose)
        except Exception as e:
            logger.error(f"[VenueRouter] Error in get_venues_nearby: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

    @router.get(
        "/ping",
        summary="Health check",
        description="Health check endpoint",
    )
    def ping() -> dict[str, str]:
        """Health check endpoint.

        Returns:
            {"status": "pong"}
        """
        return venue_handler.ping()

    return router
