"""BestTime API client with async HTTP support."""
import logging
import time
from typing import Optional
import httpx

from typing import AsyncIterator

from app.models import (
    LiveForecastResponse,
    WeekRawResponse,
    VenueFilterParams,
    VenueFilterResponse,
    NewVenueResponse,
    AccountInventoryVenue,
)
from app.metrics import (
    BESTTIME_API_CALLS_TOTAL,
    BESTTIME_API_CALL_DURATION_SECONDS,
    BESTTIME_API_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)


class BestTimeAPIClient:
    """Async HTTP client for BestTime API."""

    def __init__(
        self,
        base_url: str,
        api_key_public: str,
        api_key_private: str,
        timeout: float = 10.0,
    ):
        """Initialize BestTime API client.

        Args:
            base_url: Base URL for BestTime API (e.g., "https://besttime.app/api/v1")
            api_key_public: Public API key
            api_key_private: Private API key
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.api_key_public = api_key_public
        self.api_key_private = api_key_private
        self.timeout = timeout

        # Create async HTTP client with connection pooling
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def close(self):
        """Close the HTTP client and clean up resources."""
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        """Make an HTTP request to the BestTime API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            params: Query parameters
            json_body: JSON request body

        Returns:
            JSON response as dict

        Raises:
            httpx.HTTPStatusError: If response status is not 2xx
            httpx.RequestError: If request fails
        """
        url = f"{self.base_url}{endpoint}"

        logger.debug(f"[BestTimeAPIClient] {method} {url} params={params} body={json_body}")

        start_time = time.perf_counter()

        try:
            response = await self.client.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                headers={"Content-Type": "application/json"},
            )

            logger.debug(f"[BestTimeAPIClient] Response status: {response.status_code}")

            response.raise_for_status()

            response_json = response.json()
            logger.debug(f"[BestTimeAPIClient] Success on {method} {endpoint}")

            # Record successful call metrics
            duration = time.perf_counter() - start_time
            BESTTIME_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="success").inc()

            return response_json

        except httpx.HTTPStatusError as e:
            duration = time.perf_counter() - start_time
            BESTTIME_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            BESTTIME_API_ERRORS_TOTAL.labels(endpoint=endpoint, error_type="http_error").inc()
            logger.error(f"[BestTimeAPIClient] HTTP error on {method} {endpoint}: {e}")
            raise
        except httpx.TimeoutException as e:
            duration = time.perf_counter() - start_time
            BESTTIME_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            BESTTIME_API_ERRORS_TOTAL.labels(endpoint=endpoint, error_type="timeout").inc()
            logger.error(f"[BestTimeAPIClient] Timeout on {method} {endpoint}: {e}")
            raise
        except httpx.RequestError as e:
            duration = time.perf_counter() - start_time
            BESTTIME_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            BESTTIME_API_ERRORS_TOTAL.labels(endpoint=endpoint, error_type="connection_error").inc()
            logger.error(f"[BestTimeAPIClient] Request error on {method} {endpoint}: {e}")
            raise

    async def venue_filter(self, params: VenueFilterParams) -> VenueFilterResponse:
        """Call GET /venues/filter with given parameters.

        This is the preferred endpoint for venue discovery.

        Args:
            params: VenueFilterParams object with filter criteria

        Returns:
            VenueFilterResponse with matching venues
        """
        query_params = params.to_query_params()
        # Add private API key to query string
        query_params["api_key_private"] = self.api_key_private

        logger.info(f"[BestTimeAPIClient] Calling venue_filter with {len(query_params)} params")

        response_data = await self._request("GET", "/venues/filter", params=query_params)

        response = VenueFilterResponse(**response_data)
        logger.info(
            f"[BestTimeAPIClient] venue_filter success: status={response.status}, "
            f"venues_n={response.venues_n}"
        )

        return response

    async def get_live_forecast(
        self,
        venue_id: Optional[str] = None,
        venue_name: Optional[str] = None,
        venue_address: Optional[str] = None,
    ) -> LiveForecastResponse:
        """Retrieve live busyness forecast for a venue.

        Args:
            venue_id: Venue ID (preferred)
            venue_name: Venue name (required if venue_id not provided)
            venue_address: Venue address (required if venue_id not provided)

        Returns:
            LiveForecastResponse with live busyness data

        Raises:
            ValueError: If neither venue_id nor (venue_name + venue_address) provided
        """
        # Build query parameters
        query_params = {"api_key_private": self.api_key_private}

        if venue_id:
            query_params["venue_id"] = venue_id
        else:
            if not venue_name or not venue_address:
                raise ValueError(
                    "Either venue_id or both venue_name and venue_address must be provided"
                )
            query_params["venue_name"] = venue_name
            query_params["venue_address"] = venue_address

        # Construct endpoint with query params
        response_data = await self._request(
            "POST", "/forecasts/live", params=query_params
        )

        return LiveForecastResponse(**response_data)

    async def get_week_raw_forecast(self, venue_id: str) -> WeekRawResponse:
        """Retrieve full weekly raw forecast for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            WeekRawResponse with 7 days of hourly forecast data

        Raises:
            ValueError: If venue_id is empty
        """
        if not venue_id:
            raise ValueError("venue_id must be provided")

        query_params = {
            "api_key_public": self.api_key_public,
            "venue_id": venue_id,
        }

        response_data = await self._request(
            "GET", "/forecasts/week/raw2", params=query_params
        )

        return WeekRawResponse(**response_data)

    # Legacy methods (kept for compatibility, but venue_filter is preferred)

    async def get_venues_nearby(
        self, lat: float, lng: float
    ) -> dict:  # SearchVenuesResponse not implemented yet
        """Kick off background venue search (legacy endpoint).

        Note: This is a legacy endpoint. Use venue_filter() instead for direct results.

        Args:
            lat: Latitude
            lng: Longitude

        Returns:
            Search job response with job_id and collection_id
        """
        query_params = {
            "api_key_private": self.api_key_private,
            "q": "most popular bars, nightclubs or pubs to party and dance in recife and are open now",
            "num": "20",
            "lat": str(lat),
            "lng": str(lng),
            "opened": "now",
            "radius": "10000",
            "live": "true",
        }

        logger.warning(
            "[BestTimeAPIClient] get_venues_nearby is a legacy method. "
            "Consider using venue_filter() instead."
        )

        response_data = await self._request(
            "POST", "/venues/search", params=query_params
        )

        return response_data

    async def add_venue_to_account(
        self, venue_name: str, venue_address: str
    ) -> NewVenueResponse:
        """Register a venue in our BestTime account inventory.

        Calls POST /forecasts which is BestTime's "add new venue" endpoint.
        On success returns the venue_info (id, name, address, lat, lng,
        timezone, rating, reviews, price_level) and the 7-day analysis when
        available. On geocoder failure or monthly-cap-exceeded, BestTime
        returns HTTP 4xx with body {"status":"Error","message":"..."}.

        We treat HTTP 5xx and transport errors as raise-worthy (the caller
        knows BestTime is unhealthy). HTTP 4xx with a parseable Error body
        is returned as a NewVenueResponse with status="Error" so the
        handler can branch into the geo-fallback path.
        """
        query_params = {
            "api_key_private": self.api_key_private,
            "venue_name": venue_name,
            "venue_address": venue_address,
        }
        endpoint = "/forecasts"
        url = f"{self.base_url}{endpoint}"
        start_time = time.perf_counter()
        try:
            response = await self.client.request(
                method="POST",
                url=url,
                params=query_params,
                headers={"Content-Type": "application/json"},
            )
            duration = time.perf_counter() - start_time
            BESTTIME_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)

            # 5xx is non-recoverable: raise so the handler returns 502
            # without attempting the geo fallback.
            if response.status_code >= 500:
                BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
                BESTTIME_API_ERRORS_TOTAL.labels(
                    endpoint=endpoint, error_type="http_5xx"
                ).inc()
                response.raise_for_status()

            try:
                body = response.json()
            except Exception:
                BESTTIME_API_ERRORS_TOTAL.labels(
                    endpoint=endpoint, error_type="invalid_json"
                ).inc()
                raise

            parsed = NewVenueResponse.model_validate(body)
            if parsed.is_ok():
                BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="success").inc()
            else:
                BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
                logger.warning(
                    f"[BestTimeAPIClient] add_venue_to_account non-OK: "
                    f"status={parsed.status} message={parsed.message!r}"
                )
            return parsed
        except httpx.HTTPStatusError as e:
            BESTTIME_API_ERRORS_TOTAL.labels(
                endpoint=endpoint, error_type="http_error"
            ).inc()
            logger.error(f"[BestTimeAPIClient] HTTP error on POST {endpoint}: {e}")
            raise
        except httpx.TimeoutException as e:
            BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            BESTTIME_API_ERRORS_TOTAL.labels(
                endpoint=endpoint, error_type="timeout"
            ).inc()
            logger.error(f"[BestTimeAPIClient] Timeout on POST {endpoint}: {e}")
            raise
        except httpx.RequestError as e:
            BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            BESTTIME_API_ERRORS_TOTAL.labels(
                endpoint=endpoint, error_type="connection_error"
            ).inc()
            logger.error(f"[BestTimeAPIClient] Request error on POST {endpoint}: {e}")
            raise

    async def list_account_inventory(
        self, page_size: int = 1000
    ) -> AsyncIterator[AccountInventoryVenue]:
        """Paginate GET /api/v1/venues, yielding every venue in our account inventory.

        This endpoint does not consume BestTime credits — it just enumerates
        venues already registered to the API key. Yields one venue at a
        time; the caller decides how to batch or filter.
        """
        endpoint = "/venues"
        page = 0
        while True:
            params = {
                "api_key_private": self.api_key_private,
                "limit": page_size,
                "page": page,
            }
            try:
                data = await self._request("GET", endpoint, params=params)
            except Exception as e:
                logger.error(
                    f"[BestTimeAPIClient] list_account_inventory page={page} failed: {e}"
                )
                raise
            if not isinstance(data, list) or not data:
                return
            for row in data:
                try:
                    yield AccountInventoryVenue.model_validate(row)
                except Exception as e:
                    logger.warning(
                        f"[BestTimeAPIClient] Skipping bad inventory row on page "
                        f"{page}: {e}"
                    )
                    continue
            if len(data) < page_size:
                return
            page += 1

    async def get_venue_search_progress(
        self, job_id: str, collection_id: Optional[str] = None
    ) -> dict:  # SearchProgressResponse not implemented yet
        """Poll background search job progress (legacy endpoint).

        Args:
            job_id: Job identifier from get_venues_nearby()
            collection_id: Collection identifier (optional)

        Returns:
            Progress response with venues when job_finished=true
        """
        query_params = {"job_id": job_id}
        if collection_id:
            query_params["collection_id"] = collection_id

        response_data = await self._request("GET", "/venues/progress", params=query_params)

        return response_data
