"""Unit tests for VenueBudgetDao and VenueBudgetService."""
import json
from unittest.mock import MagicMock

import fakeredis
import pytest

from app.dao.venue_budget_dao import VenueBudgetDao
from app.services.venue_budget_service import (
    DEFAULT_MANUAL_RESERVE,
    DEFAULT_MONTHLY_QUOTA,
    VenueBudgetService,
)


@pytest.fixture
def fake():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def dao(fake):
    return VenueBudgetDao(fake)


@pytest.fixture
def service(fake, dao):
    return VenueBudgetService(
        redis_client=fake,
        budget_dao=dao,
        year_month_provider=lambda: "2026-05",
    )


class TestVenueBudgetDao:
    def test_get_month_count_returns_zero_when_unset(self, dao):
        assert dao.get_month_count("2026-05") == 0

    def test_increment_then_get(self, dao):
        assert dao.increment_month("2026-05", 3) == 3
        assert dao.get_month_count("2026-05") == 3
        assert dao.increment_month("2026-05", 1) == 4

    def test_decrement_clamps_at_zero(self, dao):
        dao.increment_month("2026-05", 2)
        dao.decrement_month("2026-05", 5)
        assert dao.get_month_count("2026-05") == 0

    def test_year_month_rollover_produces_separate_counter(self, dao):
        dao.increment_month("2026-05", 7)
        dao.increment_month("2026-06", 1)
        assert dao.get_month_count("2026-05") == 7
        assert dao.get_month_count("2026-06") == 1

    def test_negative_or_zero_increment_is_noop(self, dao):
        dao.increment_month("2026-05", 0)
        dao.increment_month("2026-05", -3)
        assert dao.get_month_count("2026-05") == 0


class TestVenueBudgetService:
    def test_defaults_when_admin_config_missing(self, service):
        settings = service.get_quota_settings()
        assert settings.monthly_quota == DEFAULT_MONTHLY_QUOTA
        assert settings.manual_reserve == DEFAULT_MANUAL_RESERVE

    def test_reads_admin_config_live(self, service, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 600, "manual_reserve": 25}),
        )
        settings = service.get_quota_settings()
        assert settings.monthly_quota == 600
        assert settings.manual_reserve == 25

    def test_invalid_json_falls_back_to_defaults(self, service, fake):
        fake.set("admin_config:venue_monthly_budget", "not-json")
        settings = service.get_quota_settings()
        assert settings.monthly_quota == DEFAULT_MONTHLY_QUOTA

    def test_reserve_above_quota_is_clamped(self, service, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 100, "manual_reserve": 200}),
        )
        settings = service.get_quota_settings()
        assert settings.manual_reserve == 100

    def test_discovery_cap_with_zero_counter(self, service, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 500, "manual_reserve": 10}),
        )
        assert service.discovery_effective_cap_remaining() == 490

    def test_discovery_cap_clamps_at_zero(self, service, dao, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 100, "manual_reserve": 10}),
        )
        dao.increment_month("2026-05", 95)
        assert service.discovery_effective_cap_remaining() == 0

    def test_can_manual_add_at_quota_is_false(self, service, dao, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 500, "manual_reserve": 10}),
        )
        dao.increment_month("2026-05", 500)
        assert service.can_manual_add() is False

    def test_reserve_manual_slot_grants_then_releases(self, service):
        granted, snap = service.reserve_manual_slot()
        assert granted
        assert snap.month_counter == 1
        service.release_manual_slot()
        assert service.get_snapshot().month_counter == 0

    def test_reserve_manual_slot_denied_at_quota(self, service, dao, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 500, "manual_reserve": 10}),
        )
        dao.increment_month("2026-05", 500)
        granted, snap = service.reserve_manual_slot()
        assert not granted
        assert snap.manual_add_available == 0
        # Counter must not have been promoted past quota.
        assert dao.get_month_count("2026-05") == 500

    def test_reserve_can_use_reserve_when_discovery_filled(self, service, dao, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 500, "manual_reserve": 10}),
        )
        # Discovery filled its 490 slots; reserve still available.
        dao.increment_month("2026-05", 490)
        assert service.discovery_effective_cap_remaining() == 0
        granted, snap = service.reserve_manual_slot()
        assert granted
        assert snap.month_counter == 491

    def test_redis_failure_falls_back_to_defaults(self):
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("boom")
        svc = VenueBudgetService(
            redis_client=broken,
            budget_dao=MagicMock(),
            year_month_provider=lambda: "2026-05",
        )
        settings = svc.get_quota_settings()
        assert settings.monthly_quota == DEFAULT_MONTHLY_QUOTA

    def test_snapshot_exposes_full_state(self, service, dao, fake):
        fake.set(
            "admin_config:venue_monthly_budget",
            json.dumps({"monthly_quota": 500, "manual_reserve": 10}),
        )
        dao.increment_month("2026-05", 100)
        snap = service.get_snapshot()
        assert snap.year_month == "2026-05"
        assert snap.month_counter == 100
        assert snap.discovery_effective_cap_remaining == 390
        assert snap.manual_add_available == 400
