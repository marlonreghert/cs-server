"""Unit tests for the serve-time live-busyness freshness gate.

Covers the pure pieces the BDD does not pin at the edges: gmttime parsing across
formats, the fresh/stale/unparseable verdict + returned age, refresh-cadence
resolution, and the dynamic window derivation (factor x interval, floored).
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
    resolve_refresh_minutes,
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


# ── classify_live_freshness → (verdict, age_minutes) ──────────────────────────
def test_classify_fresh_returns_verdict_and_age():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    lf = _FakeLive((now - timedelta(minutes=6)).isoformat())
    verdict, age = classify_live_freshness(lf, now, timedelta(minutes=10))
    assert verdict == FRESH
    assert age == 6.0


def test_classify_stale_over_window_reports_age():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    lf = _FakeLive((now - timedelta(minutes=12)).isoformat())
    verdict, age = classify_live_freshness(lf, now, timedelta(minutes=10))
    assert verdict == STALE
    assert age == 12.0


def test_classify_boundary_exactly_at_window_is_stale():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    lf = _FakeLive((now - timedelta(minutes=10)).isoformat())
    verdict, _ = classify_live_freshness(lf, now, timedelta(minutes=10))
    # fresh iff age < max_age; age == max_age is stale.
    assert verdict == STALE


def test_classify_unparseable_returns_none_age():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    for bad in ("nope", ""):
        verdict, age = classify_live_freshness(_FakeLive(bad), now, timedelta(minutes=10))
        assert verdict == UNPARSEABLE
        assert age is None


# ── resolve_refresh_minutes ───────────────────────────────────────────────────
def test_refresh_defaults_when_no_admin_service():
    assert resolve_refresh_minutes(None) == settings.venues_live_refresh_minutes


def test_refresh_reads_minutes_object_shape():
    assert resolve_refresh_minutes(_FakeAdminConfig({"minutes": 15})) == 15


def test_refresh_accepts_bare_int_and_str():
    assert resolve_refresh_minutes(_FakeAdminConfig(15)) == 15
    assert resolve_refresh_minutes(_FakeAdminConfig("15")) == 15


def test_refresh_falls_back_on_invalid_or_out_of_bounds():
    d = settings.venues_live_refresh_minutes
    assert resolve_refresh_minutes(_FakeAdminConfig({"minutes": "x"})) == d
    assert resolve_refresh_minutes(_FakeAdminConfig(0)) == d      # below MIN (1)
    assert resolve_refresh_minutes(_FakeAdminConfig(121)) == d    # above MAX (120)
    assert resolve_refresh_minutes(_FakeAdminConfig(None)) == d
    assert resolve_refresh_minutes(_FakeAdminConfig(raise_on_get=True)) == d


# ── resolve_max_age_minutes (dynamic: factor x interval, floored) ─────────────
def test_window_defaults_to_factor_times_default_interval():
    expected = max(
        settings.live_freshness_min_minutes,
        round(settings.live_freshness_refresh_factor * settings.venues_live_refresh_minutes),
    )
    assert resolve_max_age_minutes(None) == expected


def test_window_widens_with_a_slower_refresh():
    # 15-min refresh -> factor x -> wider window than the default cadence gives.
    assert resolve_max_age_minutes(_FakeAdminConfig({"minutes": 15})) == round(
        settings.live_freshness_refresh_factor * 15
    )


def test_window_is_floored_for_a_very_short_interval():
    # 1-min refresh -> factor x -> below the floor -> clamped to the minimum.
    assert resolve_max_age_minutes(_FakeAdminConfig({"minutes": 1})) == (
        settings.live_freshness_min_minutes
    )
