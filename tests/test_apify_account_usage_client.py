"""Parsing coverage for ApifyAccountUsageClient.get_headroom_usd.

Written after the 2b trial refused with `headroom=None` on 2026-08-14. The
account-headroom GATE was already covered (see
tests/test_deep_review_crawl_service.py::TestAccountHeadroomGate) — but only
against a FAKED client, so the client's own response parsing had no test at
all, and the one field it read was on the wrong nesting level. The gate held
(it fail-closed and spent nothing), but the feature could never have worked.

Every payload below is the real shape returned by the live prod account on
2026-08-13/14, trimmed to the fields this client reads.
"""
import pytest

from app.api.apify_account_usage_client import ApifyAccountUsageClient


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Returns a queued response per URL substring, recording the calls."""

    def __init__(self, by_url):
        self._by_url = by_url
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(url)
        for fragment, payload in self._by_url.items():
            if fragment in url:
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected url {url}")


def _client(me_payload, usage_payload):
    client = ApifyAccountUsageClient(api_token="t")
    client.client = _FakeHttpClient(
        {"/users/me/usage/monthly": usage_payload, "/users/me": me_payload}
    )
    return client


# The real prod shape: the cap is nested under data.plan, NOT on data.
_REAL_ME = {
    "data": {
        "username": "extraordinary_wolfspider",
        "plan": {
            "id": "STARTER",
            "monthlyUsageCreditsUsd": 29,
            "maxMonthlyUsageUsd": 29,
        },
    }
}
_REAL_USAGE = {"data": {"totalUsageCreditsUsdBeforeVolumeDiscount": 12.45}}


async def test_reads_the_cap_from_the_nested_plan_object():
    """The regression this file exists for: data.plan.maxMonthlyUsageUsd."""
    headroom = await _client(_REAL_ME, _REAL_USAGE).get_headroom_usd()
    assert headroom == pytest.approx(29 - 12.45)


async def test_falls_back_to_a_top_level_cap_if_the_shape_ever_flattens():
    me = {"data": {"maxMonthlyUsageUsd": 100}}
    headroom = await _client(me, _REAL_USAGE).get_headroom_usd()
    assert headroom == pytest.approx(100 - 12.45)


async def test_nested_plan_wins_when_both_levels_are_present():
    me = {"data": {"maxMonthlyUsageUsd": 100, "plan": {"maxMonthlyUsageUsd": 29}}}
    headroom = await _client(me, _REAL_USAGE).get_headroom_usd()
    assert headroom == pytest.approx(29 - 12.45)


async def test_missing_cap_is_unknown_headroom_not_zero_usage():
    """An absent cap must read as None (refuse), never as "no limit"."""
    me = {"data": {"plan": {"id": "STARTER"}}}
    assert await _client(me, _REAL_USAGE).get_headroom_usd() is None


async def test_missing_usage_is_unknown_headroom():
    usage = {"data": {}}
    assert await _client(_REAL_ME, usage).get_headroom_usd() is None


async def test_a_raising_transport_is_unknown_headroom_never_an_exception():
    client = ApifyAccountUsageClient(api_token="t")

    class _Boom:
        async def get(self, url, params=None):
            raise RuntimeError("network down")

    client.client = _Boom()
    assert await client.get_headroom_usd() is None


async def test_zero_headroom_is_reported_as_zero_not_as_unknown():
    """0.0 is a real answer and must survive the `is None` guard."""
    usage = {"data": {"totalUsageCreditsUsdBeforeVolumeDiscount": 29}}
    assert await _client(_REAL_ME, usage).get_headroom_usd() == pytest.approx(0.0)
