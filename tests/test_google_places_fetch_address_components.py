"""Unit coverage for GooglePlacesAPIClient.fetch_address_components —
plans/260906_address-components-via-place-details.md. Replaces
tests/test_google_places_geocode_by_place_id.py's ROLE as the address
backfill's Google-rung coverage (that file itself is kept, unmodified,
per the plan's correction (B): geocode_by_place_id is confirmed dead but
not removed, so its own tests still exercise its own contract).

Exercises the REAL client + HTTP layer (respx intercepts only the
transport), so the request shape (Places API (New), X-Goog-Api-Key header,
the minimal id,addressComponents mask) is what the client code actually
builds, not a re-statement of it.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.api.google_places_client import (
    ADDRESS_COMPONENTS_FIELDS_MASK,
    GOOGLE_PLACES_API_BASE,
    GoogleAddressLookupError,
    GooglePlacesAPIClient,
)

_PLACE_ID = "ChIJvacCasaBacurau"
_URL = f"{GOOGLE_PLACES_API_BASE}/places/{_PLACE_ID}"


def _client() -> GooglePlacesAPIClient:
    return GooglePlacesAPIClient(api_key="test-key")


def test_success_returns_address_components_verbatim():
    with respx.mock:
        respx.get(_URL).mock(
            return_value=httpx.Response(200, json={
                "id": _PLACE_ID,
                "addressComponents": [
                    {"longText": "Santo Amaro", "shortText": "Santo Amaro",
                     "types": ["sublocality_level_1", "political"]},
                    {"longText": "Recife", "shortText": "Recife",
                     "types": ["locality", "political"]},
                ],
            })
        )
        result = asyncio.run(_client().fetch_address_components(_PLACE_ID))
    assert result == [
        {"longText": "Santo Amaro", "shortText": "Santo Amaro",
         "types": ["sublocality_level_1", "political"]},
        {"longText": "Recife", "shortText": "Recife",
         "types": ["locality", "political"]},
    ]


def test_response_with_no_address_components_key_returns_none():
    """A real place with nothing to map is a normal outcome, not an
    error -- distinct from a genuine 404."""
    with respx.mock:
        respx.get(_URL).mock(return_value=httpx.Response(200, json={"id": _PLACE_ID}))
        result = asyncio.run(_client().fetch_address_components(_PLACE_ID))
    assert result is None


def test_404_returns_none_not_an_error():
    with respx.mock:
        respx.get(_URL).mock(return_value=httpx.Response(404, json={"error": {"status": "NOT_FOUND"}}))
        result = asyncio.run(_client().fetch_address_components(_PLACE_ID))
    assert result is None


@pytest.mark.parametrize("status_code", [403, 429, 500, 503])
def test_non_404_http_error_raises_address_lookup_error(status_code):
    """A permission/quota/API problem must never look like a genuine
    no-match -- the caller (the backfill service) needs to count this as
    api_error, not silently mark the venue unresolvable."""
    with respx.mock:
        respx.get(_URL).mock(return_value=httpx.Response(status_code))
        with pytest.raises(GoogleAddressLookupError):
            asyncio.run(_client().fetch_address_components(_PLACE_ID))


def test_timeout_raises_address_lookup_error():
    with respx.mock:
        respx.get(_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(GoogleAddressLookupError):
            asyncio.run(_client().fetch_address_components(_PLACE_ID))


def test_connection_error_raises_address_lookup_error():
    with respx.mock:
        respx.get(_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(GoogleAddressLookupError):
            asyncio.run(_client().fetch_address_components(_PLACE_ID))


def test_uses_places_new_header_auth_not_query_string():
    """Unlike the dead geocode_by_place_id, this method is a genuine Places
    API (New) call: auth travels in the X-Goog-Api-Key header, never the
    query string."""
    with respx.mock:
        route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"id": _PLACE_ID}))
        asyncio.run(_client().fetch_address_components(_PLACE_ID))
        request = route.calls.last.request
        assert request.headers.get("X-Goog-Api-Key") == "test-key"
        assert "key" not in request.url.params


def test_sends_the_minimal_address_components_field_mask():
    with respx.mock:
        route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"id": _PLACE_ID}))
        asyncio.run(_client().fetch_address_components(_PLACE_ID))
        request = route.calls.last.request
        assert request.headers.get("X-Goog-FieldMask") == ADDRESS_COMPONENTS_FIELDS_MASK
        assert ADDRESS_COMPONENTS_FIELDS_MASK == "id,addressComponents"


def test_accepts_bare_place_id():
    with respx.mock:
        route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"id": _PLACE_ID}))
        asyncio.run(_client().fetch_address_components(_PLACE_ID))
        assert route.calls.last.request.url == _URL


def test_accepts_full_resource_name():
    with respx.mock:
        route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"id": _PLACE_ID}))
        asyncio.run(_client().fetch_address_components(f"places/{_PLACE_ID}"))
        assert route.calls.last.request.url == _URL
