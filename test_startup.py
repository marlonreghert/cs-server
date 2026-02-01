"""Simple startup test to verify application initialization.

This script tests that all components can be initialized without errors.
Tests individual components and imports without requiring Redis.
"""
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_config_loading():
    """Test that configuration can be loaded."""
    from app.config import Settings

    logger.info("Testing config loading...")

    settings = Settings()

    assert settings.redis_host is not None
    assert settings.redis_port > 0
    assert settings.besttime_private_key is not None
    assert settings.venues_catalog_refresh_minutes == 43200
    assert settings.venues_live_refresh_minutes == 5
    assert settings.weekly_forecast_cron == "0 0 * * 0"

    logger.info("✓ Config loading successful")
    logger.info(f"  - Redis: {settings.redis_host}:{settings.redis_port}")
    logger.info(f"  - Venue catalog refresh: {settings.venues_catalog_refresh_minutes} min")
    logger.info(f"  - Live refresh: {settings.venues_live_refresh_minutes} min")
    logger.info(f"  - Weekly cron: {settings.weekly_forecast_cron}")


def test_service_imports():
    """Test that all service modules can be imported."""
    logger.info("Testing service imports...")

    from app.services import VenueService, VenuesRefresherService
    from app.handlers import VenueHandler
    from app.routers import create_venue_router
    from app.dao import RedisVenueDAO
    from app.db import GeoRedisClient
    from app.api import BestTimeAPIClient

    logger.info("✓ All service imports successful")
    logger.info("  - VenueService")
    logger.info("  - VenuesRefresherService")
    logger.info("  - VenueHandler")
    logger.info("  - create_venue_router")
    logger.info("  - RedisVenueDAO")
    logger.info("  - GeoRedisClient")
    logger.info("  - BestTimeAPIClient")


def test_fastapi_app_creation():
    """Test that FastAPI app can be created."""
    logger.info("Testing FastAPI app creation...")

    # Import will create the app
    from main import app

    assert app is not None
    assert app.title == "CS-Server API"

    logger.info("✓ FastAPI app creation successful")
    logger.info(f"  - Title: {app.title}")
    logger.info(f"  - Version: {app.version}")


def test_router_creation():
    """Test that venue router can be created."""
    from unittest.mock import Mock
    from app.routers import create_venue_router

    logger.info("Testing router creation...")

    # Create mock handler
    mock_handler = Mock()
    mock_handler.get_venues_nearby.return_value = []
    mock_handler.ping.return_value = {"status": "pong"}

    # Create router
    router = create_venue_router(mock_handler)

    assert router is not None
    assert len(router.routes) == 2  # /v1/venues/nearby and /ping

    logger.info("✓ Router creation successful")
    logger.info(f"  - Routes registered: {len(router.routes)}")
    for route in router.routes:
        logger.info(f"    - {route.methods} {route.path}")


def test_scheduler_jobs():
    """Test that scheduler job functions exist."""
    from main import (
        run_venue_catalog_refresh_job,
        run_live_forecast_refresh_job,
        run_weekly_forecast_refresh_job,
    )

    logger.info("Testing scheduler job functions...")

    assert run_venue_catalog_refresh_job is not None
    assert run_live_forecast_refresh_job is not None
    assert run_weekly_forecast_refresh_job is not None

    logger.info("✓ Scheduler job functions exist")
    logger.info("  - run_venue_catalog_refresh_job")
    logger.info("  - run_live_forecast_refresh_job")
    logger.info("  - run_weekly_forecast_refresh_job")


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("CS-Server Startup Tests")
    logger.info("=" * 60)

    try:
        test_config_loading()
        logger.info("")
        test_service_imports()
        logger.info("")
        test_fastapi_app_creation()
        logger.info("")
        test_router_creation()
        logger.info("")
        test_scheduler_jobs()
        logger.info("")
        logger.info("=" * 60)
        logger.info("✓ All startup tests passed!")
        logger.info("=" * 60)
        logger.info("")
        logger.info("Note: Full integration testing requires Redis.")
        logger.info("To start the server: python -m uvicorn main:app --host 0.0.0.0 --port 8080")
    except Exception as e:
        logger.error(f"✗ Startup test failed: {e}", exc_info=True)
        exit(1)
