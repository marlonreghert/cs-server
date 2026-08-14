"""Apify ACCOUNT-level usage lookup — the second of the deep review crawl's
two budget gates. See plans/260813_deep-review-corpus.md.

Prod's Apify account is plan STARTER: `maxMonthlyUsageUsd` is a HARD cap
shared with the production Instagram/events crawl and photo scrapers, not a
soft warning. `ReviewCrawlBudgetDao` (the local monthly review counter) only
bounds THIS feature's own spend; it has no idea how much of the SHARED
account ceiling production has already used. This client answers that
question so `DeepReviewCrawlService` can refuse a batch before it would push
the shared account into a cap that starts failing production crawls too.

Two calls, per Apify's REST API: `GET /v2/users/me` for the account's
`maxMonthlyUsageUsd` ceiling, and `GET /v2/users/me/usage/monthly` for
`totalUsageCreditsUsdBeforeVolumeDiscount` (usage already billed this
cycle). Both are read-only and spend nothing themselves.

Both field names are CONFIRMED, not assumed: read directly off the live prod
Apify account on 2026-08-13 (STARTER plan, `maxMonthlyUsageUsd: 29`, the
2026-07-29 -> 2026-08-28 cycle showing `totalUsageCreditsUsdBeforeVolumeDiscount`
of $12.45 — the exact numbers plans/260813_deep-review-corpus.md's Evidence
section cites). This is unlike the reviews actor's dataset item shape
(app/api/apify_gmaps_reviews_client.py), which genuinely is unverified pending
the plan's 2b one-venue trial. The defensive `limit is None or used is None`
fallback below stays regardless — a confirmed shape today does not guarantee
Apify never adds/renames a field later, and "unknown headroom" must still
refuse rather than guess.

`get_headroom_usd` returns None on ANY failure — a bad token, a timeout, an
unexpected response shape. The caller MUST treat None as ZERO headroom and
refuse the batch: an unknown headroom is the one case where guessing "it's
probably fine" is exactly the failure mode the reserve exists to prevent.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"


class ApifyAccountUsageClient:
    def __init__(self, api_token: str, timeout: float = 15.0):
        self.api_token = api_token
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self.client.aclose()

    async def get_headroom_usd(self) -> Optional[float]:
        """Remaining USD headroom on the account's shared monthly Apify
        usage cap this billing cycle (`maxMonthlyUsageUsd` minus usage
        already billed), or None on any lookup failure."""
        try:
            me = await self.client.get(
                f"{APIFY_API_BASE}/users/me", params={"token": self.api_token}
            )
            me.raise_for_status()
            # `maxMonthlyUsageUsd` lives on data.PLAN, not on data directly.
            # Confirmed against the live prod account on 2026-08-14, when the
            # 2b trial's first run refused with `headroom=None`: reading it off
            # `data` yielded None on every call, so the gate could never open.
            # The top-level read is kept as a fallback in case the shape ever
            # flattens; `plan` wins when both are present.
            me_data = (me.json() or {}).get("data") or {}
            plan = me_data.get("plan") or {}
            limit = plan.get("maxMonthlyUsageUsd", me_data.get("maxMonthlyUsageUsd"))

            usage = await self.client.get(
                f"{APIFY_API_BASE}/users/me/usage/monthly",
                params={"token": self.api_token},
            )
            usage.raise_for_status()
            used = (usage.json() or {}).get("data", {}).get(
                "totalUsageCreditsUsdBeforeVolumeDiscount"
            )

            if limit is None or used is None:
                logger.error(
                    "[ApifyAccountUsage] usage response missing expected fields "
                    f"(limit={limit!r}, used={used!r})"
                )
                return None
            return float(limit) - float(used)
        except Exception as e:  # noqa: BLE001 — every failure mode is "unknown headroom"
            logger.error(f"[ApifyAccountUsage] headroom lookup failed: {e}")
            return None
