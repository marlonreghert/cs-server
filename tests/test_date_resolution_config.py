"""Unit tests for app/models/date_resolution_config.py. See
plans/260812_event-attribution-and-dates.md §D.

Mirrors tests/test_menu_item_lifecycle.py's TestValidateMenuExpiryDaysConfig
/ TestLoadMenuExpiryDays exactly — the SAME Redis-mirror-with-fallback shape
this module deliberately copies.
"""
import json

import pytest

from app.models.date_resolution_config import (
    ADMIN_CONFIG_DATE_YEAR_ROLL_GRACE_DAYS_KEY,
    DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS,
    load_date_year_roll_grace_days,
    validate_date_year_roll_grace_days_config,
)
from app.services.event_date_resolver import DEFAULT_YEAR_ROLL_GRACE_DAYS


class _FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def get(self, key):
        return self.store.get(key)


def test_the_default_matches_the_resolvers_own_default():
    """The two constants live in different modules (the resolver stays
    pure, no I/O — see its own module docstring) and must never drift, or
    "the admin console shows 60" and "the resolver actually applies 60"
    could silently disagree."""
    assert DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS == DEFAULT_YEAR_ROLL_GRACE_DAYS


class TestValidateDateYearRollGraceDaysConfig:
    def test_a_positive_integer_is_accepted(self):
        assert validate_date_year_roll_grace_days_config(90) == 90

    def test_zero_is_accepted(self):
        """Unlike menu_expiry_days, zero is meaningful here -- it disables
        the grace window entirely, reverting to the pre-§D always-roll
        rule."""
        assert validate_date_year_roll_grace_days_config(0) == 0

    def test_a_negative_integer_is_rejected(self):
        with pytest.raises(ValueError):
            validate_date_year_roll_grace_days_config(-1)

    def test_a_bool_is_rejected(self):
        with pytest.raises(TypeError):
            validate_date_year_roll_grace_days_config(True)

    def test_a_non_integer_is_rejected(self):
        with pytest.raises(TypeError):
            validate_date_year_roll_grace_days_config("60")


class TestLoadDateYearRollGraceDays:
    def test_none_redis_falls_back_to_the_default(self):
        days, reason = load_date_year_roll_grace_days(None)
        assert days == DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS
        assert reason is None

    def test_missing_key_falls_back_to_the_default_without_a_fallback_reason(self):
        days, reason = load_date_year_roll_grace_days(_FakeRedis())
        assert days == DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS
        assert reason is None

    def test_reads_the_admin_override(self):
        redis = _FakeRedis({ADMIN_CONFIG_DATE_YEAR_ROLL_GRACE_DAYS_KEY: json.dumps(90)})
        days, reason = load_date_year_roll_grace_days(redis)
        assert days == 90
        assert reason is None

    def test_invalid_json_falls_back_to_the_default_with_a_reason(self):
        redis = _FakeRedis({ADMIN_CONFIG_DATE_YEAR_ROLL_GRACE_DAYS_KEY: "{not json"})
        days, reason = load_date_year_roll_grace_days(redis)
        assert days == DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS
        assert reason == "invalid_json"

    def test_invalid_shape_falls_back_to_the_default_with_a_reason(self):
        redis = _FakeRedis({ADMIN_CONFIG_DATE_YEAR_ROLL_GRACE_DAYS_KEY: json.dumps(-5)})
        days, reason = load_date_year_roll_grace_days(redis)
        assert days == DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS
        assert reason == "invalid_shape"
