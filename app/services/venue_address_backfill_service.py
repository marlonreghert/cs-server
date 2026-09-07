"""The address-components backfill: Google-authoritative, parser fallback,
provenance-guarded (plans/260906_address-components-backfill.md, Phase 4;
Google rung swapped to Place Details by
plans/260906_address-components-via-place-details.md).

Fills `venues.address.{street,neighborhood,city,postal_code}` for the
existing catalog and for every new venue going forward. Per venue:
1. If the free-tier switch is on and the venue has a stored
   `google_place_id`: resolve address components via
   `GooglePlacesAPIClient.fetch_address_components` (Place Details, minimal
   `id,addressComponents` mask) and write them with `source="google"`.
2. Unconditionally afterward, run the Phase 1 text parser against the
   venue's `raw_text` and write whatever it resolves with `source="parsed"`
   — safe regardless of step 1's outcome, because the precedence-aware
   write (Phase 2) can never let a parsed value outrank a later google one.

`apply_parser_fallback` is the ONE shared code path both the bulk batch job
(`backfill_batch`, via `process_one`) and `GooglePlacesEnrichmentService`'s
add-time hook use — a newly added venue gets the identical parser treatment
a backfilled one does, with no duplicated logic.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.config import settings
from app.metrics import (
    VENUE_ADDRESS_BACKFILL_REMAINING,
    VENUE_ADDRESS_COMPONENTS_TOTAL,
    VENUE_ADDRESS_GAUGES_REFRESHED_TIMESTAMP_SECONDS,
    VENUE_ADDRESS_NEIGHBORHOOD_EQUALS_CITY,
    VENUE_ADDRESS_PARSED_TOTAL,
    VENUE_ADDRESS_SOURCE_ROWS,
    VENUE_GEOCODING_REQUESTS_TOTAL,
)
from app.services.venue_address_components import write_mapped_components
from app.services.venue_address_parser import parse_address
from app.services.venue_city_vocabulary import load_city_vocabulary

logger = logging.getLogger(__name__)

# The free-tier kill switch (Phase 3): default OFF. Checked before every
# call to fetch_address_components (Place Details) — when false, the
# backfill runs the parser only and makes zero Place Details address
# lookups. Key name unchanged from the original Geocoding-era design —
# renaming risks a silent default-off on any code path that still reads the
# old key name.
ADMIN_CONFIG_GEOCODING_ENABLED_KEY = "address_backfill_geocoding_enabled"

# Small JSON progress state: {"last_venue_id", "processed_total",
# "written_total"}. System-written, no validator — an operator can still
# reset it by hand (e.g. {"last_venue_id": null} to re-run the whole sweep
# after enabling Geocoding or correcting the vocabulary) via the existing
# generic PUT /admin/config/{key}.
ADMIN_CONFIG_CURSOR_KEY = "address_backfill_cursor"


class VenueAddressBackfillService:
    def __init__(self, venue_repository, google_places_client, admin_config_service):
        self.venue_repository = venue_repository
        self.google_places_client = google_places_client
        self.admin_config_service = admin_config_service

    # ── the Place Details address-components attempt ─────────────────────
    async def _maybe_fetch_google_address(self, venue_id: str) -> str:
        """Returns the `VENUE_GEOCODING_REQUESTS_TOTAL` outcome label:
        `success` (the call itself succeeded, including a genuine
        zero-result), `no_place_id` (no stored place_id, or Google Places
        isn't configured at all), `api_error` (transport/quota/API
        failure — the row's nulls are left untouched, retried next run),
        or `disabled` (the free-tier switch is off)."""
        enabled = bool(
            self.admin_config_service.get(ADMIN_CONFIG_GEOCODING_ENABLED_KEY)
        ) if self.admin_config_service is not None else False
        if not enabled:
            VENUE_GEOCODING_REQUESTS_TOTAL.labels(outcome="disabled").inc()
            return "disabled"

        if self.google_places_client is None:
            VENUE_GEOCODING_REQUESTS_TOTAL.labels(outcome="no_place_id").inc()
            return "no_place_id"

        vibe_attrs = self.venue_repository.get_vibe_attributes(venue_id)
        place_id = getattr(vibe_attrs, "google_place_id", None) if vibe_attrs is not None else None
        if not place_id:
            VENUE_GEOCODING_REQUESTS_TOTAL.labels(outcome="no_place_id").inc()
            return "no_place_id"

        try:
            components = await self.google_places_client.fetch_address_components(place_id)
        except Exception as e:
            logger.warning(
                f"[VenueAddressBackfill] address lookup failed for {venue_id}: "
                f"{type(e).__name__}: {e}"
            )
            VENUE_GEOCODING_REQUESTS_TOTAL.labels(outcome="api_error").inc()
            return "api_error"

        outcome = write_mapped_components(
            self.venue_repository, venue_id, components, source="google"
        )
        VENUE_ADDRESS_COMPONENTS_TOTAL.labels(outcome=outcome).inc()
        VENUE_GEOCODING_REQUESTS_TOTAL.labels(outcome="success").inc()
        return "success"

    # ── Phase 1: the parser fallback — shared by the bulk job AND enrich_venue ──
    def apply_parser_fallback(self, venue_id: str, raw_text: str) -> str:
        """Runs the Phase 1 parser against `raw_text` and writes any
        resolved field with `source="parsed"` (precedence-guarded — never
        overwrites an equal-or-higher-precedence value already stored).
        Fetches the venue's own (lat, lng) itself for the ambiguity guard,
        since a caller only ever has `raw_text`. Never raises: a malformed
        or unrecognizable `raw_text` yields every field None, a normal,
        logged outcome. Returns the outcome label ("written"/"unchanged")
        for the caller's own bookkeeping.
        """
        venue = self.venue_repository.get_venue(venue_id)
        lat = venue.venue_lat if venue is not None else None
        lng = venue.venue_lng if venue is not None else None
        vocabulary = load_city_vocabulary(self.admin_config_service)
        parsed = parse_address(
            raw_text or "",
            vocabulary,
            venue_lat=lat,
            venue_lng=lng,
            ambiguous_radius_km=settings.address_backfill_ambiguous_radius_km,
        )
        self.venue_repository.update_venue_address_components(
            venue_id, source="parsed", **parsed
        )
        outcome = "written" if any(parsed.values()) else "unchanged"
        VENUE_ADDRESS_PARSED_TOTAL.labels(outcome=outcome).inc()
        return outcome

    # ── per-venue orchestration: Google (if eligible), then parser ────────
    async def process_one(self, venue_id: str) -> dict:
        """The full per-venue backfill pipeline: an optional Google Place
        Details address-lookup attempt followed UNCONDITIONALLY by the
        parser fallback for whatever remains unresolved — safe regardless
        of the Google step's outcome, since a parsed value can never
        outrank a later google one. Returns `{"geocoding_outcome",
        "parsed_outcome"}` (key name kept — several call sites read it, and
        renaming it buys no behavior change).
        """
        venue = self.venue_repository.get_venue(venue_id)
        if venue is None:
            logger.warning(f"[VenueAddressBackfill] venue not found: {venue_id}")
            return {"geocoding_outcome": "no_place_id", "parsed_outcome": "unchanged"}
        geocoding_outcome = await self._maybe_fetch_google_address(venue_id)
        parsed_outcome = self.apply_parser_fallback(venue_id, venue.venue_address)
        return {"geocoding_outcome": geocoding_outcome, "parsed_outcome": parsed_outcome}

    # ── the bounded, resumable, idempotent batch job ──────────────────────
    async def backfill_batch(
        self, limit: Optional[int] = None, mode: str = "fill"
    ) -> dict:
        """Reads the persisted cursor, selects up to `limit` `venues.address`
        rows with `venue_id > cursor` from the population `mode` names —
        `fill` (default: at least one structured column is still null, the
        shipped behaviour) or `upgrade` (also rows whose column is filled but
        sourced `parsed`, so Google can correct a parser-written
        venue-complex name; plans/260906_events-venue-bairro-and-ticket-
        url.md §1) — ordered by `venue_id` alone (NOT servable-first —
        `venue_id` carries no correlation to servability, so a two-tier
        (servable, venue_id) ordering resumed by a single `venue_id` cursor
        could permanently orphan a slice of the non-servable backlog once
        the cursor advances past it; a plain venue_id ordering cannot have
        this bug, and the whole catalog completes in a small, fixed number
        of batches regardless). Persists the cursor (last processed
        venue_id + running totals) ONCE per batch, not per venue — every
        write this service makes is idempotent under the Phase 2 precedence
        rule, so a crash mid-batch simply re-processes up to `limit`
        already-safe rows on the next run. Refreshes the
        `VENUE_ADDRESS_BACKFILL_REMAINING{field}` gauge at the end.

        `mode` is forwarded to the REPOSITORY, which is the hop this
        service actually calls; an unknown mode raises `ValueError` at the
        DAO boundary rather than silently sweeping the wrong population.
        `upgrade` costs no more than `fill` per venue: every paid call still
        sits behind `address_backfill_geocoding_enabled` (default OFF) and
        every run is still one bounded batch.

        Bounded by `limit` (default `settings.address_backfill_batch_size`).
        Yields control once per venue (`await asyncio.sleep(0)`) so the
        existing generic `POST /admin/trigger/address_components_backfill/
        stop` can cancel an in-flight run between venues even when
        Geocoding is disabled and the loop would otherwise never await.
        A single venue's processing failure is logged and does not abort
        the batch — one bad row must never cost the rest of it.
        """
        effective_limit = limit if limit is not None else settings.address_backfill_batch_size
        cursor_state = {}
        if self.admin_config_service is not None:
            cursor_state = self.admin_config_service.get(ADMIN_CONFIG_CURSOR_KEY) or {}
        after_id = cursor_state.get("last_venue_id")

        rows = self.venue_repository.list_address_backfill_candidates(
            after_id, effective_limit, mode=mode
        )

        written = 0
        for row in rows:
            venue_id = row["venue_id"]
            try:
                result = await self.process_one(venue_id)
                if result["parsed_outcome"] == "written" or result["geocoding_outcome"] == "success":
                    written += 1
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    f"[VenueAddressBackfill] processing failed for {venue_id}, "
                    f"leaving it for the next run: {type(e).__name__}: {e}"
                )
            await asyncio.sleep(0)

        new_last_id = rows[-1]["venue_id"] if rows else after_id
        new_cursor = {
            "last_venue_id": new_last_id,
            "processed_total": cursor_state.get("processed_total", 0) + len(rows),
            "written_total": cursor_state.get("written_total", 0) + written,
        }
        if self.admin_config_service is not None:
            self.admin_config_service.set(ADMIN_CONFIG_CURSOR_KEY, new_cursor)

        remaining = self.venue_repository.count_address_backfill_remaining()
        for field, count in remaining.items():
            VENUE_ADDRESS_BACKFILL_REMAINING.labels(field=field).set(count)
        self._refresh_address_quality_gauges()

        logger.info(
            f"[VenueAddressBackfill] batch complete: mode={mode} "
            f"processed={len(rows)} written={written} cursor={new_last_id} "
            f"remaining={remaining}"
        )
        return {
            "processed": len(rows),
            "written": written,
            "cursor": new_last_id,
            "mode": mode,
            "done": len(rows) < effective_limit,
            "remaining": remaining,
        }

    def _refresh_address_quality_gauges(self) -> None:
        """Recompute the two address-QUALITY gauges and stamp when it
        happened. Best-effort: a metrics read must never fail a batch whose
        writes already landed.

        These gauges are BATCH-SCOPED, not continuous — this job has no
        scheduler entry, it runs only when an operator POSTs
        `/admin/trigger/address_components_backfill/run`. A Prometheus gauge
        carries no staleness marker of its own, so
        `VENUE_ADDRESS_GAUGES_REFRESHED_TIMESTAMP_SECONDS` is set in the same
        breath: an operator can then alert on
        `time() - venue_address_gauges_refreshed_timestamp_seconds` and a
        process where no batch has ever run reads 0 instead of looking like
        a measurement. Refreshing them from the 2-minute projection cycle
        was rejected deliberately — that would bolt an address-quality
        `GROUP BY` onto the serving projector's all-or-nothing preparation
        read, for no gain during the sweep, which is when these are used."""
        try:
            by_source = self.venue_repository.count_address_source_rows()
            equals_city = self.venue_repository.count_address_neighborhood_equals_city()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                f"[VenueAddressBackfill] address-quality gauge refresh failed; "
                f"leaving the previous reading and its timestamp in place: {e}"
            )
            return
        for field, sources in by_source.items():
            for source, count in sources.items():
                VENUE_ADDRESS_SOURCE_ROWS.labels(field=field, source=source).set(count)
        VENUE_ADDRESS_NEIGHBORHOOD_EQUALS_CITY.set(equals_city)
        VENUE_ADDRESS_GAUGES_REFRESHED_TIMESTAMP_SECONDS.set(time.time())


__all__ = [
    "ADMIN_CONFIG_CURSOR_KEY",
    "ADMIN_CONFIG_GEOCODING_ENABLED_KEY",
    "VenueAddressBackfillService",
]
