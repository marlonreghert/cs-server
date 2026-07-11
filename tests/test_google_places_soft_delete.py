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


def test_business_status_recheck_disabled_by_default():
    """LOCKED decision: no catalog-wide Google spend on deploy. A future
    default flip must be a deliberate, reviewed change to this test, not an
    accidental one."""
    assert settings.business_status_recheck_enabled is False


@pytest.mark.asyncio
async def test_recheck_skipped_when_disabled(monkeypatch):
    """With the flag at its default (off), an already-enriched venue is
    skipped outright -- Google is never called for it."""
    monkeypatch.setattr(settings, "business_status_recheck_enabled", False)
    dao = Mock()
    dao.list_servable_venue_ids.return_value = ["v1"]
    dao.count_venues_with_vibe_attributes.return_value = 1
    dao.get_vibe_attributes.return_value = VibeAttributes(
        venue_id="v1", google_place_id="place_1", google_primary_type="bar",
    )
    google = _FakeGoogleClient(GooglePlacesDetailsResponse(place_id="place_1"))
    service = GooglePlacesEnrichmentService(google, dao)

    await service.enrich_all_venues(force_refresh=False)

    google.get_place_details.assert_not_awaited()
    dao.soft_delete_venue.assert_not_called()


@pytest.mark.asyncio
async def test_recheck_detects_permanent_closure_when_enabled(monkeypatch):
    """With the flag enabled, an already-enriched venue gets a status-only
    recheck (fields_mask='businessStatus') and is deprecated on closure --
    without re-deriving vibe_attributes."""
    monkeypatch.setattr(settings, "business_status_recheck_enabled", True)
    monkeypatch.setattr(settings, "remove_permanently_closed_venues", True)
    dao = Mock()
    dao.list_servable_venue_ids.return_value = ["v1"]
    dao.count_venues_with_vibe_attributes.return_value = 1
    dao.get_vibe_attributes.return_value = VibeAttributes(
        venue_id="v1", google_place_id="place_1", google_primary_type="bar",
    )
    dao.soft_delete_venue.return_value = True
    dao.count_deprecated_venues.return_value = 1
    google = _FakeGoogleClient(GooglePlacesDetailsResponse(
        place_id="place_1", business_status="CLOSED_PERMANENTLY",
    ))
    service = GooglePlacesEnrichmentService(google, dao)

    await service.enrich_all_venues(force_refresh=False)

    google.get_place_details.assert_awaited_once_with("place_1", fields_mask="businessStatus")
    dao.soft_delete_venue.assert_called_once_with(
        venue_id="v1", reason="google_places_closed_permanently",
        source="google_places", google_business_status="CLOSED_PERMANENTLY",
    )
    dao.set_vibe_attributes.assert_not_called()  # never re-derived


@pytest.mark.asyncio
async def test_recheck_never_attempted_for_no_match_marker(monkeypatch):
    """A venue whose vibe_attributes row IS the empty no-match poison marker
    (google_place_id="") has nothing to recheck -- Google must not be called."""
    monkeypatch.setattr(settings, "business_status_recheck_enabled", True)
    dao = Mock()
    dao.list_servable_venue_ids.return_value = ["v1"]
    dao.count_venues_with_vibe_attributes.return_value = 1
    dao.get_vibe_attributes.return_value = VibeAttributes(venue_id="v1", google_place_id="")
    google = _FakeGoogleClient(GooglePlacesDetailsResponse(place_id="place_1"))
    service = GooglePlacesEnrichmentService(google, dao)

    await service.enrich_all_venues(force_refresh=False)

    google.get_place_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_recheck_error_leaves_venue_untouched(monkeypatch):
    """A failed status-only Details call must not soft-delete or otherwise
    mutate the venue -- it is simply retried on the next run."""
    monkeypatch.setattr(settings, "business_status_recheck_enabled", True)
    dao = Mock()
    dao.list_servable_venue_ids.return_value = ["v1"]
    dao.count_venues_with_vibe_attributes.return_value = 1
    dao.get_vibe_attributes.return_value = VibeAttributes(
        venue_id="v1", google_place_id="place_1", google_primary_type="bar",
    )
    google = _FakeGoogleClient(None)  # simulates a failed Details fetch
    service = GooglePlacesEnrichmentService(google, dao)

    await service.enrich_all_venues(force_refresh=False)

    dao.soft_delete_venue.assert_not_called()
    dao.set_google_business_status.assert_not_called()


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
