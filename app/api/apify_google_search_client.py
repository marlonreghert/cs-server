"""Google results via Apify's google-search-scraper.

Exists because the venues we cannot reach any other way are still on Google:
191 Recife venues have no website at all, so every free tier has nothing to read,
and Instagram's own user search does not surface small Brazilian businesses. A
plain web search finds them, because Google indexes the Instagram profile page.

Runs on the Apify token already in use — no new provider.

The actor's input is fussier than the docs suggest: `queries` must be a STRING
(newline-separated for several), not a list, and unknown keys are rejected
outright with a 400. Both were found the hard way against the live API.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from app.metrics import (
    APIFY_API_CALLS_TOTAL,
    APIFY_API_CALL_DURATION_SECONDS,
    APIFY_API_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"
SEARCH_ACTOR = "apify~google-search-scraper"
ENDPOINT_LABEL = "google_search"


class ApifyGoogleSearchClient:
    def __init__(
        self,
        api_token: str,
        *,
        actor: str = SEARCH_ACTOR,
        country_code: str = "br",
        poll_seconds: float = 3.0,
        timeout_seconds: float = 180.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.api_token = api_token
        self.actor = actor
        self.country_code = country_code
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.client = client or httpx.AsyncClient(timeout=60.0)

    async def search(self, query: str, *, results: int = 10) -> list[dict[str, Any]]:
        """Run one search and return its result items. Raises on failure — the
        source above degrades to "no candidate"; swallowing here would hide a
        misconfigured actor behind an ordinary-looking empty result."""
        started = time.time()
        status_label = "error"
        try:
            items = await self._run(query)
            status_label = "success"
            return items[:results]
        finally:
            APIFY_API_CALLS_TOTAL.labels(
                endpoint=ENDPOINT_LABEL, status=status_label
            ).inc()
            APIFY_API_CALL_DURATION_SECONDS.labels(endpoint=ENDPOINT_LABEL).observe(
                time.time() - started
            )

    async def _run(self, query: str) -> list[dict[str, Any]]:
        run = await self.client.post(
            f"{APIFY_API_BASE}/acts/{self.actor}/runs",
            params={"token": self.api_token},
            # Only keys the actor accepts. It 400s on anything it does not know.
            json={
                "queries": query,
                "maxPagesPerQuery": 1,
                "countryCode": self.country_code,
            },
        )
        run.raise_for_status()
        run_id = run.json()["data"]["id"]

        deadline = time.time() + self.timeout_seconds
        state = None
        while time.time() < deadline:
            await asyncio.sleep(self.poll_seconds)
            poll = await self.client.get(
                f"{APIFY_API_BASE}/actor-runs/{run_id}",
                params={"token": self.api_token},
            )
            poll.raise_for_status()
            data = poll.json()["data"]
            state = data["status"]
            if state in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                break

        if state != "SUCCEEDED":
            APIFY_API_ERRORS_TOTAL.labels(
                endpoint=ENDPOINT_LABEL, error_type=str(state or "timeout")
            ).inc()
            raise RuntimeError(f"google search run ended {state}")

        dataset = data["defaultDatasetId"]
        items = await self.client.get(
            f"{APIFY_API_BASE}/datasets/{dataset}/items",
            params={"token": self.api_token},
        )
        items.raise_for_status()
        payload = items.json()
        return payload if isinstance(payload, list) else []

    async def close(self) -> None:
        await self.client.aclose()
