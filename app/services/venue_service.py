"""Simple venue service wrapper over DAO."""
import logging
from typing import Optional

from app.api import BestTimeAPIClient
from app.dao import RedisVenueDAO
from app.models import Venue

logger = logging.getLogger(__name__)


class VenueService:
    """Service for venue queries (simple wrapper over DAO)."""

    def __init__(self, venue_dao: RedisVenueDAO, besttime_api: BestTimeAPIClient):
        """Initialize venue service.

        Args:
            venue_dao: Redis DAO for venue persistence
            besttime_api: BestTime API client
        """
        self.venue_dao = venue_dao
        self.besttime_api = besttime_api

    def get_venues_nearby(self, lat: float, lon: float, radius: float) -> list[Venue]:
        """Get venues within radius of a location.

        Args:
            lat: Latitude
            lon: Longitude
            radius: Radius in kilometers

        Returns:
            List of venues within radius
        """
        return self.venue_dao.get_nearby_venues(lat, lon, radius)
