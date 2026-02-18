"""Apify Google Maps photo scraper client for venue menu photo discovery.

Uses the thescrappa/google-maps-photos-scraper actor via the Apify REST API
to fetch photos from Google Maps.

Accepts either a Google Place ID or a hex data_id (google_id).
Uses async run pattern: start run → poll status → fetch key-value store OUTPUT.

NOTE: As of Feb 2026, most Apify Google Maps photo scrapers are broken due to
Google locking down internal photo RPC endpoints. This client is kept as a
fallback interface for when an actor gets fixed or a better one appears.
"""
import asyncio
import logging
import time
from typing import Optional

import httpx

from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.metrics import (
    APIFY_API_CALLS_TOTAL,
    APIFY_API_CALL_DURATION_SECONDS,
    APIFY_API_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"

# Actor for Google Maps photo scraping
PHOTOS_ACTOR = "thescrappa~google-maps-photos-scraper"

# Polling settings for async runs
POLL_INTERVAL_SECONDS = 5.0
MAX_POLL_ATTEMPTS = 60  # 5 min max wait


class ApifyMenuPhotosClient:
    """Async HTTP client for thescrappa/google-maps-photos-scraper actor."""

    def __init__(self, api_token: str, timeout: float = 30.0):
        self.api_token = api_token
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def fetch_venue_photos(
        self,
        place_id: str,
        max_images: int = 20,
    ) -> Optional[list[dict]]:
        """Fetch photos for a Google Maps place.

        Uses async run pattern (start → poll → fetch OUTPUT from key-value store).

        Args:
            place_id: Google Place ID (e.g., "ChIJ...") or hex data_id
            max_images: Maximum photos to return (actor returns all, we truncate)

        Returns:
            List of photo dicts: [{photo_url, photo_id, latitude, longitude}],
            or None on error.
        """
        run_input = {
            "business_id": place_id,
            "use_cache": False,
        }

        start_time = time.perf_counter()
        endpoint_label = "menu_photos"

        try:
            # 1. Start the run
            run_data = await self._start_run(run_input, endpoint_label)
            if not run_data:
                return None

            run_id = run_data["id"]
            kv_store_id = run_data.get("defaultKeyValueStoreId")

            # 2. Poll until finished
            final_status = await self._poll_run(run_id, endpoint_label)
            if final_status != "SUCCEEDED":
                logger.error(
                    f"[ApifyMenuPhotos] Run {run_id} ended with status: {final_status}"
                )
                APIFY_API_CALLS_TOTAL.labels(
                    endpoint=endpoint_label, status="error"
                ).inc()
                return None

            # 3. Fetch OUTPUT from key-value store
            output = await self._fetch_output(kv_store_id, endpoint_label)

            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                duration
            )
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="success"
            ).inc()

            if not output:
                logger.warning(
                    f"[ApifyMenuPhotos] No output for place {place_id}"
                )
                return None

            photos = output.get("photos", [])
            total = output.get("total", 0)

            if not photos:
                logger.info(
                    f"[ApifyMenuPhotos] No photos found for {place_id} "
                    f"(total reported: {total})"
                )
                return None

            # Truncate to max_images
            photos = photos[:max_images]

            logger.info(
                f"[ApifyMenuPhotos] Got {len(photos)} photos for {place_id} "
                f"(total on Maps: {total})"
            )

            return photos

        except ApifyCreditExhaustedError:
            raise

        except Exception as e:
            duration = time.perf_counter() - start_time
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint_label).observe(
                duration
            )
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=endpoint_label, status="error"
            ).inc()
            logger.error(f"[ApifyMenuPhotos] Error fetching photos for {place_id}: {e}")
            return None

    async def _start_run(
        self, run_input: dict, endpoint_label: str
    ) -> Optional[dict]:
        """Start an async actor run."""
        url = f"{APIFY_API_BASE}/acts/{PHOTOS_ACTOR}/runs"
        params = {"token": self.api_token}

        response = await self.client.post(url, params=params, json=run_input)

        if response.status_code == 402:
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="credit_exhausted"
            ).inc()
            raise ApifyCreditExhaustedError("Apify credits exhausted (402)")

        response.raise_for_status()
        return response.json().get("data")

    async def _poll_run(self, run_id: str, endpoint_label: str) -> str:
        """Poll an actor run until it finishes. Returns final status."""
        url = f"{APIFY_API_BASE}/actor-runs/{run_id}"
        params = {"token": self.api_token}

        for _ in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            try:
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json().get("data", {})
                status = data.get("status", "UNKNOWN")

                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    return status

            except httpx.HTTPError as e:
                logger.warning(
                    f"[ApifyMenuPhotos] Poll error for run {run_id}: {e}"
                )

        logger.error(
            f"[ApifyMenuPhotos] Run {run_id} timed out after "
            f"{MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS}s"
        )
        APIFY_API_ERRORS_TOTAL.labels(
            endpoint=endpoint_label, error_type="timeout"
        ).inc()
        return "TIMED-OUT"

    async def _fetch_output(
        self, kv_store_id: str, endpoint_label: str
    ) -> Optional[dict]:
        """Fetch OUTPUT record from the run's key-value store."""
        url = f"{APIFY_API_BASE}/key-value-stores/{kv_store_id}/records/OUTPUT"
        params = {"token": self.api_token}

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        except httpx.HTTPError as e:
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=endpoint_label, error_type="http_error"
            ).inc()
            logger.error(
                f"[ApifyMenuPhotos] Failed to fetch OUTPUT from {kv_store_id}: {e}"
            )
            return None
