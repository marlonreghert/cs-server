"""Handler for POST /admin/venues/by-address.

Resolves a Google-Places-sourced (venue_name, venue_address, lat, lng)
into a venue in our BestTime account inventory + Redis geo index,
respecting the monthly new-venue quota and the manual-add reserve.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.dao.redis_venue_dao import RedisVenueDAO
from app.metrics import (
    ADD_VENUE_BY_ADDRESS_TOTAL,
    VENUE_MONTHLY_NEW_COUNT,
)
from app.models import (
    Venue,
    VenueFilterParams,
)
from app.services.venue_budget_service import VenueBudgetService

logger = logging.getLogger(__name__)


VENUE_LOOKUP_BY_ADDRESS_KEY_V1 = "venue_lookup_by_address_v1:{hash}"
DEFAULT_FALLBACK_RADIUS_M = 200
MAX_FALLBACK_RADIUS_M = 500


class AddVenueByAddressRequest(BaseModel):
    """Request body for POST /admin/venues/by-address."""

    venue_name: str = Field(..., min_length=1, max_length=256)
    venue_address: str = Field(..., min_length=1, max_length=1024)
    venue_lat: float = Field(..., ge=-90.0, le=90.0)
    venue_lng: float = Field(..., ge=-180.0, le=180.0)
    place_id: Optional[str] = None
    fallback_radius_meters: Optional[int] = Field(
        default=None, ge=1, le=MAX_FALLBACK_RADIUS_M
    )

    model_config = ConfigDict(extra="ignore")


@dataclass
class AddVenueOutcome:
    status_code: int
    body: dict


def _address_hash(venue_name: str, venue_address: str) -> str:
    return hashlib.sha1(
        f"{venue_name.strip().lower()}|{venue_address.strip().lower()}".encode("utf-8")
    ).hexdigest()


class AddVenueHandler:
    def __init__(
        self,
        venue_dao: RedisVenueDAO,
        besttime_api,
        budget_service: VenueBudgetService,
        redis_client,
    ) -> None:
        self.venue_dao = venue_dao
        self.besttime = besttime_api
        self.budget = budget_service
        self.redis = redis_client

    async def add(self, request: AddVenueByAddressRequest) -> AddVenueOutcome:
        radius_m = request.fallback_radius_meters or DEFAULT_FALLBACK_RADIUS_M

        # 1. Address-hash short circuit.
        existing_id = self._lookup_cached_venue_id(
            request.venue_name, request.venue_address
        )
        if existing_id:
            persisted = self.venue_dao.get_venue(existing_id)
            if persisted is not None:
                ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="already_exists").inc()
                return AddVenueOutcome(
                    status_code=200,
                    body=self._already_exists_body(persisted),
                )

        # 2. Geo-cache short circuit (handles inventory-sync hits).
        geo_hit = self._geo_lookup(
            request.venue_name, request.venue_lat, request.venue_lng, radius_m
        )
        if geo_hit is not None:
            self._save_address_cache(
                request.venue_name, request.venue_address, geo_hit.venue_id
            )
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="already_exists").inc()
            return AddVenueOutcome(
                status_code=200,
                body=self._already_exists_body(geo_hit),
            )

        # 3. Reserve a monthly slot before calling BestTime.
        granted, snap = self.budget.reserve_manual_slot()
        if not granted:
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="quota_exhausted").inc()
            return AddVenueOutcome(
                status_code=429,
                body={
                    "detail": "Monthly venue quota exhausted",
                    "year_month": snap.year_month if snap else "unknown",
                    "month_counter": snap.month_counter if snap else None,
                    "quota": snap.quota if snap else None,
                },
            )

        # 4. Call BestTime POST /forecasts.
        try:
            response = await self.besttime.add_venue_to_account(
                request.venue_name, request.venue_address
            )
        except Exception as e:
            self.budget.release_manual_slot()
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="besttime_error").inc()
            logger.error(
                f"[AddVenueHandler] BestTime transport error: {type(e).__name__}: {e}"
            )
            return AddVenueOutcome(
                status_code=502,
                body={"detail": f"BestTime is unavailable: {type(e).__name__}"},
            )

        if not _response_ok(response):
            # Recoverable failure: release the reservation and try the geo
            # fallback before we give up.
            self.budget.release_manual_slot()
            return await self._geo_fallback(request, radius_m, response)

        # 5. Success: persist + cache + record + report.
        persisted_venue = _persist_new_venue(self.venue_dao, response)

        # Best-effort cache of week_raw days if BestTime included them.
        for day in response.analysis or []:
            try:
                self.venue_dao.set_week_raw_forecast(persisted_venue.venue_id, day)
            except Exception as e:
                logger.warning(
                    f"[AddVenueHandler] week_raw cache failed for "
                    f"{persisted_venue.venue_id} day={day.day_int}: {e}"
                )

        # Best-effort inline live forecast fetch.
        await self._inline_live_forecast(persisted_venue.venue_id)

        # Cache the deterministic name+address lookup for next time.
        self._save_address_cache(
            request.venue_name, request.venue_address, persisted_venue.venue_id
        )

        # Update the gauge for observability.
        VENUE_MONTHLY_NEW_COUNT.set(self.budget.get_snapshot().month_counter)

        ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="created").inc()
        return AddVenueOutcome(
            status_code=201,
            body={
                "status": "created",
                "venue_id": persisted_venue.venue_id,
                "venue_name": persisted_venue.venue_name,
                "venue_address": persisted_venue.venue_address,
                "venue_lat": persisted_venue.venue_lat,
                "venue_lng": persisted_venue.venue_lng,
                "source": "besttime_new",
            },
        )

    # ------------------------------------------------------------------

    def _lookup_cached_venue_id(
        self, venue_name: str, venue_address: str
    ) -> Optional[str]:
        key = VENUE_LOOKUP_BY_ADDRESS_KEY_V1.format(
            hash=_address_hash(venue_name, venue_address)
        )
        try:
            return self.redis.get(key)
        except Exception as e:
            logger.warning(f"[AddVenueHandler] address-cache get failed: {e}")
            return None

    def _save_address_cache(
        self, venue_name: str, venue_address: str, venue_id: str
    ) -> None:
        key = VENUE_LOOKUP_BY_ADDRESS_KEY_V1.format(
            hash=_address_hash(venue_name, venue_address)
        )
        try:
            self.redis.set(key, venue_id)
        except Exception as e:
            logger.warning(f"[AddVenueHandler] address-cache set failed: {e}")

    def _geo_lookup(
        self, venue_name: str, lat: float, lng: float, radius_m: int
    ) -> Optional[Venue]:
        """Check the Redis geo index for a name-matching venue within radius."""
        try:
            nearby = self.venue_dao.get_nearby_venues(lat, lng, radius_m / 1000.0)
        except Exception as e:
            logger.warning(f"[AddVenueHandler] geo lookup failed: {e}")
            return None
        folded = venue_name.strip().lower()
        for venue in nearby:
            name = (venue.venue_name or "").strip().lower()
            if not name:
                continue
            if folded == name or folded in name or name in folded:
                return venue
        return None

    async def _geo_fallback(
        self,
        request: AddVenueByAddressRequest,
        radius_m: int,
        besttime_response,
    ) -> AddVenueOutcome:
        """Call /venues/filter for the request coordinate; match by name."""
        try:
            filter_response = await self.besttime.venue_filter(
                VenueFilterParams(
                    busy_min=0,
                    lat=request.venue_lat,
                    lng=request.venue_lng,
                    radius=radius_m,
                    foot_traffic="both",
                    limit=25,
                )
            )
        except Exception as e:
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="besttime_error").inc()
            logger.error(
                f"[AddVenueHandler] geo fallback /venues/filter failed: {e}"
            )
            return AddVenueOutcome(
                status_code=502,
                body={"detail": f"BestTime geo fallback unavailable: {type(e).__name__}"},
            )

        match = _find_name_match(filter_response.venues or [], request.venue_name)
        if match is None:
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(
                result="besttime_rejected_no_geo_match"
            ).inc()
            return AddVenueOutcome(
                status_code=502,
                body={
                    "detail": (
                        "BestTime rejected the address and the geo fallback "
                        f"found no matching venue near "
                        f"({request.venue_lat},{request.venue_lng}) within {radius_m}m"
                    ),
                    "besttime_status": besttime_response.status,
                    "besttime_message": besttime_response.message,
                    "candidates_seen": len(filter_response.venues or []),
                },
            )

        # Upsert the matched venue if not already in our geo index.
        existing = self.venue_dao.get_venue(match.venue_id)
        was_new = existing is None
        if was_new:
            venue = Venue(
                processed=True,
                forecast=True,
                venue_id=match.venue_id,
                venue_name=match.venue_name,
                venue_address=match.venue_address,
                venue_lat=match.venue_lat,
                venue_lng=match.venue_lng,
                venue_type=match.venue_type,
                rating=match.rating,
                reviews=match.reviews,
                price_level=match.price_level,
            )
            self.venue_dao.upsert_venue(venue)
            # Count toward monthly budget only when truly new.
            self.budget.record_new_venue_from_discovery()
        VENUE_MONTHLY_NEW_COUNT.set(self.budget.get_snapshot().month_counter)
        self._save_address_cache(
            request.venue_name, request.venue_address, match.venue_id
        )

        ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="matched_via_geo_fallback").inc()
        return AddVenueOutcome(
            status_code=200,
            body={
                "status": "matched_via_geo_fallback",
                "venue_id": match.venue_id,
                "venue_name": match.venue_name,
                "venue_address": match.venue_address,
                "venue_lat": match.venue_lat,
                "venue_lng": match.venue_lng,
                "source": "venues_filter_radius",
            },
        )

    async def _inline_live_forecast(self, venue_id: str) -> None:
        try:
            live = await self.besttime.get_live_forecast(venue_id=venue_id)
        except Exception as e:
            logger.warning(
                f"[AddVenueHandler] inline live forecast failed for {venue_id}: {e}"
            )
            return
        status = getattr(live, "status", None) or (live.get("status") if isinstance(live, dict) else None)
        available = False
        analysis = getattr(live, "analysis", None) or (live.get("analysis") if isinstance(live, dict) else None)
        if analysis is not None:
            if hasattr(analysis, "venue_live_busyness_available"):
                available = bool(analysis.venue_live_busyness_available)
            elif isinstance(analysis, dict):
                available = bool(analysis.get("venue_live_busyness_available"))
        if status != "OK" or not available:
            return
        try:
            self.venue_dao.set_live_forecast(live)
        except Exception as e:
            logger.warning(
                f"[AddVenueHandler] live forecast persist failed for {venue_id}: {e}"
            )

    def _already_exists_body(self, venue: Venue) -> dict:
        return {
            "status": "already_exists",
            "venue_id": venue.venue_id,
            "venue_name": venue.venue_name,
            "venue_address": venue.venue_address,
            "venue_lat": venue.venue_lat,
            "venue_lng": venue.venue_lng,
        }


def _response_ok(response) -> bool:
    if response is None:
        return False
    if hasattr(response, "is_ok"):
        return response.is_ok()
    # Dict fallback for callers that hand-roll responses (BDD harness).
    if isinstance(response, dict):
        info = response.get("venue_info") or {}
        return (
            response.get("status") == "OK"
            and bool(info.get("venue_id"))
        )
    return False


def _persist_new_venue(venue_dao: RedisVenueDAO, response) -> Venue:
    info = response.venue_info if hasattr(response, "venue_info") else None
    if info is None and isinstance(response, dict):
        info = response.get("venue_info") or {}
    # Normalise the venue_info shape into a Venue.
    venue_id = _get(info, "venue_id")
    venue_lat = _get(info, "venue_lat") or 0.0
    venue_lng = _get(info, "venue_lng")
    if venue_lng is None:
        venue_lng = _get(info, "venue_lon") or 0.0
    venue = Venue(
        processed=True,
        forecast=True,
        venue_id=venue_id,
        venue_name=_get(info, "venue_name") or "",
        venue_address=_get(info, "venue_address") or "",
        venue_lat=float(venue_lat or 0.0),
        venue_lng=float(venue_lng or 0.0),
        rating=_get(info, "rating"),
        reviews=_get(info, "reviews"),
        price_level=_get(info, "price_level"),
    )
    venue_dao.upsert_venue(venue)
    return venue


def _get(source, key):
    if source is None:
        return None
    if hasattr(source, key):
        return getattr(source, key)
    if isinstance(source, dict):
        return source.get(key)
    return None


def _find_name_match(venues: list, venue_name: str):
    folded = venue_name.strip().lower()
    for v in venues:
        n = (getattr(v, "venue_name", None) or "").strip().lower()
        if not n:
            continue
        if folded == n or folded in n or n in folded:
            return v
    return None
