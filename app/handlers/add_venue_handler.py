"""Handler for POST /admin/venues/by-address.

Resolves a Google-Places-sourced (venue_name, venue_address, lat, lng)
into a venue in our BestTime account inventory + Redis geo index,
respecting the monthly new-venue quota and the manual-add reserve.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
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
# A create that times out has often still completed (and been charged) on
# BestTime's side; give their inventory a moment to reflect it before the
# free reconcile read.
DEFAULT_TIMEOUT_RECOVERY_GRACE_SECONDS = 2.0
# Geo-fallback containment (substring) matches are gated on the shorter folded
# name being at least this long, so a short generic word ("bar") can never
# containment-link to a longer name ("barcelona bar") — only an exact folded
# match links a shorter-than-this name.
MIN_CONTAINMENT_MATCH_LEN = 5
# A fresh geo-fallback link is reversible for this window (measured from the
# venue's RDS created_at). Undo past the window is refused as not undo-eligible.
GEO_LINK_UNDO_WINDOW_SECONDS = 24 * 60 * 60
# Deprecation reason/source stamped by an undo. The source is the reactivation
# key: an active re-add of a venue deprecated with this source is allowed to
# resurrect it (RdsVenueStore._preserve_deprecation exemption).
GEO_LINK_UNDO_REASON = "geo_link_undone"
GEO_LINK_UNDO_SOURCE = "admin_geo_link_undo"


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
        rds_store=None,
        timeout_recovery_grace_seconds: float = DEFAULT_TIMEOUT_RECOVERY_GRACE_SECONDS,
    ) -> None:
        self.venue_dao = venue_dao
        self.besttime = besttime_api
        self.budget = budget_service
        self.redis = redis_client
        # System-of-record store for the geo-link undo path: reads created_at for
        # the recency guard and soft-deletes on RDS (the projector then drops the
        # venue from serving). Optional so non-RDS wirings still construct.
        self.rds_store = rds_store
        self.timeout_recovery_grace_seconds = timeout_recovery_grace_seconds
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
            # Short-circuit on an ACTIVE hit — and, unchanged from before, on a
            # venue deprecated for any reason OTHER than a geo-link undo (e.g.
            # permanently closed): falling through would spend a BestTime create
            # on a venue _preserve_deprecation keeps hidden anyway. Only the
            # admin_geo_link_undo source falls through to the BestTime path,
            # which reactivates it — so a re-add after an undo is never blocked.
            if persisted is not None and (
                persisted.is_active()
                or persisted.deprecated_source != GEO_LINK_UNDO_SOURCE
            ):
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
        create_started = time.perf_counter()
        try:
            response = await self.besttime.add_venue_to_account(
                request.venue_name, request.venue_address
            )
        except httpx.TimeoutException:
            # The create is synchronous and slow; a timeout often means it
            # still completed (and was charged) on BestTime's side. Reconcile
            # against the free inventory read before failing — never retry
            # the create itself. Slot release happens inside when unconfirmed.
            return await self._recover_timed_out_create(
                request, time.perf_counter() - create_started
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
        return await self._finalize_created_venue(
            request,
            persisted_venue,
            analysis=response.analysis or [],
            result_label="created",
        )

    # ------------------------------------------------------------------

    async def _finalize_created_venue(
        self,
        request: AddVenueByAddressRequest,
        venue: Venue,
        analysis: list,
        result_label: str,
        recovered_from_timeout: bool = False,
    ) -> AddVenueOutcome:
        """Shared success tail for a venue confirmed on BestTime's side —
        whether the create returned inline or was recovered from the account
        inventory after a timeout."""
        # Record the unique BestTime interaction against the monthly ledger so
        # the unique-venue count reflects manual adds, not just refresh.
        self.budget.mark_touched(venue.venue_id)

        # Fully Google-enrich the venue inline so it carries real metadata (type,
        # hours, reviews, business status, rating) immediately — not just the
        # BestTime-baseline price set at persist time. Google-only: no extra
        # BestTime call. Degrade-safe: any failure logs and the add still succeeds.
        await self._enrich_from_google(venue, request.place_id)

        # Best-effort cache of week_raw days if BestTime included them.
        for day in analysis:
            try:
                self.venue_dao.set_week_raw_forecast(venue.venue_id, day)
            except Exception as e:
                logger.warning(
                    f"[AddVenueHandler] week_raw cache failed for "
                    f"{venue.venue_id} day={day.day_int}: {e}"
                )

        # Best-effort inline live forecast fetch.
        await self._inline_live_forecast(venue.venue_id)

        # Cache the deterministic name+address lookup for next time.
        self._save_address_cache(
            request.venue_name, request.venue_address, venue.venue_id
        )

        # Update the gauge for observability.
        VENUE_MONTHLY_NEW_COUNT.set(self.budget.get_snapshot().month_counter)

        ADD_VENUE_BY_ADDRESS_TOTAL.labels(result=result_label).inc()
        body = {
            "status": "created",
            "venue_id": venue.venue_id,
            "venue_name": venue.venue_name,
            "venue_address": venue.venue_address,
            "venue_lat": venue.venue_lat,
            "venue_lng": venue.venue_lng,
            "source": "besttime_new",
        }
        if recovered_from_timeout:
            body["recovered_from_timeout"] = True
        return AddVenueOutcome(status_code=201, body=body)

    async def _recover_timed_out_create(
        self, request: AddVenueByAddressRequest, elapsed_seconds: float
    ) -> AddVenueOutcome:
        """Reconcile a timed-out POST /forecasts against the account inventory.

        BestTime's venue_id is deterministic on name+address, and prod
        incidents show a timed-out create routinely leaves a created-and-
        charged venue behind. Search the inventory (free read) for the
        submitted venue: on a hit, complete the add exactly like a successful
        create; on a miss — or if the reconcile read itself fails — release
        the slot and return an honest timeout error. Never issues a second
        create (each POST /forecasts re-charges)."""
        logger.warning(
            f"[AddVenueHandler] BestTime create timed out after "
            f"{elapsed_seconds:.1f}s for {request.venue_name!r}; reconciling "
            "against the account inventory"
        )
        match = None
        try:
            if self.timeout_recovery_grace_seconds > 0:
                await asyncio.sleep(self.timeout_recovery_grace_seconds)
            match = await self._find_in_account_inventory(
                request.venue_name, request.venue_address
            )
        except Exception as e:
            # A reconcile failure must never mask the original timeout.
            logger.warning(
                f"[AddVenueHandler] timeout reconcile failed for "
                f"{request.venue_name!r}: {type(e).__name__}: {e}"
            )

        if match is None:
            self.budget.release_manual_slot()
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="timeout_unconfirmed").inc()
            return AddVenueOutcome(
                status_code=502,
                body={
                    "detail": (
                        f"BestTime venue create timed out after "
                        f"{elapsed_seconds:.0f}s and the venue was not "
                        "confirmed in the account inventory; nothing was "
                        "persisted. A later retry maps to the same venue id "
                        "on BestTime's side, so retrying cannot create a "
                        "duplicate."
                    ),
                },
            )

        logger.warning(
            f"[AddVenueHandler] recovered timed-out create: {match.venue_id} "
            f"({match.venue_name!r}) found in the account inventory after a "
            f"{elapsed_seconds:.1f}s create timeout; completing the add"
        )
        venue = Venue(
            processed=True,
            forecast=True,
            venue_id=match.venue_id,
            venue_name=match.venue_name or request.venue_name,
            venue_address=match.venue_address or request.venue_address,
            venue_lat=float(
                match.venue_lat if match.venue_lat is not None else request.venue_lat
            ),
            venue_lng=float(
                match.venue_lng if match.venue_lng is not None else request.venue_lng
            ),
        )
        # Same price sourcing as _persist_new_venue: with inline enrichment
        # wired, it owns the single Google fetch (baseline only here).
        price_place_id = (
            None
            if self.google_places_enrichment_service is not None
            else request.place_id
        )
        await self._derive_and_set_price(venue, price_place_id)
        self.venue_dao.upsert_venue(venue)
        return await self._finalize_created_venue(
            request,
            venue,
            analysis=[],
            result_label="created_recovered_timeout",
            recovered_from_timeout=True,
        )

    async def _find_in_account_inventory(
        self, venue_name: str, venue_address: str
    ):
        """Search the account inventory (free, paged read) for the submitted
        venue by accent-folded name; disambiguate multiple name matches by
        address-token overlap."""
        target_name = _fold_text(venue_name)
        candidates = []
        async for row in self.besttime.list_account_inventory():
            name = _fold_text(row.venue_name or "")
            if not name:
                continue
            if name == target_name or target_name in name or name in target_name:
                candidates.append(row)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        address_tokens = set(_fold_text(venue_address).split())
        return max(
            candidates,
            key=lambda row: len(
                address_tokens & set(_fold_text(row.venue_address or "").split())
            ),
        )

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
            # Skip deprecated venues so a re-add of an undone geo link is not
            # short-circuited to the dead row — it falls through to BestTime,
            # which reactivates it.
            if not venue.is_active():
                continue
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
                body={
                    "detail": f"BestTime geo fallback unavailable: {type(e).__name__}",
                    "besttime_status": _field(besttime_response, "status"),
                    "besttime_message": _field(besttime_response, "message"),
                },
            )

        match, match_reason = _find_name_match(
            filter_response.venues or [], request.venue_name, request.venue_address
        )
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
                # was_new drives undoability (only a newly-created row is
                # undoable); match_reason feeds batch automation (auto-keep
                # "exact", queue "containment" for review within the undo window).
                "newly_linked": was_new,
                "match_reason": match_reason,
            },
        )

    async def undo_geo_link(self, venue_id: str) -> AddVenueOutcome:
        """Reverse a fresh geo-fallback link on the system of record.

        Eligibility (checked in order): the venue must exist (else 404); if it is
        already deprecated by a prior undo it is a 200 no-op (idempotent, no
        second counter decrement); if it is deprecated by anything else, or older
        than the 24h window, it is a 409 (not undo-eligible). An eligible venue is
        soft-deleted with the geo-link-undo source (the projector then removes it
        from serving), the discovery slot is returned to the monthly counter, and
        the address-hash cache entry is dropped so a future re-add is not
        short-circuited to the now-deprecated row.
        """
        if self.rds_store is None:
            logger.error("[AddVenueHandler] geo-link undo unavailable: no RDS store")
            return AddVenueOutcome(
                status_code=503,
                body={"detail": "geo-link undo unavailable: system-of-record store not configured"},
            )

        row = self.rds_store.get_venue(venue_id)
        if row is None:
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="geo_link_undo_rejected").inc()
            logger.warning(f"[AddVenueHandler] geo-link undo: venue {venue_id!r} not found")
            return AddVenueOutcome(
                status_code=404,
                body={"detail": f"venue {venue_id} not found"},
            )

        if row.get("lifecycle_status") == "deprecated":
            if row.get("deprecated_source") == GEO_LINK_UNDO_SOURCE:
                # Idempotent: a repeat undo of an already-undone link is a no-op
                # and must NOT decrement the counter a second time.
                logger.info(f"[AddVenueHandler] geo-link undo: {venue_id} already undone")
                return AddVenueOutcome(
                    status_code=200,
                    body={"status": "already_undone", "venue_id": venue_id},
                )
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="geo_link_undo_rejected").inc()
            logger.warning(
                f"[AddVenueHandler] geo-link undo refused: {venue_id} deprecated by "
                f"{row.get('deprecated_source')!r}, not a geo-link undo"
            )
            return AddVenueOutcome(
                status_code=409,
                body={
                    "detail": (
                        f"venue {venue_id} is not undo-eligible "
                        f"(deprecated by {row.get('deprecated_source')})"
                    )
                },
            )

        created_at = _coerce_dt(row.get("created_at"))
        if created_at is None or _age_seconds(created_at) > GEO_LINK_UNDO_WINDOW_SECONDS:
            ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="geo_link_undo_rejected").inc()
            logger.warning(
                f"[AddVenueHandler] geo-link undo refused: {venue_id} outside the 24h "
                f"window (created_at={row.get('created_at')!r})"
            )
            return AddVenueOutcome(
                status_code=409,
                body={
                    "detail": (
                        f"venue {venue_id} is older than 24h and cannot be undone"
                    )
                },
            )

        # Eligible: soft-delete on RDS (projector drops it from serving next
        # cycle), return the discovery slot, drop the address cache. The touch
        # ledger entry stays — the BestTime interaction really happened.
        self.rds_store.soft_delete_venue(
            venue_id, reason=GEO_LINK_UNDO_REASON, source=GEO_LINK_UNDO_SOURCE
        )
        self.budget.release_discovery_slot()
        self._drop_address_cache(row.get("venue_name") or "", row.get("venue_address") or "")
        VENUE_MONTHLY_NEW_COUNT.set(self.budget.get_snapshot().month_counter)
        ADD_VENUE_BY_ADDRESS_TOTAL.labels(result="geo_link_undone").inc()
        logger.info(
            f"[AddVenueHandler] geo-link undo: {venue_id} deprecated, discovery slot returned"
        )
        return AddVenueOutcome(
            status_code=200,
            body={"status": "undone", "venue_id": venue_id},
        )

    def _drop_address_cache(self, venue_name: str, venue_address: str) -> None:
        """Delete the name+address → venue_id lookup entry so a re-add is not
        short-circuited to a just-undone (deprecated) venue. Best-effort."""
        key = VENUE_LOOKUP_BY_ADDRESS_KEY_V1.format(
            hash=_address_hash(venue_name, venue_address)
        )
        try:
            self.redis.delete(key)
        except Exception as e:
            logger.warning(f"[AddVenueHandler] address-cache delete failed: {e}")

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


def _fold_text(text: str) -> str:
    """Accent-fold, casefold, strip punctuation, and collapse whitespace so
    BestTime's normalized inventory strings match operator-submitted ones
    (e.g. "LAÇA, Pina" ~ "Laca Pina")."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )
    cleaned = "".join(ch if ch.isalnum() else " " for ch in without_accents)
    return " ".join(cleaned.casefold().split())


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


def _find_name_match(venues: list, venue_name: str, venue_address: str = ""):
    """Pick the best geo-fallback candidate, returning ``(venue, reason)`` where
    reason is ``"exact"`` or ``"containment"`` — or ``(None, None)`` when nothing
    matches.

    A candidate matches when its folded name equals the submitted folded name
    ("exact"), or when one folded name contains the other AND the shorter folded
    name is at least ``MIN_CONTAINMENT_MATCH_LEN`` characters ("containment") — so
    a short generic word never containment-links to a longer name. Among matches,
    exact ranks above containment; ties break on address-token overlap (folded,
    set-intersected), then BestTime's original order.
    """
    submitted = _fold_text(venue_name)
    if not submitted:
        return None, None
    submitted_tokens = set(_fold_text(venue_address).split())
    best_key = None
    best_venue = None
    best_reason = None
    for index, v in enumerate(venues):
        candidate = _fold_text(getattr(v, "venue_name", None) or "")
        if not candidate:
            continue
        if candidate == submitted:
            reason = "exact"
            rank_primary = 0
        elif submitted in candidate or candidate in submitted:
            if min(len(submitted), len(candidate)) < MIN_CONTAINMENT_MATCH_LEN:
                continue
            reason = "containment"
            rank_primary = 1
        else:
            continue
        overlap = len(
            submitted_tokens
            & set(_fold_text(getattr(v, "venue_address", None) or "").split())
        )
        # Lower is better: exact before containment, then more overlap, then the
        # earlier candidate in BestTime's order.
        key = (rank_primary, -overlap, index)
        if best_key is None or key < best_key:
            best_key, best_venue, best_reason = key, v, reason
    if best_venue is None:
        return None, None
    return best_venue, best_reason


def _coerce_dt(value):
    """Coerce a timestamp to a datetime (Postgres yields datetime; the RDS fake /
    JSON yields an ISO string). Returns None on a missing/unparseable value."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def _age_seconds(dt: datetime) -> float:
    """Seconds since ``dt``, treating a naive datetime as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()
