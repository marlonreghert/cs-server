"""Unit tests for the photo archive pipeline's v2 internals.

Covers the parts whose failure costs money or silently loses data, and which the
BDD scenarios can only assert end-to-end: configuration validation boundaries,
run-prefix ordering, selection caps, the rate limiter's arithmetic, backoff, and
the estimate.

The rate-limiter tests drive an injected clock and sleeper — they assert the
pacing exactly and never sleep, so they cannot flake under a loaded CI box.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.services.venue_photo_archive_service import (
    ELIGIBILITY_POINT_RADIUS,
    ELIGIBILITY_VENUE_IDS,
    InvalidArchiveConfig,
    InvalidArchivePath,
    MAX_RADIUS_KM,
    VenuePhotoArchiveService,
    new_run_id,
    parse_config,
    parse_venue_ids,
    run_prefix,
    validate_override,
)
from app.utils.rate_limiter import AsyncRateLimiter, backoff_delay, is_throttled

DEFAULTS = {"default_max_venues": 50, "default_max_photos": 5}


def cfg(**over):
    return parse_config(over or None, **DEFAULTS)


# ── configuration validation ─────────────────────────────────────────────────
class TestParseConfig:
    def test_defaults_are_small_enough_to_stay_inside_the_free_tier(self):
        c = cfg()
        assert c["max_venues"] == 50
        assert c["max_photos_per_venue"] == 5
        # The default click must cost at most this many billed requests.
        assert c["max_venues"] * c["max_photos_per_venue"] == 250

    def test_new_run_is_the_default_path_mode(self):
        assert cfg()["path_mode"] == "new_run"

    def test_legacy_sources_list_is_accepted(self):
        assert cfg(sources=["google_photos"])["source"] == "google_photos"

    def test_legacy_top_level_venue_ids_become_an_id_eligibility(self):
        c = cfg(venue_ids="a, b ,a")
        assert c["eligibility"]["mode"] == ELIGIBILITY_VENUE_IDS
        assert c["eligibility"]["venue_ids"] == ["a", "b"]  # de-duplicated, ordered

    def test_absent_eligibility_and_no_ids_means_all(self):
        assert cfg()["eligibility"]["mode"] == "all"

    @pytest.mark.parametrize("value", [0, -1, "nope", 100_001])
    def test_max_venues_rejects_out_of_range(self, value):
        with pytest.raises(InvalidArchiveConfig):
            cfg(max_venues=value)

    @pytest.mark.parametrize("value", [0, -3, 101])
    def test_max_photos_rejects_out_of_range(self, value):
        with pytest.raises(InvalidArchiveConfig):
            cfg(max_photos_per_venue=value)

    def test_unknown_source_is_rejected(self):
        with pytest.raises(InvalidArchiveConfig):
            cfg(source="instagram_posts")

    def test_unknown_path_mode_is_rejected(self):
        with pytest.raises(InvalidArchiveConfig):
            cfg(path_mode="whenever")

    def test_skip_none_without_overwrite_is_rejected(self):
        # Disabling the skip is how a run re-buys the catalog; it must take two
        # deliberate settings, never one dropdown.
        with pytest.raises(InvalidArchiveConfig):
            cfg(skip_scope="none")

    def test_skip_none_with_overwrite_is_allowed(self):
        assert cfg(skip_scope="none", overwrite=True)["skip_scope"] == "none"

    def test_id_eligibility_requires_at_least_one_id(self):
        with pytest.raises(InvalidArchiveConfig):
            cfg(eligibility={"mode": ELIGIBILITY_VENUE_IDS, "venue_ids": " , "})

    @pytest.mark.parametrize("radius", [0, -1, MAX_RADIUS_KM + 1, "wide"])
    def test_radius_bounds_are_enforced(self, radius):
        with pytest.raises(InvalidArchiveConfig):
            cfg(eligibility={
                "mode": ELIGIBILITY_POINT_RADIUS, "lat": -8.0, "lon": -34.9,
                "radius_km": radius,
            })

    @pytest.mark.parametrize("lat,lon", [(-91, 0), (91, 0), (0, -181), (0, 181)])
    def test_coordinate_bounds_are_enforced(self, lat, lon):
        with pytest.raises(InvalidArchiveConfig):
            cfg(eligibility={
                "mode": ELIGIBILITY_POINT_RADIUS, "lat": lat, "lon": lon,
                "radius_km": 5,
            })

    def test_a_valid_geo_eligibility_round_trips(self):
        c = cfg(eligibility={
            "mode": ELIGIBILITY_POINT_RADIUS, "lat": -8.05, "lon": -34.88,
            "radius_km": 2.5,
        })
        assert c["eligibility"] == {
            "mode": ELIGIBILITY_POINT_RADIUS, "lat": -8.05, "lon": -34.88,
            "radius_km": 2.5,
        }

    def test_config_errors_are_catchable_as_path_errors(self):
        # Callers that already guard InvalidArchivePath must keep working.
        assert issubclass(InvalidArchiveConfig, InvalidArchivePath)


# ── run prefixes ─────────────────────────────────────────────────────────────
class TestRunPrefix:
    def test_layout_is_hive_style_key_value(self):
        when = datetime(2026, 7, 27, 5, 9, 18, tzinfo=timezone.utc)
        prefix = run_prefix("google_photos", when, "abc123")
        assert prefix == (
            "retrieved/source=google_photos/year=2026/month=07/day=27/"
            "run_id=abc123/"
        )
        for segment in prefix.strip("/").split("/")[1:]:
            assert "=" in segment

    def test_prefixes_sort_chronologically_as_plain_strings(self):
        # This is the property that lets "latest run" be found by LISTING alone —
        # the pipeline's IAM role cannot GetObject, so it can never read a pointer.
        times = [
            datetime(2026, 7, 9, 23, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 12, 1, 5, 0, 0, tzinfo=timezone.utc),
            datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        ]
        prefixes = [run_prefix("s", t, new_run_id(t)) for t in times]
        assert prefixes == sorted(prefixes)

    def test_runs_within_one_day_still_sort_by_time(self):
        """The run id is the ONLY thing separating two runs on the same day.

        A random id sorts randomly and picks the wrong "latest" about two thirds
        of the time, which silently breaks append_latest and the skip_scope cost
        gate. This is the regression guard for using a time-ordered id.
        """
        day = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        moments = [day.replace(second=s) for s in (1, 2, 3, 30, 59)]
        prefixes = [run_prefix("s", m, new_run_id(m)) for m in moments]
        assert prefixes == sorted(prefixes)
        assert max(prefixes) == prefixes[-1], "latest-by-listing picked the wrong run"

    def test_run_ids_are_unique_within_a_millisecond(self):
        when = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        ids = {new_run_id(when) for _ in range(5000)}
        assert len(ids) == 5000

    def test_a_run_id_avoids_ambiguous_characters(self):
        # Crockford base32: no I, L, O or U, so an id copied out of a bucket
        # listing cannot be mistranscribed.
        assert not (set(new_run_id()) & set("ILOU"))

    def test_override_traversal_is_rejected_on_the_normalised_path(self):
        with pytest.raises(InvalidArchivePath):
            validate_override("media/../raw/")
        with pytest.raises(InvalidArchivePath):
            validate_override("raw/besttime/")
        with pytest.raises(InvalidArchivePath):
            validate_override("")
        assert validate_override("/media/manual/x") == "media/manual/x/"

    def test_parse_venue_ids_accepts_a_list_or_a_string(self):
        assert parse_venue_ids(["a", " b ", "a"]) == ["a", "b"]
        assert parse_venue_ids("a,b, ,a") == ["a", "b"]
        assert parse_venue_ids(None) == []


# ── selection ────────────────────────────────────────────────────────────────
class _Dao:
    def __init__(self, venues):
        self._venues = venues  # {id: (lat, lng)}

    def list_active_venue_ids(self):
        return list(self._venues)

    def get_venue(self, venue_id):
        coords = self._venues.get(venue_id)
        if coords is None:
            return None
        return type("V", (), {"venue_lat": coords[0], "venue_lng": coords[1]})()

    def get_vibe_attributes(self, venue_id):
        return None


def _service(venues, **kw):
    return VenuePhotoArchiveService(
        google_places_client=None, venue_dao=_Dao(venues), media_store=None,
        downloader=object(), default_max_venues=50, **kw,
    )


class TestSelection:
    def test_cap_truncates_and_reports_what_it_dropped(self):
        svc = _service({f"v{i}": (-8.05, -34.88) for i in range(40)})
        selected, unknown, eligible = svc.select_venues(cfg(max_venues=10))
        assert len(selected) == 10
        assert eligible == 40  # the caller reports truncated_from from this
        assert unknown == []

    def test_unknown_ids_are_reported_not_fatal(self):
        svc = _service({"a": (-8.05, -34.88), "b": (-8.05, -34.88)})
        selected, unknown, _ = svc.select_venues(
            cfg(eligibility={"mode": ELIGIBILITY_VENUE_IDS, "venue_ids": "a,zz,b"})
        )
        assert selected == ["a", "b"]
        assert unknown == ["zz"]

    def test_radius_selects_only_venues_inside(self):
        svc = _service({
            "near": (-8.0505, -34.8805),
            "far": (-8.95, -34.88),          # ~100 km away
        })
        selected, _, _ = svc.select_venues(cfg(eligibility={
            "mode": ELIGIBILITY_POINT_RADIUS, "lat": -8.05, "lon": -34.88,
            "radius_km": 2,
        }))
        assert selected == ["near"]

    def test_a_venue_without_coordinates_is_never_inside_a_radius(self):
        svc = _service({"nowhere": None})
        selected, _, _ = svc.select_venues(cfg(eligibility={
            "mode": ELIGIBILITY_POINT_RADIUS, "lat": -8.05, "lon": -34.88,
            "radius_km": 500,
        }))
        assert selected == []


# ── estimate ─────────────────────────────────────────────────────────────────
class _NoStore:
    async def list_run_prefixes(self, source):
        return []

    async def list_day_partitions(self, source):
        return []


class TestEstimate:
    def _svc(self, n):
        svc = _service({f"v{i}": (-8.05, -34.88) for i in range(n)},
                       cost_per_1k_usd=7.0)
        svc.media_store = _NoStore()
        return svc

    def test_upper_bound_calls_and_cost(self):
        est = asyncio.run(self._svc(20).estimate(
            {"max_venues": 100, "max_photos_per_venue": 5}
        ))
        assert est["venues_selected"] == 20
        # 20 x (1 Place Details + 5 photo requests). The Details call was
        # previously omitted, understating every Google run by a third.
        assert est["est_google_calls"] == 120
        assert est["est_cost_usd"] == pytest.approx(0.84)  # 120 / 1000 * $7

    def test_cost_is_zero_when_nothing_is_selected(self):
        est = asyncio.run(self._svc(0).estimate({"max_photos_per_venue": 5}))
        assert est["est_google_calls"] == 0
        assert est["est_cost_usd"] == 0

    def test_the_caveat_states_the_price_is_unverified(self):
        est = asyncio.run(self._svc(1).estimate({}))
        assert "estimate" in est["caveat"].lower()
        assert "verified" in est["caveat"].lower()
        assert est["assumptions"]

    def test_the_cap_bounds_the_estimate(self):
        est = asyncio.run(self._svc(500).estimate(
            {"max_venues": 10, "max_photos_per_venue": 2}
        ))
        assert est["venues_selected"] == 10
        assert est["est_google_calls"] == 30   # 10 x (1 Details + 2 photos)


# ── rate limiting and backoff ────────────────────────────────────────────────
class _Clock:
    """Manual clock: sleeping advances time, so pacing is exact and instant."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class TestRateLimiter:
    def test_burst_is_admitted_without_waiting(self):
        clock = _Clock()
        limiter = AsyncRateLimiter(10, burst=3, clock=clock, sleeper=clock.sleep)

        async def go():
            return [await limiter.acquire() for _ in range(3)]

        assert asyncio.run(go()) == [0.0, 0.0, 0.0]
        assert clock.slept == []

    def test_the_request_after_the_burst_waits_exactly_one_interval(self):
        clock = _Clock()
        limiter = AsyncRateLimiter(10, burst=1, clock=clock, sleeper=clock.sleep)

        async def go():
            await limiter.acquire()
            return await limiter.acquire()

        assert asyncio.run(go()) == pytest.approx(0.1)  # 1 token / 10 per second

    def test_tokens_refill_over_elapsed_time(self):
        clock = _Clock()
        limiter = AsyncRateLimiter(10, burst=1, clock=clock, sleeper=clock.sleep)

        async def go():
            await limiter.acquire()
            clock.now += 1.0          # a second passes doing other work
            return await limiter.acquire()

        assert asyncio.run(go()) == 0.0

    def test_a_request_larger_than_the_bucket_cannot_deadlock(self):
        clock = _Clock()
        limiter = AsyncRateLimiter(10, burst=2, clock=clock, sleeper=clock.sleep)
        assert asyncio.run(limiter.acquire(50)) >= 0.0

    def test_a_non_positive_rate_is_rejected(self):
        with pytest.raises(ValueError):
            AsyncRateLimiter(0)


class TestThrottleDetection:
    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_throttling_and_transient_server_errors_are_retryable(self, code):
        err = type("E", (Exception,), {"status_code": code})()
        assert is_throttled(err)

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_client_errors_are_not_retried(self, code):
        err = type("E", (Exception,), {"status_code": code})()
        assert not is_throttled(err)

    def test_the_httpx_response_shape_is_understood(self):
        response = type("R", (), {"status_code": 429})()
        err = type("E", (Exception,), {"response": response})()
        assert is_throttled(err)

    def test_an_unrelated_error_is_not_throttling(self):
        assert not is_throttled(RuntimeError("boom"))


class TestBackoff:
    def test_delay_grows_exponentially_and_is_capped(self):
        assert backoff_delay(1, base=0.5) == 0.5
        assert backoff_delay(2, base=0.5) == 1.0
        assert backoff_delay(3, base=0.5) == 2.0
        assert backoff_delay(99, base=0.5, cap=30.0) == 30.0

    def test_jitter_only_ever_extends_the_delay(self):
        assert backoff_delay(1, base=1.0, jitter=0.5) == 1.5
        assert backoff_delay(1, base=1.0, jitter=-1.0) == 1.0  # clamped
