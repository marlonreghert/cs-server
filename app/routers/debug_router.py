"""Debug routes for investigating venue data."""
import logging
from typing import Optional, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Create router at module level
router = APIRouter(prefix="/debug", tags=["debug"])

# Global references - set during startup
_venue_dao = None
_google_places_client = None


def set_debug_dependencies(venue_dao, google_places_client=None):
    """Set the dependencies for debug routes (called during startup)."""
    global _venue_dao, _google_places_client
    _venue_dao = venue_dao
    _google_places_client = google_places_client
    logger.info("[DebugRouter] Dependencies injected")


class VenueDebugInfo(BaseModel):
    """Complete debug info for a venue."""
    venue_id: str
    venue_name: str
    venue_address: str
    venue_lat: float
    venue_lng: float
    venue_type: Optional[str] = None

    # Full venue data as dict
    venue_data: dict

    # Associated data
    has_vibe_attributes: bool = False
    vibe_attributes: Optional[dict] = None
    vibe_labels: Optional[list[str]] = None

    has_live_forecast: bool = False
    live_forecast: Optional[dict] = None

    has_photos: bool = False
    photos: Optional[list[str]] = None

    # Opening hours data
    has_opening_hours: bool = False
    opening_hours: Optional[dict] = None

    # Google Places check (if available)
    google_places_check: Optional[dict] = None


class VenueSearchResult(BaseModel):
    """Search results for venue lookup."""
    query: str
    total_venues_in_db: int
    matches_found: int
    venues: list[VenueDebugInfo]


@router.get(
    "/venue/search",
    response_model=VenueSearchResult,
    summary="Search venues by name",
    description="Search for venues in Redis by name (case-insensitive partial match)",
)
async def search_venue_by_name(
    name: str = Query(..., description="Venue name to search for (partial match)"),
    check_google: bool = Query(False, description="Also check Google Places API for business status"),
) -> VenueSearchResult:
    """Search for venues by name and return all associated data."""
    if _venue_dao is None:
        return VenueSearchResult(
            query=name,
            total_venues_in_db=0,
            matches_found=0,
            venues=[],
        )

    # Get all venue IDs
    all_venue_ids = _venue_dao.list_all_venue_ids()
    search_lower = name.lower()

    matches = []

    for venue_id in all_venue_ids:
        venue = _venue_dao.get_venue(venue_id)
        if venue is None:
            continue

        # Case-insensitive partial match on venue name
        if search_lower in venue.venue_name.lower():
            # Get all associated data
            vibe_attrs = _venue_dao.get_vibe_attributes(venue_id)
            live_forecast = _venue_dao.get_live_forecast(venue_id)
            photos = _venue_dao.get_venue_photos(venue_id)
            opening_hours = _venue_dao.get_opening_hours(venue_id)

            # Check Google Places if requested
            google_check = None
            if check_google and _google_places_client:
                try:
                    # Search for place ID
                    place_id = await _google_places_client.search_place_id(
                        venue_name=venue.venue_name,
                        venue_address=venue.venue_address,
                        lat=venue.venue_lat,
                        lng=venue.venue_lng,
                    )

                    if place_id:
                        # Get place details
                        details = await _google_places_client.get_place_details(place_id)
                        if details:
                            google_check = {
                                "place_id": place_id,
                                "display_name": details.display_name,
                                "business_status": details.business_status,
                                "is_permanently_closed": details.is_permanently_closed(),
                                "is_temporarily_closed": details.is_temporarily_closed(),
                                "is_operational": details.is_operational(),
                            }
                        else:
                            google_check = {
                                "place_id": place_id,
                                "error": "Failed to fetch place details",
                            }
                    else:
                        google_check = {
                            "error": "Could not find Google Place ID for this venue",
                        }
                except Exception as e:
                    google_check = {"error": str(e)}

            debug_info = VenueDebugInfo(
                venue_id=venue.venue_id,
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                venue_lat=venue.venue_lat,
                venue_lng=venue.venue_lng,
                venue_type=venue.venue_type,
                venue_data=venue.model_dump(),
                has_vibe_attributes=vibe_attrs is not None,
                vibe_attributes=vibe_attrs.model_dump() if vibe_attrs else None,
                vibe_labels=vibe_attrs.get_vibe_labels() if vibe_attrs else None,
                has_live_forecast=live_forecast is not None,
                live_forecast=live_forecast.model_dump() if live_forecast else None,
                has_photos=photos is not None and len(photos) > 0,
                photos=photos,
                has_opening_hours=opening_hours is not None and opening_hours.has_hours(),
                opening_hours=opening_hours.model_dump() if opening_hours else None,
                google_places_check=google_check,
            )
            matches.append(debug_info)

    return VenueSearchResult(
        query=name,
        total_venues_in_db=len(all_venue_ids),
        matches_found=len(matches),
        venues=matches,
    )


@router.get(
    "/venue/{venue_id}",
    response_model=VenueDebugInfo,
    summary="Get venue by ID",
    description="Get complete debug info for a venue by its ID",
)
async def get_venue_by_id(
    venue_id: str,
    check_google: bool = Query(False, description="Also check Google Places API for business status"),
) -> VenueDebugInfo:
    """Get complete debug info for a venue by ID."""
    if _venue_dao is None:
        raise Exception("DAO not initialized")

    venue = _venue_dao.get_venue(venue_id)
    if venue is None:
        raise Exception(f"Venue not found: {venue_id}")

    # Get all associated data
    vibe_attrs = _venue_dao.get_vibe_attributes(venue_id)
    live_forecast = _venue_dao.get_live_forecast(venue_id)
    photos = _venue_dao.get_venue_photos(venue_id)
    opening_hours = _venue_dao.get_opening_hours(venue_id)

    # Check Google Places if requested
    google_check = None
    if check_google and _google_places_client:
        try:
            place_id = await _google_places_client.search_place_id(
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                lat=venue.venue_lat,
                lng=venue.venue_lng,
            )

            if place_id:
                details = await _google_places_client.get_place_details(place_id)
                if details:
                    google_check = {
                        "place_id": place_id,
                        "display_name": details.display_name,
                        "business_status": details.business_status,
                        "is_permanently_closed": details.is_permanently_closed(),
                        "is_temporarily_closed": details.is_temporarily_closed(),
                        "is_operational": details.is_operational(),
                    }
                else:
                    google_check = {"place_id": place_id, "error": "Failed to fetch details"}
            else:
                google_check = {"error": "Could not find Google Place ID"}
        except Exception as e:
            google_check = {"error": str(e)}

    return VenueDebugInfo(
        venue_id=venue.venue_id,
        venue_name=venue.venue_name,
        venue_address=venue.venue_address,
        venue_lat=venue.venue_lat,
        venue_lng=venue.venue_lng,
        venue_type=venue.venue_type,
        venue_data=venue.model_dump(),
        has_vibe_attributes=vibe_attrs is not None,
        vibe_attributes=vibe_attrs.model_dump() if vibe_attrs else None,
        vibe_labels=vibe_attrs.get_vibe_labels() if vibe_attrs else None,
        has_live_forecast=live_forecast is not None,
        live_forecast=live_forecast.model_dump() if live_forecast else None,
        has_photos=photos is not None and len(photos) > 0,
        photos=photos,
        has_opening_hours=opening_hours is not None and opening_hours.has_hours(),
        opening_hours=opening_hours.model_dump() if opening_hours else None,
        google_places_check=google_check,
    )


@router.get(
    "/stats",
    summary="Get database stats",
    description="Get statistics about venues in the database",
)
async def get_stats() -> dict:
    """Get database statistics."""
    if _venue_dao is None:
        return {"error": "DAO not initialized"}

    all_venue_ids = _venue_dao.list_all_venue_ids()
    venues_with_vibe_attrs = _venue_dao.count_venues_with_vibe_attributes()
    venues_with_photos = _venue_dao.count_venues_with_photos()

    return {
        "total_venues": len(all_venue_ids),
        "venues_with_vibe_attributes": venues_with_vibe_attrs,
        "venues_with_photos": venues_with_photos,
    }
