"""The archival tap inside BestTimeAPIClient.

Two things are under test: every endpoint lands in the right dataset (dataset
names are a storage contract — renaming one splits its table's history), and a
recorder that misbehaves cannot change a single BestTime return value or
exception. The two BestTime paths guarded most carefully here are the ones that
have already caused production incidents: the 404-empty filter envelope and the
monthly-cap 429 on create.
"""
import httpx
import pytest

from app.api.besttime_client import BestTimeAPIClient
from app.models import VenueFilterParams

BASE_URL = "https://besttime.app/api/v1"


class _RecordingWriter:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)
        return True

    def datasets(self):
        return [r["dataset"] for r in self.records]


class _ExplodingWriter:
    def record(self, **kwargs):
        raise RuntimeError("the data lake writer is broken")


def _client(datalake=None):
    return BestTimeAPIClient(
        base_url=BASE_URL,
        api_key_public="pub_test",
        api_key_private="pri_test",
        datalake=datalake,
    )


def _program(client, status, body):
    async def _request(**kwargs):
        return httpx.Response(
            status_code=status, json=body, request=httpx.Request("GET", BASE_URL)
        )

    client.client.request = _request


def _live_body(venue_id="ven_1"):
    return {
        "status": "OK",
        "venue_info": {"venue_id": venue_id},
        "analysis": {
            "venue_live_busyness": 50,
            "venue_live_busyness_available": True,
        },
    }


class TestDatasetMapping:
    async def test_live_forecast(self):
        writer = _RecordingWriter()
        client = _client(writer)
        _program(client, 200, _live_body())
        await client.get_live_forecast(venue_id="ven_1")
        assert writer.datasets() == ["live_forecast"]
        assert writer.records[0]["endpoint"] == "/forecasts/live"
        assert writer.records[0]["venue_id"] == "ven_1"

    async def test_week_raw_forecast(self):
        writer = _RecordingWriter()
        client = _client(writer)
        _program(
            client,
            200,
            {"status": "OK", "venue_id": "ven_1", "window": {}, "analysis": {"week_raw": []}},
        )
        await client.get_week_raw_forecast("ven_1")
        assert writer.datasets() == ["week_raw_forecast"]

    async def test_venue_filter(self):
        writer = _RecordingWriter()
        client = _client(writer)
        _program(client, 200, {"status": "OK", "venues": [], "venues_n": 0})
        await client.venue_filter(VenueFilterParams(lat=-8.05, lng=-34.88, radius=1000))
        assert writer.datasets() == ["venue_filter"]

    async def test_venue_create(self):
        writer = _RecordingWriter()
        client = _client(writer)
        _program(
            client,
            200,
            {
                "status": "OK",
                "venue_info": {
                    "venue_id": "ven_new",
                    "venue_name": "New",
                    "venue_address": "addr",
                },
            },
        )
        await client.add_venue_to_account("New", "addr")
        assert writer.datasets() == ["venue_create"]

    async def test_account_inventory_records_every_page(self):
        writer = _RecordingWriter()
        client = _client(writer)
        pages = [[{"venue_id": "a"}, {"venue_id": "b"}], [{"venue_id": "c"}]]

        async def _request(**kwargs):
            page = int(kwargs["params"]["page"])
            body = pages[page] if page < len(pages) else []
            return httpx.Response(
                status_code=200, json=body, request=httpx.Request("GET", BASE_URL)
            )

        client.client.request = _request
        [v async for v in client.list_account_inventory(page_size=2)]

        assert writer.datasets() == ["account_inventory", "account_inventory"]


class TestErrorsAreArchived:
    async def test_a_timeout_is_recorded_as_an_error(self):
        writer = _RecordingWriter()
        client = _client(writer)

        async def _request(**kwargs):
            raise httpx.TimeoutException("timed out")

        client.client.request = _request

        with pytest.raises(httpx.TimeoutException):
            await client.get_live_forecast(venue_id="ven_1")

        assert writer.records[0]["outcome"] == "error"
        assert writer.records[0]["dataset"] == "live_forecast"
        assert "timeout" in writer.records[0]["error"]
        assert writer.records[0]["payload"] is None

    async def test_a_rejected_create_is_still_archived(self):
        writer = _RecordingWriter()
        client = _client(writer)
        _program(
            client,
            200,
            {"status": "Error", "message": "Could not geocode the venue address"},
        )
        result = await client.add_venue_to_account("Nowhere", "nowhere")

        assert result.status == "Error"
        assert writer.records[0]["dataset"] == "venue_create"
        assert writer.records[0]["outcome"] == "error"


class TestArchivalNeverChangesBehavior:
    """A defect in the writer must be invisible to every caller."""

    async def test_zero_match_filter_404_still_returns_an_empty_result(self):
        """BestTime answers a zero-match filter with HTTP 404 and a parseable
        body. Misreading that as a transport failure previously turned terminal
        'nothing nearby' into retryable 502s in production."""
        body = {
            "status": "Error",
            "venues": [],
            "message": "No venues found matching the filter criteria",
        }
        params = VenueFilterParams(lat=-8.05, lng=-34.88, radius=1000)

        without = _client(None)
        _program(without, 404, body)
        baseline = await without.venue_filter(params)

        with_broken_lake = _client(_ExplodingWriter())
        _program(with_broken_lake, 404, body)
        actual = await with_broken_lake.venue_filter(params)

        assert actual.venues == baseline.venues == []
        assert actual.venues_n == baseline.venues_n == 0
        assert actual.status == baseline.status

    async def test_monthly_cap_429_is_surfaced_unchanged(self):
        """The cap is a terminal quota state, never retried — and the tap must
        not disturb that."""
        body = {
            "status": "Error",
            "message": "You have reached the maximum number of monthly venues. "
            "The venue counter will reset next month.",
        }

        without = _client(None)
        _program(without, 429, body)
        baseline = await without.add_venue_to_account("Capped", "addr")

        with_broken_lake = _client(_ExplodingWriter())
        _program(with_broken_lake, 429, body)
        actual = await with_broken_lake.add_venue_to_account("Capped", "addr")

        assert actual.status == baseline.status == "Error"
        assert actual.message == baseline.message

    async def test_a_broken_writer_does_not_break_a_healthy_call(self):
        client = _client(_ExplodingWriter())
        _program(client, 200, _live_body())
        result = await client.get_live_forecast(venue_id="ven_1")
        assert result.status == "OK"

    async def test_a_broken_writer_does_not_swallow_a_real_error(self):
        client = _client(_ExplodingWriter())

        async def _request(**kwargs):
            raise httpx.ConnectError("no route to host")

        client.client.request = _request

        with pytest.raises(httpx.RequestError):
            await client.get_live_forecast(venue_id="ven_1")


class TestArchivalOff:
    async def test_no_writer_means_no_work(self):
        client = _client(None)
        _program(client, 200, _live_body())
        result = await client.get_live_forecast(venue_id="ven_1")
        assert result.status == "OK"
        assert client._datalake is None
