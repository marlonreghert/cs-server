"""Google Places API (New) client for fetching venue vibe attributes."""
import logging
import time
from typing import Optional
import httpx

from app.models.vibe_attributes import GooglePlacesDetailsResponse, VibeAttributes
from app.metrics import (
    GOOGLE_PLACES_API_CALLS_TOTAL,
    GOOGLE_PLACES_API_CALL_DURATION_SECONDS,
    GOOGLE_PLACES_API_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)

# Google Places API (New) base URL
GOOGLE_PLACES_API_BASE = "https://places.googleapis.com/v1"

# Field mask for vibe-related attributes
# See: https://developers.google.com/maps/documentation/places/web-service/place-details
VIBE_FIELDS_MASK = ",".join([
    "id",
    "displayName",
    # Boolean attributes
    "allowsDogs",
    "goodForChildren",
    "goodForGroups",
    "goodForWatchingSports",
    "liveMusic",
    "outdoorSeating",
    "reservable",
    "restroom",
    "servesBeer",
    "servesBreakfast",
    "servesBrunch",
    "servesCocktails",
    "servesCoffee",
    "servesDinner",
    "servesLunch",
    "servesVegetarianFood",
    "servesWine",
    # Accessibility
    "accessibilityOptions",
    # Summaries
    "generativeSummary",
    "editorialSummary",
])


class GooglePlacesAPIClient:
    """Async HTTP client for Google Places API (New).

    This client fetches vibe-related attributes from Google's Places API,
    including LGBTQ+ friendliness, atmosphere attributes, and AI-generated summaries.
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = 15.0,
    ):
        """Initialize Google Places API client.

        Args:
            api_key: Google Maps/Places API key
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.timeout = timeout

        # Create async HTTP client with connection pooling
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def close(self):
        """Close the HTTP client and clean up resources."""
        await self.client.aclose()

    async def search_place_id(
        self,
        venue_name: str,
        venue_address: str,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
    ) -> Optional[str]:
        """Search for a venue by name/address to get its Google Place ID.

        Uses Text Search (New) API to find the place.

        Args:
            venue_name: Name of the venue
            venue_address: Address of the venue
            lat: Optional latitude for location bias
            lng: Optional longitude for location bias

        Returns:
            Google Place ID if found, None otherwise
        """
        url = f"{GOOGLE_PLACES_API_BASE}/places:searchText"

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "places.id,places.displayName",
        }

        # Combine name and address for search
        query = f"{venue_name} {venue_address}"

        body = {
            "textQuery": query,
            "maxResultCount": 1,
        }

        # Add location bias if coordinates available
        if lat is not None and lng is not None:
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": 500.0,  # 500m radius
                }
            }

        logger.debug(f"[GooglePlacesAPIClient] Searching for: {query}")

        start_time = time.perf_counter()

        try:
            response = await self.client.post(url, headers=headers, json=body)
            response.raise_for_status()

            data = response.json()
            places = data.get("places", [])

            duration = time.perf_counter() - start_time
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint="text_search").observe(duration)
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint="text_search", status="success").inc()

            if places:
                # Return the first (best match) place ID
                place_id = places[0].get("id")
                logger.debug(f"[GooglePlacesAPIClient] Found place ID: {place_id}")
                return place_id

            logger.warning(f"[GooglePlacesAPIClient] No place found for: {query}")
            return None

        except httpx.HTTPStatusError as e:
            duration = time.perf_counter() - start_time
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint="text_search").observe(duration)
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint="text_search", status="error").inc()
            GOOGLE_PLACES_API_ERRORS_TOTAL.labels(endpoint="text_search", error_type="http_error").inc()
            logger.error(f"[GooglePlacesAPIClient] Text search error: {e}")
            return None

        except Exception as e:
            duration = time.perf_counter() - start_time
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint="text_search").observe(duration)
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint="text_search", status="error").inc()
            logger.error(f"[GooglePlacesAPIClient] Text search exception: {e}")
            return None

    async def get_place_details(
        self,
        place_id: str,
        fields_mask: Optional[str] = None,
    ) -> Optional[GooglePlacesDetailsResponse]:
        """Fetch place details including vibe attributes.

        Uses the Google Places API (New) which provides structured attribute data.

        Args:
            place_id: Google Place ID
            fields_mask: Optional custom field mask (uses VIBE_FIELDS_MASK by default)

        Returns:
            GooglePlacesDetailsResponse with vibe attributes, or None on error
        """
        endpoint = f"/places/{place_id}"
        url = f"{GOOGLE_PLACES_API_BASE}{endpoint}"

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": fields_mask or VIBE_FIELDS_MASK,
        }

        logger.debug(f"[GooglePlacesAPIClient] GET {endpoint}")

        start_time = time.perf_counter()

        try:
            response = await self.client.get(url, headers=headers)

            logger.debug(f"[GooglePlacesAPIClient] Response status: {response.status_code}")

            response.raise_for_status()

            data = response.json()

            # Record successful call metrics
            duration = time.perf_counter() - start_time
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint="place_details").observe(duration)
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint="place_details", status="success").inc()

            return self._parse_place_details(place_id, data)

        except httpx.HTTPStatusError as e:
            duration = time.perf_counter() - start_time
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint="place_details").observe(duration)
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint="place_details", status="error").inc()
            GOOGLE_PLACES_API_ERRORS_TOTAL.labels(endpoint="place_details", error_type="http_error").inc()

            # Handle specific errors gracefully
            if e.response.status_code == 404:
                logger.warning(f"[GooglePlacesAPIClient] Place not found: {place_id}")
            elif e.response.status_code == 403:
                logger.error(f"[GooglePlacesAPIClient] API key issue or quota exceeded: {e}")
            else:
                logger.error(f"[GooglePlacesAPIClient] HTTP error for {place_id}: {e}")
            return None

        except httpx.TimeoutException as e:
            duration = time.perf_counter() - start_time
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint="place_details").observe(duration)
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint="place_details", status="error").inc()
            GOOGLE_PLACES_API_ERRORS_TOTAL.labels(endpoint="place_details", error_type="timeout").inc()
            logger.error(f"[GooglePlacesAPIClient] Timeout for {place_id}: {e}")
            return None

        except httpx.RequestError as e:
            duration = time.perf_counter() - start_time
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint="place_details").observe(duration)
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint="place_details", status="error").inc()
            GOOGLE_PLACES_API_ERRORS_TOTAL.labels(endpoint="place_details", error_type="connection_error").inc()
            logger.error(f"[GooglePlacesAPIClient] Request error for {place_id}: {e}")
            return None

    def _parse_place_details(self, place_id: str, data: dict) -> GooglePlacesDetailsResponse:
        """Parse the Google Places API response into our model.

        Args:
            place_id: The place ID
            data: Raw JSON response from Google Places API

        Returns:
            GooglePlacesDetailsResponse with parsed attributes
        """
        # Extract accessibility options if present
        accessibility = data.get("accessibilityOptions", {})

        # Extract display name
        display_name_obj = data.get("displayName", {})
        display_name = display_name_obj.get("text") if isinstance(display_name_obj, dict) else None

        # Extract generative summary
        generative_summary_obj = data.get("generativeSummary", {})
        generative_summary = None
        if isinstance(generative_summary_obj, dict):
            overview = generative_summary_obj.get("overview", {})
            if isinstance(overview, dict):
                generative_summary = overview.get("text")

        # Extract editorial summary
        editorial_summary_obj = data.get("editorialSummary", {})
        editorial_summary = None
        if isinstance(editorial_summary_obj, dict):
            editorial_summary = editorial_summary_obj.get("text")

        return GooglePlacesDetailsResponse(
            place_id=place_id,
            display_name=display_name,
            # Boolean attributes
            allows_dogs=data.get("allowsDogs"),
            good_for_children=data.get("goodForChildren"),
            good_for_groups=data.get("goodForGroups"),
            good_for_watching_sports=data.get("goodForWatchingSports"),
            live_music=data.get("liveMusic"),
            outdoor_seating=data.get("outdoorSeating"),
            reservable=data.get("reservable"),
            restroom=data.get("restroom"),
            serves_beer=data.get("servesBeer"),
            serves_breakfast=data.get("servesBreakfast"),
            serves_brunch=data.get("servesBrunch"),
            serves_cocktails=data.get("servesCocktails"),
            serves_coffee=data.get("servesCoffee"),
            serves_dinner=data.get("servesDinner"),
            serves_lunch=data.get("servesLunch"),
            serves_vegetarian_food=data.get("servesVegetarianFood"),
            serves_wine=data.get("servesWine"),
            # Accessibility
            wheelchair_accessible_entrance=accessibility.get("wheelchairAccessibleEntrance"),
            wheelchair_accessible_parking=accessibility.get("wheelchairAccessibleParking"),
            wheelchair_accessible_restroom=accessibility.get("wheelchairAccessibleRestroom"),
            wheelchair_accessible_seating=accessibility.get("wheelchairAccessibleSeating"),
            # Summaries
            generative_summary=generative_summary,
            editorial_summary=editorial_summary,
        )

    def details_to_vibe_attributes(
        self,
        venue_id: str,
        details: GooglePlacesDetailsResponse,
    ) -> VibeAttributes:
        """Convert Google Places details to our VibeAttributes model.

        Args:
            venue_id: Our internal venue ID
            details: Parsed Google Places response

        Returns:
            VibeAttributes model for storage
        """
        return VibeAttributes(
            venue_id=venue_id,
            # LGBTQ+ - Google doesn't have specific fields yet, may need to parse from summary
            lgbtq_friendly=None,  # TODO: Parse from generative_summary if available
            transgender_safespace=None,
            # Crowd & Social
            good_for_groups=details.good_for_groups,
            good_for_kids=details.good_for_children,
            good_for_working=None,  # Not directly available in API
            # Pet Related
            allows_dogs=details.allows_dogs,
            # Accessibility
            wheelchair_accessible_entrance=details.wheelchair_accessible_entrance,
            wheelchair_accessible_seating=details.wheelchair_accessible_seating,
            wheelchair_accessible_restroom=details.wheelchair_accessible_restroom,
            # Atmosphere
            live_music=details.live_music,
            outdoor_seating=details.outdoor_seating,
            rooftop=None,  # Not directly available
            # Service Style
            reservable=details.reservable,
            serves_breakfast=details.serves_breakfast,
            serves_brunch=details.serves_brunch,
            serves_lunch=details.serves_lunch,
            serves_dinner=details.serves_dinner,
            serves_vegetarian_food=details.serves_vegetarian_food,
            serves_beer=details.serves_beer,
            serves_wine=details.serves_wine,
            serves_cocktails=details.serves_cocktails,
            # AI Summary
            generative_summary=details.generative_summary or details.editorial_summary,
        )


async def search_for_lgbtq_indicators(summary: Optional[str]) -> bool:
    """Analyze summary text for LGBTQ+ friendliness indicators.

    This is a simple heuristic approach. In production, you might want to use
    a more sophisticated NLP approach or an LLM to analyze the text.

    Args:
        summary: AI-generated or editorial summary text

    Returns:
        True if LGBTQ+ friendly indicators found
    """
    if not summary:
        return False

    summary_lower = summary.lower()

    # Keywords that indicate LGBTQ+ friendliness
    lgbtq_keywords = [
        "lgbtq",
        "lgbt",
        "gay",
        "lesbian",
        "queer",
        "pride",
        "drag",
        "inclusive",
        "welcoming to all",
        "diverse crowd",
        "rainbow",
    ]

    return any(keyword in summary_lower for keyword in lgbtq_keywords)
