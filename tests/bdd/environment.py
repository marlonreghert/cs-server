"""Behave test harness for cs-server BDD scenarios.

Builds a self-contained FastAPI app per scenario with:
- fakeredis as the Redis backend (no docker required)
- A mocked BestTime client whose responses can be programmed per-scenario
- Real handlers/DAOs/services so behaviour is end-to-end except at the
  BestTime HTTP boundary
"""
from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao import RedisVenueDAO, VenueBudgetDao
from app.db.geo_redis_client import GeoRedisClient
from app.handlers import AddVenueHandler
from app.services.venue_budget_service import VenueBudgetService


class _ProgrammableBestTime:
    """Minimal stub for BestTimeAPIClient used in BDD.

    Scenarios set the next response via the `programmed_*` attributes.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # Default: no programmed responses; tests must set them explicitly.
        self.programmed_add_venue: Any = None
        self.programmed_live_forecast: Any = None
        self.programmed_week_forecast: Any = None
        self.programmed_venue_filter: Any = None
        self.programmed_inventory_pages: list[list[dict]] | None = None

    async def add_venue_to_account(self, venue_name: str, venue_address: str):
        self.calls.append(
            {"method": "add_venue_to_account", "venue_name": venue_name, "venue_address": venue_address}
        )
        if self.programmed_add_venue is None:
            raise RuntimeError("BDD harness: programmed_add_venue not set")
        if isinstance(self.programmed_add_venue, Exception):
            raise self.programmed_add_venue
        return self.programmed_add_venue

    async def get_live_forecast(self, venue_id=None, venue_name=None, venue_address=None):
        self.calls.append({"method": "get_live_forecast", "venue_id": venue_id})
        if self.programmed_live_forecast is None:
            raise RuntimeError("BDD harness: programmed_live_forecast not set")
        return self.programmed_live_forecast

    async def get_week_raw_forecast(self, venue_id: str):
        self.calls.append({"method": "get_week_raw_forecast", "venue_id": venue_id})
        if self.programmed_week_forecast is None:
            raise RuntimeError("BDD harness: programmed_week_forecast not set")
        return self.programmed_week_forecast

    async def venue_filter(self, params):
        self.calls.append({"method": "venue_filter", "params": params})
        if self.programmed_venue_filter is None:
            raise RuntimeError("BDD harness: programmed_venue_filter not set")
        return self.programmed_venue_filter

    async def list_account_inventory(self, page_size: int = 1000):
        from app.models import AccountInventoryVenue

        self.calls.append({"method": "list_account_inventory", "page_size": page_size})
        for page in self.programmed_inventory_pages or []:
            for venue in page:
                if isinstance(venue, AccountInventoryVenue):
                    yield venue
                else:
                    yield AccountInventoryVenue.model_validate(venue)

    async def close(self):
        pass


def _build_test_app(context) -> None:
    """Construct a fresh test app + harness for the scenario."""
    # Fresh fakeredis instance
    context.fake_redis = fakeredis.FakeRedis(decode_responses=True)
    context.geo_redis = GeoRedisClient(context.fake_redis)
    context.venue_dao = RedisVenueDAO(context.geo_redis)
    context.besttime = _ProgrammableBestTime()
    context.fixed_year_month = "2026-05"  # default; overridable per scenario

    # FastAPI app + lazy router registration. The new admin venue router
    # may not exist yet during true-RED; tolerate ImportError so the test
    # harness reports a meaningful HTTP 404 rather than crashing on import.
    app = FastAPI()
    try:
        from app.routers import admin_trigger_router, set_admin_container

        app.include_router(admin_trigger_router)

        # Pin year_month deterministically so scenarios can write counters
        # under a specific key.
        def _year_month_provider():
            return getattr(context, "year_month", context.fixed_year_month)

        context.budget_dao = VenueBudgetDao(context.fake_redis)
        context.budget_service = VenueBudgetService(
            redis_client=context.fake_redis,
            budget_dao=context.budget_dao,
            year_month_provider=_year_month_provider,
        )
        context.add_venue_handler = AddVenueHandler(
            venue_dao=context.venue_dao,
            besttime_api=context.besttime,
            budget_service=context.budget_service,
            redis_client=context.fake_redis,
        )

        container = MagicMock()
        container.venue_dao = context.venue_dao
        container.redis_venue_dao = context.venue_dao
        container.besttime_api = context.besttime
        container.fake_redis = context.fake_redis
        container.fixed_year_month = context.fixed_year_month
        container.add_venue_handler = context.add_venue_handler
        container.venue_budget_service = context.budget_service
        try:
            set_admin_container(container)
        except Exception:
            pass
        context.container = container
    except ImportError:
        pass

    context.app = app
    context.client = TestClient(app)


def before_feature(context, feature):
    # Features tagged @wip are work-in-progress (steps not yet implemented).
    if "wip" in feature.tags:
        feature.skip("WIP — step definitions not yet implemented")


def before_scenario(context, scenario):
    # Scenario-level @wip skip (e.g. config-to-RDS lands with vibes_bot in Phase 2).
    if "wip" in scenario.effective_tags:
        scenario.skip("WIP — deferred to a later phase")
        return
    _build_test_app(context)
    _build_rds_layer(context)


def _build_rds_layer(context) -> None:
    """Attach the RDS write-through layer with an in-memory fake store.

    Added alongside the plain context.venue_dao so existing features are
    unaffected; the RDS feature uses context.repository / context.rds_store.
    """
    from app.dao.redis_venue_dao import RedisVenueDAO
    from app.dao.venue_repository import VenueRepository
    from app.services.engagement_service import EngagementService
    from app.services.redis_projection_service import RedisProjectionService
    from tests.rds_fake import InMemoryRdsVenueStore

    context.rds_store = InMemoryRdsVenueStore()
    context.repository = VenueRepository(context.geo_redis, rds_store=context.rds_store)
    context.redis_only_dao = RedisVenueDAO(context.geo_redis)
    context.redis_projection_service = RedisProjectionService(
        repository=context.repository,
        redis_only_dao=context.redis_only_dao,
        rds_store=context.rds_store,
    )
    context.engagement_service = EngagementService(
        redis_client=context.fake_redis,
        rds_store=context.rds_store,
        pseudonymization_key="test-hmac-key",
    )


def after_scenario(context, scenario):
    try:
        context.client.close()
    except Exception:
        pass
    # Drop cached app modules with mutable global state so the next scenario
    # starts clean. We only need to drop the admin router module because
    # that's the one we re-inject container state into per scenario.
    for mod_name in list(sys.modules):
        if mod_name.startswith("app.routers.admin_trigger_router"):
            importlib.reload(sys.modules[mod_name])
