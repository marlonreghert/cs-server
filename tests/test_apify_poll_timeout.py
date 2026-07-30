"""Unit tests for Apify poll-budget exhaustion and the continuation window.

These cover the edges the BDD suite cannot express, because they are internal to
the client: what `_poll_run` returns, how many polls it spends, and which of the
four terminal states short-circuit it.

Time is not faked. `POLL_INTERVAL_SECONDS` is patched to a millisecond so the real
loop runs for real, just quickly — the alternative, stubbing `asyncio.sleep`,
would also stub it for anything else the loop awaits.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY

import app.api.apify_gmaps_extractor_client as mod
from app.api.apify_gmaps_extractor_client import (
    POLL_BUDGET_EXHAUSTED,
    ApifyGMapsExtractorClient,
    ApifyPollTimeoutError,
)
from app.api.apify_instagram_client import ApifyCreditExhaustedError

INTERVAL = 0.001
BASE = 4
ENDPOINT = "gmaps_archive_photos"


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Http:
    """Minimal Apify transport whose run status is a function of poll count."""

    def __init__(self, statuses, items=None, credit_at=None):
        # `statuses` is consumed one per poll; the last value repeats forever.
        self.statuses = list(statuses)
        self.items = items if items is not None else [{"title": "V", "imageUrls": []}]
        self.credit_at = credit_at
        self.starts = 0
        self.polls = 0
        self.dataset_reads = 0

    async def post(self, url, params=None, json=None, **kw):
        self.starts += 1
        return _Resp({"data": {"id": "run_x", "defaultDatasetId": "ds_x"}})

    async def get(self, url, params=None, **kw):
        if "/actor-runs/" in url:
            self.polls += 1
            if self.credit_at is not None and self.polls >= self.credit_at:
                return _Resp({"error": "credits"}, status_code=402)
            idx = min(self.polls - 1, len(self.statuses) - 1)
            return _Resp({"data": {"status": self.statuses[idx]}})
        self.dataset_reads += 1
        return _Resp(self.items)


def _client(http, continuation_seconds=0.0):
    client = ApifyGMapsExtractorClient(
        api_token="t", poll_continuation_seconds=continuation_seconds
    )
    client.client = http  # type: ignore[assignment]
    return client


def _run(coro):
    return asyncio.run(coro)


def _poll(http, continuation_seconds=0.0):
    client = _client(http, continuation_seconds)
    with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
            patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
        return _run(client._poll_run("run_x", ENDPOINT))


def _metric(name, **labels):
    v = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if v is None else float(v)


class TestPollRunReturnsLastNonTerminalStatus:
    def test_exhausted_budget_reports_the_status_it_was_stuck_in(self):
        status, last = _poll(_Http(["READY"]))
        assert status == POLL_BUDGET_EXHAUSTED
        assert last == "READY"

    def test_running_is_distinguished_from_ready(self):
        _, last = _poll(_Http(["RUNNING"]))
        assert last == "RUNNING"

    def test_the_latest_non_terminal_status_wins_not_the_first(self):
        # A run that starts queued and then begins working must be reported as
        # RUNNING: the remedy for a slow run is more time, for a queued one less
        # concurrency, and the first observation would prescribe the wrong fix.
        _, last = _poll(_Http(["READY", "READY", "RUNNING", "RUNNING"]))
        assert last == "RUNNING"

    def test_our_sentinel_is_not_apifys_timed_out(self):
        # Apify's TIMED-OUT is a real terminal answer from the actor; ours means
        # only that we stopped watching. Collapsing both into "TIMED-OUT" left the
        # caller unable to tell a dead run from a live one.
        status, _ = _poll(_Http(["TIMED-OUT"]))
        assert status == "TIMED-OUT"
        assert status != POLL_BUDGET_EXHAUSTED


class TestTerminalStatusesShortCircuit:
    @pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"])
    def test_terminal_status_returns_immediately(self, terminal):
        http = _Http([terminal])
        status, _ = _poll(http, continuation_seconds=BASE * INTERVAL)
        assert status == terminal
        assert http.polls == 1, "a terminal status must not be polled again"

    def test_a_failed_run_does_not_count_as_a_poll_timeout(self):
        before = _metric("apify_api_errors_total", endpoint=ENDPOINT, error_type="timeout")
        _poll(_Http(["FAILED"]))
        after = _metric("apify_api_errors_total", endpoint=ENDPOINT, error_type="timeout")
        assert after == before, "a run that failed on Apify's side is not a timeout"


class TestContinuationWindow:
    def test_disabled_by_default_spends_only_the_base_budget(self):
        http = _Http(["RUNNING"])
        _poll(http)
        assert http.polls == BASE

    def test_continuation_extends_the_budget_without_starting_a_new_run(self):
        http = _Http(["RUNNING"])
        _poll(http, continuation_seconds=BASE * INTERVAL)
        assert http.polls == BASE * 2
        assert http.starts == 0, "polling must never start an actor run"

    def test_a_run_that_lands_during_the_continuation_succeeds(self):
        # Non-terminal through the base budget, terminal one poll into the
        # continuation — the exact shape of the 35 venues that were dropped.
        statuses = ["RUNNING"] * BASE + ["SUCCEEDED"]
        status, _ = _poll(_Http(statuses), continuation_seconds=BASE * INTERVAL)
        assert status == "SUCCEEDED"

    def test_a_permanently_queued_run_terminates_rather_than_hanging(self):
        http = _Http(["READY"])
        status, last = _poll(http, continuation_seconds=BASE * INTERVAL)
        assert status == POLL_BUDGET_EXHAUSTED
        assert last == "READY"
        assert http.polls == BASE * 2, "the continuation must be bounded"

    def test_continuation_is_rounded_down_to_whole_polls(self):
        # Half a poll interval buys no extra polls; it must not round up into one.
        http = _Http(["RUNNING"])
        _poll(http, continuation_seconds=INTERVAL / 2)
        assert http.polls == BASE


class TestFetchVenuePhotosSignalsTimeoutDistinctly:
    def _fetch(self, http, continuation_seconds=0.0):
        client = _client(http, continuation_seconds)
        with patch.object(mod, "MAX_POLL_ATTEMPTS", BASE), \
                patch.object(mod, "POLL_INTERVAL_SECONDS", INTERVAL):
            return _run(client.fetch_venue_photos("Bar do Cuscuz, Recife"))

    def test_timeout_raises_rather_than_returning_none(self):
        # The whole point: a bare None was indistinguishable from "no such venue",
        # so 35 mid-scrape venues were filed as absent from Google Maps.
        with pytest.raises(ApifyPollTimeoutError) as excinfo:
            self._fetch(_Http(["RUNNING"]))
        assert excinfo.value.last_status == "RUNNING"

    def test_an_empty_dataset_still_returns_none(self):
        assert self._fetch(_Http(["SUCCEEDED"], items=[])) is None

    def test_a_failed_run_still_returns_none(self):
        assert self._fetch(_Http(["FAILED"])) is None

    def test_timeout_observes_the_call_duration(self):
        # The old early return skipped observe(), so 89 real calls surfaced as 54
        # observations and the slow tail was missing from the latency data.
        before = _metric("apify_api_call_duration_seconds_count", endpoint=ENDPOINT)
        with pytest.raises(ApifyPollTimeoutError):
            self._fetch(_Http(["RUNNING"]))
        after = _metric("apify_api_call_duration_seconds_count", endpoint=ENDPOINT)
        assert after == before + 1

    def test_timeout_records_the_status_label(self):
        before = _metric("apify_poll_timeouts_total", endpoint=ENDPOINT, last_status="READY")
        with pytest.raises(ApifyPollTimeoutError):
            self._fetch(_Http(["READY"]))
        after = _metric("apify_poll_timeouts_total", endpoint=ENDPOINT, last_status="READY")
        assert after == before + 1

    def test_only_one_actor_run_is_started_for_a_timed_out_venue(self):
        http = _Http(["RUNNING"])
        with pytest.raises(ApifyPollTimeoutError):
            self._fetch(http, continuation_seconds=BASE * INTERVAL)
        assert http.starts == 1, "recovery must reuse the run already paid for"

    def test_a_recovered_venue_returns_its_photos(self):
        statuses = ["RUNNING"] * BASE + ["SUCCEEDED"]
        http = _Http(statuses, items=[{
            "title": "V", "imageUrls": ["https://lh3/x1", "https://lh3/x2"],
        }])
        result = self._fetch(http, continuation_seconds=BASE * INTERVAL)
        assert result is not None
        assert len(result["photos"]) == 2
        assert http.starts == 1


class TestCreditExhaustionDuringPolling:
    def test_a_402_mid_poll_stops_the_run(self):
        # The balance can empty after the run starts, not only at start-run. Left
        # unhandled, the poll loop read the 402 body as an unknown status and kept
        # polling until the budget ran out.
        with pytest.raises(ApifyCreditExhaustedError):
            _poll(_Http(["RUNNING"], credit_at=2))

    def test_exhaustion_is_not_reported_as_a_poll_timeout(self):
        before = _metric("apify_api_errors_total", endpoint=ENDPOINT, error_type="timeout")
        with pytest.raises(ApifyCreditExhaustedError):
            _poll(_Http(["RUNNING"], credit_at=2))
        after = _metric("apify_api_errors_total", endpoint=ENDPOINT, error_type="timeout")
        assert after == before
