"""Unit tests for the serve-time live-busyness freshness gate.

Covers the pure pieces the BDD does not pin at the edges: gmttime parsing across
formats, the fresh/stale/unparseable boundary, and admin-override resolution.
"""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services.live_freshness import (
    FRESH,
    STALE,
    UNPARSEABLE,
    classify_live_freshness,
    parse_gmttime,
    resolve_max_age_minutes,
)


class _FakeVenueInfo:
    def __init__(self, gmttime):
        self.venue_current_gmttime = gmttime


class _FakeLive:
    def __init__(self, gmttime):
        self.venue_info = _FakeVenueInfo(gmttime)


class _FakeAdminConfig:
    def __init__(self, value=None, raise_on_get=False):
        self._value = value
        self._raise = raise_on_get

    def get(self, key):
        if self._raise:
            raise RuntimeError("mirror unavailable")
        return self._value


# ── parse_gmttime ─────────────────────────────────────────────────────────────
def test_parse_gmttime_iso_with_z():
    dt = parse_gmttime("2026-07-01T12:00:00Z")
    assert dt == datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_gmttime_iso_naive_is_treated_as_utc():
    dt = parse_gmttime("2026-07-01T12:00:00")
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 12


def test_parse_gmttime_besttime_display_format():
    dt = parse_gmttime("Wednesday 2026-07-01 03:07AM")
    assert dt == datetime(2026, 7, 1, 3, 7, tzinfo=timezone.utc)


def test_parse_gmttime_besttime_24h_format():
    dt = parse_gmttime("Wednesday 2026-07-01 15:07:00")
    assert dt == datetime(2026, 7, 1, 15, 7, 0, tzinfo=timezone.utc)


def test_parse_gmttime_garbage_returns_none():
    assert parse_gmttime("definitely-not-a-timestamp") is None


def test_parse_gmttime_empty_and_non_string_return_none():
    assert parse_gmttime("") is None
    assert parse_gmttime("   ") is None
    assert parse_gmttime(None) is None
    assert parse_gmttime(12345) is None


# ── classify_live_freshness ───────────────────────────────────────────────────
def test_classify_fresh_just_under_window():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    lf = _FakeLive((now - timedelta(minutes=1439)).isoformat())
    assert classify_live_freshness(lf, now, timedelta(minutes=1440)) == FRESH


def test_classify_stale_over_window():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    lf = _FakeLive((now - timedelta(minutes=1500)).isoformat())
    assert classify_live_freshness(lf, now, timedelta(minutes=1440)) == STALE


def test_classify_boundary_exactly_at_window_is_stale():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    lf = _FakeLive((now - timedelta(minutes=1440)).isoformat())
    # fresh iff age < max_age; age == max_age is stale.
    assert classify_live_freshness(lf, now, timedelta(minutes=1440)) == STALE


def test_classify_unparseable_gmttime():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert classify_live_freshness(_FakeLive("nope"), now, timedelta(minutes=1440)) == UNPARSEABLE
    assert classify_live_freshness(_FakeLive(""), now, timedelta(minutes=1440)) == UNPARSEABLE


# ── resolve_max_age_minutes ───────────────────────────────────────────────────
def test_resolve_defaults_when_no_admin_service():
    assert resolve_max_age_minutes(None) == settings.live_freshness_max_age_minutes


def test_resolve_uses_valid_admin_override():
    assert resolve_max_age_minutes(_FakeAdminConfig(15)) == 15


def test_resolve_accepts_stringified_int_override():
    assert resolve_max_age_minutes(_FakeAdminConfig("15")) == 15


def test_resolve_falls_back_on_non_numeric_override():
    assert resolve_max_age_minutes(_FakeAdminConfig("not-a-number")) == (
        settings.live_freshness_max_age_minutes
    )


def test_resolve_falls_back_on_out_of_bounds_override():
    assert resolve_max_age_minutes(_FakeAdminConfig(0)) == settings.live_freshness_max_age_minutes
    assert resolve_max_age_minutes(_FakeAdminConfig(10_000_000)) == (
        settings.live_freshness_max_age_minutes
    )


def test_resolve_falls_back_when_absent():
    assert resolve_max_age_minutes(_FakeAdminConfig(None)) == (
        settings.live_freshness_max_age_minutes
    )


def test_resolve_falls_back_when_admin_read_raises():
    assert resolve_max_age_minutes(_FakeAdminConfig(raise_on_get=True)) == (
        settings.live_freshness_max_age_minutes
    )
