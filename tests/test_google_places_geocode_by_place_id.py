"""Unit coverage for GooglePlacesAPIClient.geocode_by_place_id —
plans/260906_address-components-backfill.md, Phase 3. Exercises the REAL
client + HTTP layer (respx intercepts only the transport), so the request
shape (query-string `key=` auth, the legacy Geocoding endpoint, not Places
(New)) is what the client code actually builds, not a re-statement of it.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.api.google_places_client import (
    GOOGLE_GEOCODING_API_BASE,
    GoogleGeocodingError,
    GooglePlacesAPIClient,
)

_PLACE_ID = "ChIJvacCasaBacurau"


def _client() -> GooglePlacesAPIClient:
    return GooglePlacesAPIClient(api_key="test-key")


def test_success_normalizes_components_to_longtext_and_types():
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={
                "status": "OK",
                "results": [{
                    "address_components": [
                        {"long_name": "Santo Amaro", "short_name": "Santo Amaro",
                         "types": ["sublocality_level_1", "political"]},
                        {"long_name": "Recife", "short_name": "Recife",
                         "types": ["locality", "political"]},
                    ],
                }],
            })
        )
        result = asyncio.run(_client().geocode_by_place_id(_PLACE_ID))
    assert result == [
        {"longText": "Santo Amaro", "types": ["sublocality_level_1", "political"]},
        {"longText": "Recife", "types": ["locality", "political"]},
    ]


def test_zero_results_returns_none_not_an_error():
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
        )
        result = asyncio.run(_client().geocode_by_place_id(_PLACE_ID))
    assert result is None


def test_not_found_returns_none_not_an_error():
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={"status": "NOT_FOUND", "results": []})
        )
        result = asyncio.run(_client().geocode_by_place_id(_PLACE_ID))
    assert result is None


def test_empty_results_with_ok_status_returns_none():
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": []})
        )
        result = asyncio.run(_client().geocode_by_place_id(_PLACE_ID))
    assert result is None


def test_result_with_no_address_components_returns_empty_list():
    """Google answered (a real place) but published nothing to map — a
    normal, if unlikely, outcome, distinct from a genuine zero-result."""
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": [{}]})
        )
        result = asyncio.run(_client().geocode_by_place_id(_PLACE_ID))
    assert result == []


@pytest.mark.parametrize("status", ["REQUEST_DENIED", "OVER_QUERY_LIMIT", "INVALID_REQUEST", "UNKNOWN_ERROR"])
def test_non_ok_non_zero_status_raises_geocoding_error(status):
    """A permission/quota/API problem must never look like a genuine
    no-match — the caller (the backfill service) needs to count this as
    api_error, not silently mark the venue unresolvable."""
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={"status": status, "results": []})
        )
        with pytest.raises(GoogleGeocodingError):
            asyncio.run(_client().geocode_by_place_id(_PLACE_ID))


def test_http_error_status_raises_geocoding_error():
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(return_value=httpx.Response(403))
        with pytest.raises(GoogleGeocodingError):
            asyncio.run(_client().geocode_by_place_id(_PLACE_ID))


def test_connection_error_raises_geocoding_error():
    with respx.mock:
        respx.get(GOOGLE_GEOCODING_API_BASE).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(GoogleGeocodingError):
            asyncio.run(_client().geocode_by_place_id(_PLACE_ID))


def test_uses_query_string_key_auth_not_header():
    """The legacy Geocoding API is a DIFFERENT REST family from Places
    (New): auth travels in the query string, never the X-Goog-Api-Key
    header every other method on this client uses."""
    with respx.mock:
        route = respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": []})
        )
        asyncio.run(_client().geocode_by_place_id(_PLACE_ID))
        request = route.calls.last.request
        assert request.url.params.get("key") == "test-key"
        assert "X-Goog-Api-Key" not in request.headers


def test_accepts_full_resource_name_and_sends_the_bare_id():
    with respx.mock:
        route = respx.get(GOOGLE_GEOCODING_API_BASE).mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": []})
        )
        asyncio.run(_client().geocode_by_place_id(f"places/{_PLACE_ID}"))
        request = route.calls.last.request
        assert request.url.params.get("place_id") == _PLACE_ID
