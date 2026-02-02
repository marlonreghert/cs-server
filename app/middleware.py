"""FastAPI middleware for Prometheus metrics instrumentation."""
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_PROGRESS,
    HTTP_REQUEST_SIZE_BYTES,
    HTTP_RESPONSE_SIZE_BYTES,
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Middleware to collect HTTP request metrics for Prometheus."""

    # Endpoints to exclude from metrics (like /metrics itself)
    EXCLUDE_PATHS = {"/metrics", "/health", "/ping"}

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process request and collect metrics."""
        path = request.url.path
        method = request.method

        # Skip metrics for excluded paths
        if path in self.EXCLUDE_PATHS:
            return await call_next(request)

        # Normalize endpoint for metrics (avoid high cardinality from path params)
        endpoint = self._normalize_endpoint(path)

        # Track request size
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                HTTP_REQUEST_SIZE_BYTES.labels(
                    method=method, endpoint=endpoint
                ).observe(int(content_length))
            except ValueError:
                pass

        # Track in-progress requests
        HTTP_REQUESTS_IN_PROGRESS.labels(method=method, endpoint=endpoint).inc()

        # Track request timing
        start_time = time.perf_counter()

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            # Record duration
            duration = time.perf_counter() - start_time
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=method, endpoint=endpoint
            ).observe(duration)

            # Decrement in-progress
            HTTP_REQUESTS_IN_PROGRESS.labels(method=method, endpoint=endpoint).dec()

            # Record request count with status
            HTTP_REQUESTS_TOTAL.labels(
                method=method, endpoint=endpoint, status_code=str(status_code)
            ).inc()

        # Track response size
        response_size = response.headers.get("content-length")
        if response_size:
            try:
                HTTP_RESPONSE_SIZE_BYTES.labels(
                    method=method, endpoint=endpoint
                ).observe(int(response_size))
            except ValueError:
                pass

        return response

    def _normalize_endpoint(self, path: str) -> str:
        """Normalize URL path to avoid high cardinality from path parameters.

        Converts paths like /v1/venues/abc123 to /v1/venues/{id}
        """
        # Split path into segments
        segments = path.strip("/").split("/")

        # Known API patterns
        normalized = []
        for i, segment in enumerate(segments):
            # Check if this looks like an ID (UUID-like or long alphanumeric)
            if self._is_id_segment(segment):
                normalized.append("{id}")
            else:
                normalized.append(segment)

        return "/" + "/".join(normalized) if normalized else "/"

    def _is_id_segment(self, segment: str) -> bool:
        """Check if a path segment looks like an ID."""
        # UUID pattern or long alphanumeric strings
        if len(segment) >= 20 and segment.replace("-", "").isalnum():
            return True
        # Pure numeric IDs
        if segment.isdigit() and len(segment) >= 5:
            return True
        return False
