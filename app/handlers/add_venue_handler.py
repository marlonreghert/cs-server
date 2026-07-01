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

from app.api.besttime_client import BestTimeInvalidResponseError
from app.dao.redis_venue_dao import RedisVenueDAO
from app.metrics import (
    ADD_VENUE_BY_ADDRESS_TOTAL,
    VENUE_MONTHLY_NEW_COUNT,
)
from app.models import (
    Venue,
    VenueFilterParams,
)
from app.services.price_signal import derive_price_signal
from app.services.venue_budget_service import VenueBudgetService

logger = logging.getLogger(__name__)


VENUE_LOOKUP_BY_ADDRESS_KEY_V1 = "venue_lookup_by_address_v1:{hash}"
# Geo fallback is a clutter-prone venue_filter; keep its blast radius tight (50m)
# so a rejected add only matches a venue essentially at the requested point.
DEFAULT_FALLBACK_RADIUS_M = 50
MAX_FALLBACK_RADIUS_M = 50


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
        google_places_client=None,
        google_places_enrichment_service=None,
    ) -> None:
        self.venue_dao = venue_dao
        self.besttime = besttime_api
        self.budget = budget_service
        self.redis = redis_client
        # Optional: when configured AND the request carries a `place_id`, the
        # manual-add flow re-sources the price tier from Google (enum + range) via
        # the shared derivation helper. Dependency-aware: absent client / place_id
        # falls back to the BestTime price through the same helper (never 0).
        self.google_places_client = google_places_client
        # Optional: fully Google-enriches the venue inline at add time (type/vibe,
        # hours, reviews, business status, rating) after persist. Absent -> the add
        # still succeeds with the BestTime-baseline price only (degrade-safe).
        self.google_places_enrichment_service = google_places_enrichment_service

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
        except BestTimeInvalidResponseError as e:
            # BestTime answered, but with a body we cannot parse — our parse
            # bug or their contract change, NOT an outage. Keep it legible so
            # operators do not chase a fake BestTime incident.
            self.budget.release_manual_slot()
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="besttime_bad_response").inc()
            logger.error(f"[AddVenueHandler] BestTime bad response: {e}")
            return AddVenueOutcome(
                status_code=502,
                body={"detail": "BestTime returned an unparseable response"},
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
            # Release the reservation either way — BestTime did not add a venue.
            self.budget.release_manual_slot()
            # A monthly-cap rejection is its own legible state: surface BestTime's
            # status/message instead of laundering it through the geo fallback
            # into a misleading "rejected the address" (the originating bug).
            if _is_monthly_cap_rejection(response):
                ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="besttime_monthly_cap").inc()
                snap = self.budget.get_snapshot()
                logger.warning(
                    "[AddVenueHandler] BestTime monthly venue cap reached: "
                    f"{_field(response, 'message')!r}"
                )
                return AddVenueOutcome(
                    status_code=429,
                    body={
                        "detail": "BestTime monthly venue cap reached",
                        "besttime_status": _field(response, "status"),
                        "besttime_message": _field(response, "message"),
                        "year_month": snap.year_month,
                        "quota": snap.quota,
                    },
                )
            # Recoverable failure: try the geo fallback before we give up.
            return await self._geo_fallback(request, radius_m, response)

        # 5. Success: persist + cache + record + report.
        persisted_venue = await self._persist_new_venue(response, request.place_id)
        # Record the unique BestTime interaction against the monthly ledger so
        # the unique-venue count reflects manual adds, not just refresh.
        self.budget.mark_touched(persisted_venue.venue_id)

        # Fully Google-enrich the venue inline so it carries real metadata (type,
        # hours, reviews, business status, rating) immediately — not just the
        # BestTime-baseline price set in _persist_new_venue. Google-only: no extra
        # BestTime call. Degrade-safe: any failure logs and the add still succeeds.
        await self._enrich_from_google(persisted_venue, request.place_id)

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
                besttime_price_level=match.price_level,
            )
            await self._derive_and_set_price(venue, request.place_id)
            self.venue_dao.upsert_venue(venue)
            # Count toward monthly budget only when truly new.
            self.budget.record_new_venue_from_discovery()
            # The venue_filter call interacted with this venue — record it.
            self.budget.mark_touched(match.venue_id)
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

    async def _persist_new_venue(self, response, place_id: Optional[str]) -> Venue:
        """Build a Venue from a BestTime POST /forecasts response, derive its served
        price tier, and upsert it.

        Price sourcing avoids a doubled paid Google Details call: when an inline
        enrichment service is wired, ``_enrich_from_google`` makes the single Google
        fetch and sets the price, so here we set only a BestTime BASELINE
        (``place_id=None`` — no Google call). Without an enrichment service (the
        legacy path), we keep the original behavior and re-source the Google price
        here from ``place_id``."""
        info = response.venue_info if hasattr(response, "venue_info") else None
        if info is None and isinstance(response, dict):
            info = response.get("venue_info") or {}
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
            besttime_price_level=_get(info, "price_level"),
        )
        # When inline enrichment is wired, it owns the single Google Details fetch;
        # set only the BestTime baseline here (place_id=None -> no Google call) to
        # avoid a doubled paid call. Otherwise (legacy path) re-source Google price
        # here as before.
        price_place_id = None if self.google_places_enrichment_service is not None else place_id
        await self._derive_and_set_price(venue, price_place_id)
        self.venue_dao.upsert_venue(venue)
        return venue

    async def _derive_and_set_price(self, venue: Venue, place_id: Optional[str]) -> None:
        """Set the served price tier on a venue via the shared derivation helper.

        Re-sources Google's `priceLevel` enum + `priceRange` from `place_id` when a
        Google client is configured (PRIMARY), falling back to the venue's BestTime
        price (already on `besttime_price_level`). Dependency-aware and never raises:
        a missing client / place_id / failed fetch falls through to BestTime/NULL.
        Never writes 0.
        """
        google_enum = None
        google_range = None
        if place_id and self.google_places_client is not None:
            try:
                details = await self.google_places_client.get_place_details(place_id)
            except Exception as e:
                logger.warning(
                    f"[AddVenueHandler] Google price fetch failed for {place_id}: "
                    f"{type(e).__name__}: {e}"
                )
                details = None
            if details is not None:
                google_enum = details.price_level
                google_range = details.price_range
        derived = derive_price_signal(
            google_enum, google_range, venue.besttime_price_level
        )
        venue.google_price_level = google_enum
        venue.price_range = google_range
        venue.price_level = derived.price_level
        venue.price_level_source = derived.source

    async def _enrich_from_google(self, venue: Venue, request_place_id: Optional[str]) -> None:
        """Fully Google-enrich a just-persisted venue inline (type/vibe, hours,
        reviews, business status, rating; Google price overwrites the BestTime
        baseline when present, else the baseline is preserved).

        Resolves the Google place_id from the request or via Text Search when the
        request carried none. Never raises: a missing service, no place_id, no
        Google match, or a details failure just logs and returns — the add still
        succeeds. Google-only: makes no BestTime call. enrich_venue persists the
        place_id on the vibe row for future re-enrichment.
        """
        service = self.google_places_enrichment_service
        if service is None:
            return
        try:
            place_id = request_place_id
            if not place_id and self.google_places_client is not None:
                place_id = await self.google_places_client.search_place_id(
                    venue_name=venue.venue_name,
                    venue_address=venue.venue_address,
                    lat=venue.venue_lat,
                    lng=venue.venue_lng,
                )
            if not place_id:
                logger.info(
                    f"[AddVenueHandler] no Google place_id for {venue.venue_id}; "
                    "skipping inline enrichment (Google fields stay empty)"
                )
                return
            # force_refresh=True: the venue was just created, so any stale/empty
            # vibe row must not short-circuit the fetch.
            await service.enrich_venue(
                venue_id=venue.venue_id,
                google_place_id=place_id,
                force_refresh=True,
            )
        except Exception as e:
            logger.warning(
                f"[AddVenueHandler] Google enrichment failed for {venue.venue_id}: "
                f"{type(e).__name__}: {e}"
            )


def _field(source, key):
    """Read an attribute or dict key (BestTime responses come as either)."""
    if source is None:
        return None
    if hasattr(source, key):
        return getattr(source, key)
    if isinstance(source, dict):
        return source.get(key)
    return None


def _is_monthly_cap_rejection(response) -> bool:
    """True when a non-OK /forecasts response is BestTime's monthly unique-venue
    cap rejection (vs a geocoder failure). BestTime returns e.g. "Max amount of
    monthly venues (500) reached. Venue counter will reset ...". Geocoder errors
    ("Could not geocode address") do not match."""
    message = _field(response, "message")
    if not isinstance(message, str):
        return False
    low = message.lower()
    return "monthly venues" in low or "venue counter will reset" in low


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
