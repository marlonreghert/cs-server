"""BestTime API client with async HTTP support."""
import asyncio
import logging
import time
from collections import deque
from typing import Callable, Optional
import httpx
from pydantic import ValidationError

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
    BESTTIME_SEARCH_RATE_LIMIT_TOTAL,
)

logger = logging.getLogger(__name__)


class BestTimeInvalidResponseError(Exception):
    """BestTime answered 2xx but the body's envelope (status / venue_info)
    cannot be parsed. Distinct from transport errors so callers can tell a
    parse bug on our side from a BestTime outage."""


class BestTimeRateLimitedError(Exception):
    """A BestTime venue-search call could not proceed within the configured
    rate-limit wait budget (nothing was sent, no search quota drawn), or
    BestTime kept answering HTTP 429 after bounded retries. Retryable later —
    callers should surface it as a temporary BestTime condition, never as a
    venue rejection."""


# BestTime's documented Venue Search limits (documentation.besttime.app):
# 30 requests/minute and 300 requests/hour. The create call (POST /forecasts)
# draws the same "Venue Search" monthly quota, so it is paced with the family.
_MINUTE_WINDOW_SECONDS = 60.0
_HOUR_WINDOW_SECONDS = 3600.0


def _looks_like_monthly_cap_body(response: httpx.Response) -> bool:
    """True when a 429 body carries BestTime's monthly unique-venue cap message
    (mirrors the handler's `_is_monthly_cap_rejection` keywords). Cap answers
    must flow through the normal parse path — they are a terminal quota state,
    not a transient rate limit worth retrying."""
    try:
        message = response.json().get("message")
    except Exception:
        return False
    if not isinstance(message, str):
        return False
    low = message.lower()
    return "monthly venues" in low or "venue counter will reset" in low


class _SearchRateLimiter:
    """Client-side sliding-window pacing for the venue-search family.

    Waits (bounded by max_wait_seconds) until both the per-minute and per-hour
    windows have room; a wait that would exceed the budget raises
    BestTimeRateLimitedError before anything is sent. Clock and sleep are
    injectable so tests never sleep for real.
    """

    def __init__(
        self,
        per_minute: int,
        per_hour: int,
        max_wait_seconds: float,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], "asyncio.Future"] = asyncio.sleep,
    ):
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.max_wait_seconds = max_wait_seconds
        self._time = time_func
        self._sleep = sleep_func
        self._minute: deque[float] = deque()
        self._hour: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _required_wait(self, now: float) -> float:
        while self._minute and now - self._minute[0] >= _MINUTE_WINDOW_SECONDS:
            self._minute.popleft()
        while self._hour and now - self._hour[0] >= _HOUR_WINDOW_SECONDS:
            self._hour.popleft()
        wait = 0.0
        if self.per_minute > 0 and len(self._minute) >= self.per_minute:
            wait = max(wait, self._minute[0] + _MINUTE_WINDOW_SECONDS - now)
        if self.per_hour > 0 and len(self._hour) >= self.per_hour:
            wait = max(wait, self._hour[0] + _HOUR_WINDOW_SECONDS - now)
        return wait

    async def acquire(self, endpoint: str) -> None:
        waited_total = 0.0
        while True:
            async with self._lock:
                now = self._time()
                wait = self._required_wait(now)
                if wait <= 0:
                    self._minute.append(now)
                    self._hour.append(now)
                    return
                if waited_total + wait > self.max_wait_seconds:
                    BESTTIME_SEARCH_RATE_LIMIT_TOTAL.labels(
                        endpoint=endpoint, event="rejected"
                    ).inc()
                    raise BestTimeRateLimitedError(
                        f"venue-search rate window full; needs {wait:.1f}s more "
                        f"(budget {self.max_wait_seconds:.0f}s exhausted)"
                    )
            BESTTIME_SEARCH_RATE_LIMIT_TOTAL.labels(
                endpoint=endpoint, event="waited"
            ).inc()
            logger.info(
                f"[BestTimeAPIClient] pacing {endpoint}: waiting {wait:.1f}s "
                f"for the venue-search rate window"
            )
            await self._sleep(wait)
            waited_total += wait


class BestTimeAPIClient:
    """Async HTTP client for BestTime API."""

    def __init__(
        self,
        base_url: str,
        api_key_public: str,
        api_key_private: str,
        timeout: float = 10.0,
        add_venue_timeout: float = 60.0,
        search_rate_per_minute: int = 30,
        search_rate_per_hour: int = 300,
        rate_max_wait_seconds: float = 75.0,
    ):
        """Initialize BestTime API client.

        Args:
            base_url: Base URL for BestTime API (e.g., "https://besttime.app/api/v1")
            api_key_public: Public API key
            api_key_private: Private API key
            timeout: Request timeout in seconds for the frequent live/read calls
            add_venue_timeout: Request timeout in seconds for the slow, synchronous
                POST /forecasts "create venue" call (add_venue_to_account). Kept
                separate and larger because BestTime builds a fresh forecast on
                that request and is far slower than the read paths.
            search_rate_per_minute / search_rate_per_hour: BestTime's documented
                Venue Search limits (30/min, 300/hour); paces the search-family
                calls client-side. <=0 disables that window.
            rate_max_wait_seconds: longest total pacing/429 wait per call before
                failing fast with BestTimeRateLimitedError.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key_public = api_key_public
        self.api_key_private = api_key_private
        self.timeout = timeout
        self.add_venue_timeout = add_venue_timeout
        self.rate_max_wait_seconds = rate_max_wait_seconds
        self._search_limiter = _SearchRateLimiter(
            per_minute=search_rate_per_minute,
            per_hour=search_rate_per_hour,
            max_wait_seconds=rate_max_wait_seconds,
        )

        # Create async HTTP client with connection pooling
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def close(self):
        """Close the HTTP client and clean up resources."""
        await self.client.aclose()

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
        """Wait before retrying a 429: honor Retry-After when parseable, else
        exponential backoff (1s, 2s, ...)."""
        header = response.headers.get("retry-after")
        if header is not None:
            try:
                return max(0.0, float(header))
            except ValueError:
                pass
        return float(2**attempt)

    async def _send_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict],
        endpoint: str,
        json_body: Optional[dict] = None,
        timeout: Optional[float] = None,
        retry_429: bool = False,
        stop_retry_on: Optional[Callable[[httpx.Response], bool]] = None,
        retry_log_suffix: str = "",
    ) -> httpx.Response:
        """Send the request, applying the bounded, Retry-After-aware 429 retry.

        The single retry loop shared by `_request` (search-family reads, via
        ``retry_429``) and `add_venue_to_account` (the create, via ``timeout`` +
        ``stop_retry_on``), so the two can no longer drift. Returns the final
        `httpx.Response`; the caller owns all response parsing, ``raise_for_status``,
        and success/error result metrics + logs. Transport errors from
        ``client.request`` propagate to the caller's own except blocks unchanged.

        Args:
            timeout: per-call timeout passed to ``client.request`` (omitted when
                None so read calls inherit the client-wide default).
            retry_429: retry HTTP 429 answers (bounded, Retry-After-aware).
            stop_retry_on: predicate on a 429 response that, when true, breaks the
                loop and surfaces that response as terminal (never retried) — the
                monthly-cap 429 for the create.
            retry_log_suffix: appended after ``<method> <endpoint>`` in the retry
                warning (e.g. " (create)"), preserving the original messages.

        Raises:
            BestTimeRateLimitedError: bounded 429 retries were exhausted.
        """
        request_kwargs: dict = {
            "method": method,
            "url": url,
            "params": params,
            "json": json_body,
            "headers": {"Content-Type": "application/json"},
        }
        if timeout is not None:
            request_kwargs["timeout"] = timeout

        attempt = 0
        waited = 0.0
        while True:
            response = await self.client.request(**request_kwargs)
            if not (retry_429 and response.status_code == 429):
                break
            # A 429 the predicate claims as terminal (e.g. the monthly-cap body)
            # flows to the caller's normal parse path — never retried.
            if stop_retry_on is not None and stop_retry_on(response):
                break
            wait = self._retry_after_seconds(response, attempt)
            if attempt >= 2 or waited + wait > self.rate_max_wait_seconds:
                BESTTIME_SEARCH_RATE_LIMIT_TOTAL.labels(
                    endpoint=endpoint, event="rejected"
                ).inc()
                BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
                raise BestTimeRateLimitedError(
                    f"BestTime kept answering 429 on {method} {endpoint}"
                )
            BESTTIME_SEARCH_RATE_LIMIT_TOTAL.labels(
                endpoint=endpoint, event="retry_429"
            ).inc()
            logger.warning(
                f"[BestTimeAPIClient] 429 on {method} {endpoint}{retry_log_suffix}; "
                f"retrying in {wait:.1f}s (attempt {attempt + 1}/3)"
            )
            await asyncio.sleep(wait)
            waited += wait
            attempt += 1

        return response

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        retry_429: bool = False,
    ) -> dict:
        """Make an HTTP request to the BestTime API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            params: Query parameters
            json_body: JSON request body
            retry_429: retry HTTP 429 answers (bounded, Retry-After-aware) —
                used by the venue-search-family calls, which BestTime rate
                limits at 30/min and 300/hour.

        Returns:
            JSON response as dict

        Raises:
            httpx.HTTPStatusError: If response status is not 2xx
            httpx.RequestError: If request fails
            BestTimeRateLimitedError: retry_429 exhausted its bounded retries
        """
        url = f"{self.base_url}{endpoint}"

        logger.debug(f"[BestTimeAPIClient] {method} {url} params={params} body={json_body}")

        start_time = time.perf_counter()

        try:
            response = await self._send_with_retry(
                method,
                url,
                params=params,
                endpoint=endpoint,
                json_body=json_body,
                retry_429=retry_429,
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

        await self._search_limiter.acquire("/venues/filter")
        try:
            response_data = await self._request(
                "GET", "/venues/filter", params=query_params, retry_429=True
            )
        except httpx.HTTPStatusError as e:
            # BestTime answers a ZERO-MATCH filter with HTTP 404 and a
            # parseable body ({"status":"Error","venues":[],"message":"No
            # venues found matching the filter criteria...", ...}). That is a
            # legitimate empty result, not a transport failure — surfacing it
            # as an error made the add handler's geo-fallback classify
            # terminal "nothing nearby" rejections as retryable
            # besttime_error 502s (prod 2026-07-04, 25 misclassified adds).
            if e.response.status_code != 404:
                raise
            try:
                body = e.response.json()
            except Exception:
                raise e
            if not isinstance(body.get("venues"), list):
                raise
            logger.info(
                "[BestTimeAPIClient] venue_filter: zero matches "
                f"(404-empty envelope): {str(body.get('message'))[:120]}"
            )
            BESTTIME_API_CALLS_TOTAL.labels(
                endpoint="/venues/filter", status="success"
            ).inc()
            return VenueFilterResponse(
                status=body.get("status") or "Error",
                venues=[],
                venues_n=0,
            )

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
        # The create draws the BestTime "Venue Search" quota and shares its
        # 30/min + 300/hour rate limits — pace it with the search family.
        await self._search_limiter.acquire(endpoint)
        start_time = time.perf_counter()
        try:
            # Same bounded 429 retry as the reads (shared _send_with_retry), plus
            # two create-specific deltas: the per-call add-venue timeout, and the
            # monthly-cap 429 treated as terminal (never retried) so it flows to
            # the parse path below and the handler can surface the cap legibly.
            response = await self._send_with_retry(
                "POST",
                url,
                params=query_params,
                endpoint=endpoint,
                timeout=self.add_venue_timeout,
                retry_429=True,
                stop_retry_on=_looks_like_monthly_cap_body,
                retry_log_suffix=" (create)",
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

            try:
                parsed = NewVenueResponse.model_validate(body)
            except ValidationError as e:
                # Analysis parsing is tolerant, so only the envelope
                # (status / message / venue_info) can fail here. Surface it
                # as its own legible failure, not a transport error.
                BESTTIME_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
                BESTTIME_API_ERRORS_TOTAL.labels(
                    endpoint=endpoint, error_type="invalid_response_schema"
                ).inc()
                failed_fields = sorted(
                    {".".join(str(loc) for loc in err["loc"]) for err in e.errors()}
                )
                logger.error(
                    f"[BestTimeAPIClient] POST {endpoint} returned an "
                    f"unparseable envelope; failed fields: {failed_fields}"
                )
                raise BestTimeInvalidResponseError(
                    f"unparseable POST {endpoint} response envelope "
                    f"(failed fields: {failed_fields})"
                ) from e
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
