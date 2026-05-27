"""Tests for Google Places closure lifecycle handling."""
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import settings
from app.models.vibe_attributes import GooglePlacesDetailsResponse, VibeAttributes
from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService


class _FakeGoogleClient:
    def __init__(self, details: GooglePlacesDetailsResponse):
        self.details = details
        self.get_place_details = AsyncMock(return_value=details)

    def details_to_vibe_attributes(self, venue_id, details):
        return VibeAttributes(
            venue_id=venue_id,
            google_place_id=details.place_id,
            google_primary_type=details.primary_type,
        )


@pytest.mark.asyncio
async def test_permanently_closed_venue_is_soft_deleted_not_hard_deleted(monkeypatch):
    """Permanent Google closure must retain Redis data and mark lifecycle."""
    monkeypatch.setattr(settings, "remove_permanently_closed_venues", True)
    details = GooglePlacesDetailsResponse(
        place_id="place_closed",
        primary_type="bar",
        business_status="CLOSED_PERMANENTLY",
    )
    dao = Mock()
    dao.get_vibe_attributes.return_value = None
    dao.soft_delete_venue.return_value = True
    dao.count_deprecated_venues.return_value = 1

    service = GooglePlacesEnrichmentService(_FakeGoogleClient(details), dao)

    result = await service.enrich_venue("venue_closed", "place_closed", force_refresh=True)

    assert result is None
    dao.set_google_business_status.assert_called_once_with(
        "venue_closed", "CLOSED_PERMANENTLY"
    )
    dao.soft_delete_venue.assert_called_once_with(
        venue_id="venue_closed",
        reason="google_places_closed_permanently",
        source="google_places",
        google_business_status="CLOSED_PERMANENTLY",
    )
    dao.delete_venue.assert_not_called()
    dao.set_vibe_attributes.assert_not_called()


@pytest.mark.asyncio
async def test_temporarily_closed_venue_remains_active_and_enriched(monkeypatch):
    """Temporary Google closure must not delete or soft-delete the venue."""
    monkeypatch.setattr(settings, "remove_temporarily_closed_venues", True)
    details = GooglePlacesDetailsResponse(
        place_id="place_temp",
        primary_type="bar",
        business_status="CLOSED_TEMPORARILY",
    )
    dao = Mock()
    dao.get_vibe_attributes.return_value = None

    service = GooglePlacesEnrichmentService(_FakeGoogleClient(details), dao)

    result = await service.enrich_venue("venue_temp", "place_temp", force_refresh=True)

    assert result is not None
    assert result.venue_id == "venue_temp"
    dao.set_google_business_status.assert_called_once_with(
        "venue_temp", "CLOSED_TEMPORARILY"
    )
    dao.soft_delete_venue.assert_not_called()
    dao.delete_venue.assert_not_called()
    dao.set_vibe_attributes.assert_called_once()
