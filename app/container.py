"""Dependency injection container for application components."""
import logging
from typing import Optional

import redis

from app.config import Settings
from app.db import GeoRedisClient
from app.dao import RedisVenueDAO
from app.api import BestTimeAPIClient
from app.services import VenueService, VenuesRefresherService
from app.handlers import VenueHandler

logger = logging.getLogger(__name__)


class Container:
    """Dependency injection container.

    Matches Go implementation: di/container.go
    Initializes and wires up all application dependencies.
    """

    def __init__(self, settings: Settings):
        """Initialize container with all dependencies.

        Args:
            settings: Application settings
        """
        logger.info(f"[Container] Initializing container")
        self.settings = settings

        # Initialize Redis client
        logger.info(
            f"[Container] Connecting to Redis at {settings.redis_host}:{settings.redis_port}"
        )
        redis_internal_client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            db=settings.redis_db,
            decode_responses=True,
        )

        # Test Redis connection
        try:
            redis_internal_client.ping()
            logger.info("[Container] Redis connection successful")
        except Exception as e:
            logger.error(f"[Container] Failed to connect to Redis: {e}")
            raise

        # Initialize Redis client wrapper
        self.redis_client = GeoRedisClient(redis_internal_client)

        # Initialize Redis Venue DAO
        self.redis_venue_dao = RedisVenueDAO(self.redis_client)

        # Initialize BestTime API client
        self.besttime_api = BestTimeAPIClient(
            api_key_public=settings.besttime_public_key,
            api_key_private=settings.besttime_private_key,
            base_url=settings.besttime_endpoint_base_v1,
        )

        # Initialize services
        self.venue_service = VenueService(self.redis_venue_dao, self.besttime_api)
        self.venues_refresher_service = VenuesRefresherService(
            self.redis_venue_dao,
            self.besttime_api,
            venue_limit_override=settings.venue_limit_override,
        )

        # Initialize handlers
        self.venue_handler = VenueHandler(self.redis_venue_dao)

        logger.info("[Container] Container initialized successfully")

    async def shutdown(self):
        """Clean up resources on shutdown."""
        logger.info("[Container] Shutting down container")
        try:
            await self.besttime_api.close()
            logger.info("[Container] BestTime API client closed")
        except Exception as e:
            logger.error(f"[Container] Error closing BestTime API client: {e}")
