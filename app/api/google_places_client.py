"""Google Places API (New) client for fetching venue vibe attributes and photos."""
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional
import httpx

from app.models.vibe_attributes import GooglePlacesDetailsResponse, VibeAttributes
from app.models.venue import PriceRange
from app.metrics import (
    GOOGLE_PLACES_API_CALLS_TOTAL,
    GOOGLE_PLACES_API_CALL_DURATION_SECONDS,
    GOOGLE_PLACES_API_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)

# Google Places API (New) base URL
GOOGLE_PLACES_API_BASE = "https://places.googleapis.com/v1"

# Field mask for fetching photos (include author attributions for copyright compliance)
PHOTOS_FIELDS_MASK = "photos.name,photos.authorAttributions"
# Field mask for vibe-related attributes
# See: https://developers.google.com/maps/documentation/places/web-service/place-details
VIBE_FIELDS_MASK = ",".join([
    "id",
    "displayName",
    "primaryType",
    # Business status (OPERATIONAL, CLOSED_TEMPORARILY, CLOSED_PERMANENTLY)
    "businessStatus",
    # Website (used to detect Instagram URLs)
    "websiteUri",
    # Opening hours
    "regularOpeningHours",           # Standard weekly hours
    "currentOpeningHours",           # Today's hours (may differ due to holidays)
    "currentSecondaryOpeningHours",  # Special hours (holidays, events)
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
    # Reviews
    "reviews",
    # Aggregate review signal — drives the venue-card "4.5 ★ (586)" UI.
    # Inventory-synced venues come in without these populated; enrichment
    # backfills them onto the Venue model (see GooglePlacesEnrichmentService).
    "rating",
    "userRatingCount",
    "priceLevel",
    # Objective money range (currency + start/end). PRIMARY tier source stays the
    # enum; this fills enum-less venues and is served as the structured range.
    "priceRange",
])

# Language code for Portuguese (Brazil) - used for opening hours descriptions
LANGUAGE_CODE = "pt-BR"


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

    @asynccontextmanager
    async def _instrumented(self, endpoint: str):
        """Time one Google Places API call and emit its metrics uniformly.

        On clean exit: DURATION(endpoint) + CALLS(endpoint, success). On failure:
        DURATION(endpoint) + CALLS(endpoint, error) + — for the three known
        transport failures — ERRORS(endpoint, http_error|timeout|connection_error),
        then the exception re-raises so the caller keeps its own per-exception
        logging and return/raise. One consistent error taxonomy across all five
        endpoints; metric + label names are unchanged and error_type values stay
        within the existing {http_error, timeout, connection_error} set.
        """
        start_time = time.perf_counter()
        try:
            yield
        except BaseException as e:
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(
                time.perf_counter() - start_time
            )
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            error_type = _classify_google_error(e)
            if error_type is not None:
                GOOGLE_PLACES_API_ERRORS_TOTAL.labels(
                    endpoint=endpoint, error_type=error_type
                ).inc()
            raise
        else:
            GOOGLE_PLACES_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(
                time.perf_counter() - start_time
            )
            GOOGLE_PLACES_API_CALLS_TOTAL.labels(endpoint=endpoint, status="success").inc()

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

        try:
            async with self._instrumented("text_search"):
                response = await self.client.post(url, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
                places = data.get("places", [])

            if places:
                # Return the first (best match) place ID
                place_id = places[0].get("id")
                logger.debug(f"[GooglePlacesAPIClient] Found place ID: {place_id}")
                return place_id

            logger.warning(f"[GooglePlacesAPIClient] No place found for: {query}")
            return None

        except httpx.HTTPStatusError as e:
            logger.error(f"[GooglePlacesAPIClient] Text search error: {e}")
            return None

        except Exception as e:
            logger.error(f"[GooglePlacesAPIClient] Text search exception: {e}")
            return None

    async def get_place_location(
        self, place_id: str
    ) -> Optional[tuple[float, float]]:
        """Fetch just a place's (lat, lng) via Places (New) with a minimal field
        mask. Used by the batch-add path to resolve coordinates for rows that
        carry a place_id but no coords. Returns None if unavailable.
        """
        if not place_id:
            return None
        pid = place_id if place_id.startswith("places/") else f"places/{place_id}"
        url = f"{GOOGLE_PLACES_API_BASE}/{pid}"
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "location",
        }
        try:
            async with self._instrumented("place_location"):
                response = await self.client.get(url, headers=headers)
                response.raise_for_status()
                loc = (response.json() or {}).get("location") or {}
                lat, lng = loc.get("latitude"), loc.get("longitude")
            if lat is None or lng is None:
                return None
            return float(lat), float(lng)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[GooglePlacesAPIClient] get_place_location failed for "
                f"{place_id}: {type(e).__name__}: {e}"
            )
            return None

    async def resolve_coordinates(
        self,
        venue_name: str,
        venue_address: str,
        place_id: Optional[str] = None,
        lat_bias: Optional[float] = None,
        lng_bias: Optional[float] = None,
    ) -> tuple[Optional[str], Optional[float], Optional[float]]:
        """Resolve a venue's Google place_id + (lat, lng). Uses the given
        place_id when present, otherwise a Text Search (biased by the city
        center). Returns (place_id, lat, lng) with None fields when a step
        cannot be resolved. Never raises.
        """
        pid = place_id or await self.search_place_id(
            venue_name, venue_address, lat=lat_bias, lng=lng_bias
        )
        if not pid:
            return None, None, None
        loc = await self.get_place_location(pid)
        if loc is None:
            return pid, None, None
        return pid, loc[0], loc[1]

    async def get_place_details(
        self,
        place_id: str,
        fields_mask: Optional[str] = None,
    ) -> Optional[GooglePlacesDetailsResponse]:
        """Fetch place details including vibe attributes.

        Uses the Google Places API (New) which provides structured attribute data.

        Args:
            place_id: Google Place ID (can be full resource name like 'places/ChIJ...' or just 'ChIJ...')
            fields_mask: Optional custom field mask (uses VIBE_FIELDS_MASK by default)

        Returns:
            GooglePlacesDetailsResponse with vibe attributes, or None on error
        """
        # Handle both formats: 'places/ChIJ...' or just 'ChIJ...'
        if place_id.startswith("places/"):
            endpoint = f"/{place_id}"
        else:
            endpoint = f"/places/{place_id}"
        url = f"{GOOGLE_PLACES_API_BASE}{endpoint}"

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": fields_mask or VIBE_FIELDS_MASK,
        }

        # Add language code for Portuguese opening hours descriptions
        params = {"languageCode": LANGUAGE_CODE}

        logger.debug(f"[GooglePlacesAPIClient] GET {endpoint}")

        try:
            async with self._instrumented("place_details"):
                response = await self.client.get(url, headers=headers, params=params)
                logger.debug(f"[GooglePlacesAPIClient] Response status: {response.status_code}")
                response.raise_for_status()
                data = response.json()

            return self._parse_place_details(place_id, data)

        except httpx.HTTPStatusError as e:
            # Handle specific errors gracefully
            if e.response.status_code == 404:
                logger.warning(f"[GooglePlacesAPIClient] Place not found: {place_id}")
            elif e.response.status_code == 403:
                logger.error(f"[GooglePlacesAPIClient] API key issue or quota exceeded: {e}")
            else:
                logger.error(f"[GooglePlacesAPIClient] HTTP error for {place_id}: {e}")
            return None

        except httpx.TimeoutException as e:
            logger.error(f"[GooglePlacesAPIClient] Timeout for {place_id}: {e}")
            return None

        except httpx.RequestError as e:
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

        # Extract opening hours
        regular_hours = data.get("regularOpeningHours", {})
        current_hours = data.get("currentOpeningHours", {})
        # currentSecondaryOpeningHours is an ARRAY of objects per the API spec
        secondary_hours_list = data.get("currentSecondaryOpeningHours") or []

        # Get weekday descriptions (pre-formatted strings in Portuguese)
        weekday_descriptions = regular_hours.get("weekdayDescriptions", []) if regular_hours else None

        # Get current open status
        open_now = current_hours.get("openNow") if current_hours else None

        # Get special days descriptions (holidays)
        special_days = None
        if isinstance(secondary_hours_list, list) and secondary_hours_list:
            first_secondary = secondary_hours_list[0]
            if isinstance(first_secondary, dict):
                secondary_descriptions = first_secondary.get("weekdayDescriptions", [])
                if secondary_descriptions:
                    special_days = secondary_descriptions

        # Parse reviews (top 5, sorted by relevance by API)
        raw_reviews = data.get("reviews", []) or []
        parsed_reviews = []
        for r in raw_reviews[:5]:
            text_obj = r.get("text", {})
            author_obj = r.get("authorAttribution", {})
            parsed_reviews.append({
                "author_name": author_obj.get("displayName", ""),
                "rating": r.get("rating", 0),
                "text": text_obj.get("text", "") if isinstance(text_obj, dict) else "",
                "relative_time": r.get("relativePublishTimeDescription", ""),
                "language": text_obj.get("languageCode") if isinstance(text_obj, dict) else None,
                "publish_time": r.get("publishTime"),
            })

        return GooglePlacesDetailsResponse(
            place_id=place_id,
            display_name=display_name,
            primary_type=data.get("primaryType"),
            website_uri=data.get("websiteUri"),
            # Business status (OPERATIONAL, CLOSED_TEMPORARILY, CLOSED_PERMANENTLY)
            business_status=data.get("businessStatus"),
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
            # Opening hours
            weekday_descriptions=weekday_descriptions,
            open_now=open_now,
            special_days=special_days,
            # Reviews
            reviews=parsed_reviews if parsed_reviews else None,
            # Aggregate review signal (raw — enrichment derives the 1..4/NULL tier
            # from these via app/services/price_signal.py).
            rating=data.get("rating"),
            user_rating_count=data.get("userRatingCount"),
            price_level=data.get("priceLevel"),
            price_range=_parse_price_range(data.get("priceRange")),
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

    async def get_place_photos(
        self,
        place_id: str,
        max_photos: int = 5,
        max_width: int = 800,
    ) -> list[dict]:
        """Resolve FRESH, KEYLESS photo URLs (with author attribution) for a place.

        Two-step, per the Google Places API (New):
          1. Place Details (photos field mask) -> photo resource `name`s +
             author attributions (required for copyright compliance).
          2. For each `name`, call the photo media endpoint with
             `skipHttpRedirect=true` and the API key in the `X-Goog-Api-Key`
             HEADER (never the URL) to read the KEYLESS `photoUri`
             (https://lh3.googleusercontent.com/...).

        The returned/stored URL therefore carries no `key=` parameter and is not
        a `places.googleapis.com/.../media` redirect URL, so it does not die when
        Google rotates the photo token.

        Args:
            place_id: Google Place ID ('places/ChIJ...' or bare 'ChIJ...')
            max_photos: Maximum number of photos to resolve (default 5)
            max_width: Requested max width in pixels (maxWidthPx, default 800)

        Returns:
            List of dicts: [{url: <keyless photoUri>, author_name: str | None}, ...],
            capped at max_photos. A photo whose media call fails is skipped (the
            venue is not failed).

        Raises:
            httpx.HTTPStatusError / httpx.TimeoutException / httpx.RequestError:
            when the Place Details call itself fails, so the on-demand resolver
            can distinguish a hard failure (do not cache) from a genuine
            zero-photos result (cache empty).
        """
        # Handle both formats: 'places/ChIJ...' or just 'ChIJ...'
        if place_id.startswith("places/"):
            endpoint = f"/{place_id}"
        else:
            endpoint = f"/places/{place_id}"
        url = f"{GOOGLE_PLACES_API_BASE}{endpoint}"

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": PHOTOS_FIELDS_MASK,
        }

        logger.debug(f"[GooglePlacesAPIClient] Fetching photos for place: {place_id}")

        # Step 1: Place Details -> photo resource names. A hard error here is
        # propagated (metrics recorded first) so the caller can avoid caching.
        try:
            async with self._instrumented("place_photos"):
                response = await self.client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"[GooglePlacesAPIClient] Photo details error for {place_id}: {e}")
            raise
        except httpx.TimeoutException as e:
            logger.error(f"[GooglePlacesAPIClient] Photo details timeout for {place_id}: {e}")
            raise
        except httpx.RequestError as e:
            logger.error(f"[GooglePlacesAPIClient] Photo details request error for {place_id}: {e}")
            raise

        photos = data.get("photos", []) or []

        # Step 2: resolve each photo `name` to its keyless media URI (skip a
        # photo whose media call fails rather than failing the whole venue).
        result: list[dict] = []
        for photo in photos[:max_photos]:
            photo_name = photo.get("name")
            if not photo_name:
                continue
            keyless_uri = await self._resolve_photo_media_uri(photo_name, max_width)
            if not keyless_uri:
                continue
            author_name = None
            attributions = photo.get("authorAttributions", [])
            if attributions:
                author_name = attributions[0].get("displayName")
            result.append({"url": keyless_uri, "author_name": author_name})

        logger.debug(f"[GooglePlacesAPIClient] Resolved {len(result)} keyless photos for {place_id}")
        return result

    async def _resolve_photo_media_uri(
        self, photo_name: str, max_width: int
    ) -> Optional[str]:
        """Call the Google photo media endpoint with `skipHttpRedirect=true` and
        the API key in the `X-Goog-Api-Key` HEADER, returning the KEYLESS
        `photoUri`. The key never appears in the request URL or the returned URL.

        Returns None on any failure so the caller skips this one photo.
        """
        url = f"{GOOGLE_PLACES_API_BASE}/{photo_name}/media"
        headers = {"X-Goog-Api-Key": self.api_key}
        params = {"maxWidthPx": max_width, "skipHttpRedirect": "true"}
        try:
            async with self._instrumented("place_photo_media"):
                response = await self.client.get(url, headers=headers, params=params)
                response.raise_for_status()
                uri = (response.json() or {}).get("photoUri")
            if not uri:
                logger.warning(f"[GooglePlacesAPIClient] media response for {photo_name} had no photoUri")
            return uri
        except Exception as e:  # noqa: BLE001 — one bad photo must not fail the venue
            logger.warning(f"[GooglePlacesAPIClient] media call failed for {photo_name}: {type(e).__name__}: {e}")
            return None


def _classify_google_error(e: BaseException) -> Optional[str]:
    """Map a raised exception to the API error_type label, or None when it is not
    one of the three known transport failures (in which case only CALLS(error) is
    recorded, no ERRORS_TOTAL). Order matters: TimeoutException is a RequestError,
    so it is checked first; HTTPStatusError is not a RequestError."""
    if isinstance(e, httpx.HTTPStatusError):
        return "http_error"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    if isinstance(e, httpx.RequestError):
        return "connection_error"
    return None


def _money_units(money: Optional[dict]) -> Optional[float]:
    """Parse a Google `Money` object's whole-currency `units` (a string-encoded
    integer) to a number. Returns None when absent/unparsable."""
    if not isinstance(money, dict):
        return None
    units = money.get("units")
    if units is None:
        return None
    try:
        return float(units)
    except (TypeError, ValueError):
        return None


def _parse_price_range(price_range: Optional[dict]) -> Optional[PriceRange]:
    """Parse Google `priceRange` ({startPrice, endPrice} `Money` objects) into a
    structured PriceRange. `endPrice` may be absent (unbounded "more than X") ->
    `max=None`. Never raises: a partial/garbage range yields None or a best-effort
    range so the derivation can fall through."""
    if not isinstance(price_range, dict):
        return None
    start = price_range.get("startPrice")
    end = price_range.get("endPrice")
    currency = None
    if isinstance(start, dict):
        currency = start.get("currencyCode")
    if currency is None and isinstance(end, dict):
        currency = end.get("currencyCode")
    pmin = _money_units(start)
    pmax = _money_units(end)
    if currency is None and pmin is None and pmax is None:
        return None
    return PriceRange(currency=currency, min=pmin, max=pmax)
