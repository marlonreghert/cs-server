"""Unit tests for GooglePlacesAPIClient.search_place_id's no-match/error split.

A genuine zero-result (Google answered, no places) must always return None.
A transport/quota failure (HTTP error status, timeout, connection error) must
return None when raise_on_error=False (every existing caller's default,
unchanged behavior) but raise GooglePlacesSearchError when raise_on_error=True
(the enrichment loop's opt-in) -- never silently collapsed into "no match".
"""
import httpx
import pytest

from app.api.google_places_client import GooglePlacesAPIClient, GooglePlacesSearchError


def _client_with_handler(handler):
    client = GooglePlacesAPIClient(api_key="unit-key")
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return client


@pytest.mark.asyncio
async def test_zero_results_returns_none_default():
    def handler(request):
        return httpx.Response(200, json={"places": []})

    client = _client_with_handler(handler)
    result = await client.search_place_id("Bar X", "Rua Y, 1")
    assert result is None


@pytest.mark.asyncio
async def test_zero_results_returns_none_even_with_raise_on_error():
    """A genuine no-match is NOT an error -- it must still return None even
    when the caller opted into raise_on_error."""
    def handler(request):
        return httpx.Response(200, json={"places": []})

    client = _client_with_handler(handler)
    result = await client.search_place_id("Bar X", "Rua Y, 1", raise_on_error=True)
    assert result is None


@pytest.mark.asyncio
async def test_found_result_returns_place_id():
    def handler(request):
        return httpx.Response(200, json={"places": [{"id": "places/ABC"}]})

    client = _client_with_handler(handler)
    result = await client.search_place_id("Bar X", "Rua Y, 1")
    assert result == "places/ABC"


@pytest.mark.asyncio
async def test_http_error_returns_none_by_default():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    client = _client_with_handler(handler)
    result = await client.search_place_id("Bar X", "Rua Y, 1")
    assert result is None


@pytest.mark.asyncio
async def test_http_error_raises_when_opted_in():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    client = _client_with_handler(handler)
    with pytest.raises(GooglePlacesSearchError):
        await client.search_place_id("Bar X", "Rua Y, 1", raise_on_error=True)


@pytest.mark.asyncio
async def test_timeout_returns_none_by_default():
    def handler(request):
        raise httpx.TimeoutException("timed out")

    client = _client_with_handler(handler)
    result = await client.search_place_id("Bar X", "Rua Y, 1")
    assert result is None


@pytest.mark.asyncio
async def test_timeout_raises_when_opted_in():
    def handler(request):
        raise httpx.TimeoutException("timed out")

    client = _client_with_handler(handler)
    with pytest.raises(GooglePlacesSearchError):
        await client.search_place_id("Bar X", "Rua Y, 1", raise_on_error=True)


@pytest.mark.asyncio
async def test_connection_error_raises_when_opted_in():
    def handler(request):
        raise httpx.ConnectError("boom")

    client = _client_with_handler(handler)
    with pytest.raises(GooglePlacesSearchError):
        await client.search_place_id("Bar X", "Rua Y, 1", raise_on_error=True)
