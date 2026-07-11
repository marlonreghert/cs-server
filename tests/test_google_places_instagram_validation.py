"""Unit tests for GooglePlacesEnrichmentService's Instagram validation sweep.

validate_cached_instagram_handles must delete a cached handle ONLY on a
definitive 404 from Instagram; 429/403/other-status/network-error outcomes
must keep the handle (a mid-sweep rate-limit must never mass-delete valid
handles -- each re-discovery costs a paid Apify run).
"""
from unittest.mock import Mock

import httpx
import pytest
import respx

from app.models.instagram import VenueInstagram
from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService


def _dao_with_handle(venue_id="v1", handle="barconchittas"):
    dao = Mock()
    dao.list_active_venue_ids.return_value = [venue_id]
    dao.get_venue_instagram.return_value = VenueInstagram(
        venue_id=venue_id, instagram_handle=handle,
        instagram_url=f"https://instagram.com/{handle}",
        status="found", confidence_score=0.9,
    )
    return dao


def _service(dao):
    return GooglePlacesEnrichmentService(google_places_client=Mock(), venue_dao=dao)


@pytest.mark.asyncio
@respx.mock
async def test_check_status_found_on_200():
    respx.head("https://www.instagram.com/somehandle/").mock(
        return_value=httpx.Response(200)
    )
    status = await GooglePlacesEnrichmentService._check_instagram_status("somehandle")
    assert status == "found"


@pytest.mark.asyncio
@respx.mock
async def test_check_status_not_found_on_404():
    respx.head("https://www.instagram.com/somehandle/").mock(
        return_value=httpx.Response(404)
    )
    status = await GooglePlacesEnrichmentService._check_instagram_status("somehandle")
    assert status == "not_found"


@pytest.mark.asyncio
@respx.mock
async def test_check_status_unknown_on_rate_limit():
    respx.head("https://www.instagram.com/somehandle/").mock(
        return_value=httpx.Response(429)
    )
    status = await GooglePlacesEnrichmentService._check_instagram_status("somehandle")
    assert status == "unknown"


@pytest.mark.asyncio
@respx.mock
async def test_check_status_unknown_on_forbidden():
    respx.head("https://www.instagram.com/somehandle/").mock(
        return_value=httpx.Response(403)
    )
    status = await GooglePlacesEnrichmentService._check_instagram_status("somehandle")
    assert status == "unknown"


@pytest.mark.asyncio
@respx.mock
async def test_check_status_unknown_on_network_error():
    respx.head("https://www.instagram.com/somehandle/").mock(
        side_effect=httpx.ConnectError("boom")
    )
    status = await GooglePlacesEnrichmentService._check_instagram_status("somehandle")
    assert status == "unknown"


@pytest.mark.asyncio
@respx.mock
async def test_validate_deletes_only_on_definitive_404():
    dao = _dao_with_handle()
    respx.head("https://www.instagram.com/barconchittas/").mock(
        return_value=httpx.Response(404)
    )
    service = _service(dao)

    removed = await service.validate_cached_instagram_handles()

    assert removed == 1
    dao.delete_venue_instagram.assert_called_once_with("v1")


@pytest.mark.asyncio
@respx.mock
async def test_validate_keeps_handle_on_rate_limit():
    dao = _dao_with_handle()
    respx.head("https://www.instagram.com/barconchittas/").mock(
        return_value=httpx.Response(429)
    )
    service = _service(dao)

    removed = await service.validate_cached_instagram_handles()

    assert removed == 0
    dao.delete_venue_instagram.assert_not_called()


@pytest.mark.asyncio
@respx.mock
async def test_validate_keeps_handle_on_network_error():
    dao = _dao_with_handle()
    respx.head("https://www.instagram.com/barconchittas/").mock(
        side_effect=httpx.ConnectError("boom")
    )
    service = _service(dao)

    removed = await service.validate_cached_instagram_handles()

    assert removed == 0
    dao.delete_venue_instagram.assert_not_called()


@pytest.mark.asyncio
@respx.mock
async def test_instagram_profile_exists_true_on_unknown():
    """The website-extraction boolean gate fails open on an ambiguous check."""
    respx.head("https://www.instagram.com/somehandle/").mock(
        return_value=httpx.Response(429)
    )
    assert await GooglePlacesEnrichmentService._instagram_profile_exists("somehandle") is True


@pytest.mark.asyncio
@respx.mock
async def test_instagram_profile_exists_false_on_404():
    respx.head("https://www.instagram.com/somehandle/").mock(
        return_value=httpx.Response(404)
    )
    assert await GooglePlacesEnrichmentService._instagram_profile_exists("somehandle") is False
