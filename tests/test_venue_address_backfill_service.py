"""Unit coverage for app.services.venue_address_backfill_service — Phase 4
of plans/260906_address-components-backfill.md. Exercises the real
VenueRepository/InMemoryRdsVenueStore/AdminConfigService stack (fakeredis
for the admin-config mirror), never a bare mock of the service's own
collaborators, so this proves the ACTUAL precedence/cursor/selection SQL
paths, not a re-statement of them.

Covers: batch bounding, cursor persistence and resume (including across a
fresh service instance, simulating a process restart), idempotent re-run,
the geocoding-disabled degraded path (and, separately, the api_error and
no_place_id paths), and the shared `apply_parser_fallback` hook new venues
use.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis
import pytest

from app.dao.venue_repository import VenueRepository
from app.db.geo_redis_client import GeoRedisClient
from app.models.venue import Venue
from app.models.vibe_attributes import VibeAttributes
from app.services.admin_config_service import AdminConfigService
from app.services.venue_address_backfill_service import (
    ADMIN_CONFIG_CURSOR_KEY,
    ADMIN_CONFIG_GEOCODING_ENABLED_KEY,
    VenueAddressBackfillService,
)
from tests.rds_fake import InMemoryRdsVenueStore

_RECIFE_RAW = "R. X, {n} - Recife PE 5000{n}-000 Brazil"


def _venue(vid, raw_text, lat=-8.05, lng=-34.88):
    return Venue(
        venue_id=vid, venue_name=f"Venue {vid}", venue_address=raw_text,
        venue_lat=lat, venue_lng=lng,
    )


def _make(google_places_client=None):
    rds_store = InMemoryRdsVenueStore()
    redis = fakeredis.FakeRedis(decode_responses=True)
    geo_redis = GeoRedisClient(redis)
    repository = VenueRepository(geo_redis, rds_store=rds_store)
    admin_config = AdminConfigService(redis_client=redis, rds_store=rds_store)
    service = VenueAddressBackfillService(
        venue_repository=repository,
        google_places_client=google_places_client,
        admin_config_service=admin_config,
    )
    return service, repository, rds_store, admin_config


# ── apply_parser_fallback: the shared hook new venues use ────────────────
class TestApplyParserFallback:
    def test_resolves_and_writes_with_source_parsed(self):
        service, repository, rds_store, _ = _make()
        repository.upsert_venue(_venue("v1", "R. da Guia, 207 - Boa Viagem Recife - PE 51020-000 Brazil"))
        outcome = service.apply_parser_fallback("v1", "R. da Guia, 207 - Boa Viagem Recife - PE 51020-000 Brazil")
        addr = rds_store.get_address("v1")
        assert outcome == "written"
        assert addr["neighborhood"] == "Boa Viagem"
        assert addr["neighborhood_source"] == "parsed"
        assert addr["city"] == "Recife"
        assert addr["city_source"] == "parsed"

    def test_never_raises_on_unparseable_text(self):
        service, repository, rds_store, _ = _make()
        repository.upsert_venue(_venue("v2", "1 Sky Garden Walk London EC3M 8AF United Kingdom"))
        outcome = service.apply_parser_fallback("v2", "1 Sky Garden Walk London EC3M 8AF United Kingdom")
        assert outcome == "unchanged"
        addr = rds_store.get_address("v2")
        assert addr["city"] is None
        assert addr["neighborhood"] is None

    def test_never_overwrites_an_operator_value(self):
        service, repository, rds_store, _ = _make()
        repository.upsert_venue(_venue("v3", "R. X, 1 - Boa Viagem Recife - PE 51020-000 Brazil"))
        repository.update_venue_address_components("v3", source="operator", neighborhood="Recife Antigo")
        service.apply_parser_fallback("v3", "R. X, 1 - Boa Viagem Recife - PE 51020-000 Brazil")
        assert rds_store.get_address("v3")["neighborhood"] == "Recife Antigo"


# ── process_one: the Google-if-eligible-then-parser per-venue pipeline ────
class TestProcessOne:
    def test_geocoding_disabled_by_default_makes_no_geocode_call(self):
        client = AsyncMock()
        service, repository, rds_store, _ = _make(google_places_client=client)
        repository.upsert_venue(_venue("v4", _RECIFE_RAW.format(n=4)))
        repository.set_vibe_attributes(VibeAttributes(venue_id="v4", google_place_id="ChIJ4"))

        result = asyncio.run(service.process_one("v4"))

        assert result["geocoding_outcome"] == "disabled"
        client.geocode_by_place_id.assert_not_called()
        addr = rds_store.get_address("v4")
        assert addr["city"] == "Recife"
        assert addr["city_source"] == "parsed"

    def test_no_stored_place_id_skips_straight_to_parser(self):
        client = AsyncMock()
        service, repository, rds_store, admin_config = _make(google_places_client=client)
        admin_config.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, True)
        repository.upsert_venue(_venue("v5", _RECIFE_RAW.format(n=5)))
        # No vibe_attributes row at all -> no google_place_id.

        result = asyncio.run(service.process_one("v5"))

        assert result["geocoding_outcome"] == "no_place_id"
        client.geocode_by_place_id.assert_not_called()
        assert rds_store.get_address("v5")["city"] == "Recife"

    def test_geocoding_success_writes_source_google_and_still_runs_parser(self):
        client = AsyncMock()
        client.geocode_by_place_id.return_value = [
            {"longText": "Santo Amaro", "types": ["sublocality_level_1", "political"]},
        ]
        service, repository, rds_store, admin_config = _make(google_places_client=client)
        admin_config.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, True)
        repository.upsert_venue(_venue("v6", _RECIFE_RAW.format(n=6)))
        repository.set_vibe_attributes(VibeAttributes(venue_id="v6", google_place_id="ChIJ6"))

        result = asyncio.run(service.process_one("v6"))

        assert result["geocoding_outcome"] == "success"
        client.geocode_by_place_id.assert_called_once_with("ChIJ6")
        addr = rds_store.get_address("v6")
        assert addr["neighborhood"] == "Santo Amaro"
        assert addr["neighborhood_source"] == "google"
        # The parser still ran for the field Google's response didn't answer:
        assert addr["city"] == "Recife"
        assert addr["city_source"] == "parsed"

    def test_api_error_leaves_nulls_untouched_but_parser_still_fills(self):
        client = AsyncMock()
        client.geocode_by_place_id.side_effect = RuntimeError("quota exceeded")
        service, repository, rds_store, admin_config = _make(google_places_client=client)
        admin_config.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, True)
        repository.upsert_venue(_venue("v7", _RECIFE_RAW.format(n=7)))
        repository.set_vibe_attributes(VibeAttributes(venue_id="v7", google_place_id="ChIJ7"))

        result = asyncio.run(service.process_one("v7"))

        assert result["geocoding_outcome"] == "api_error"
        addr = rds_store.get_address("v7")
        assert addr["neighborhood_source"] is None  # never poisoned with a false answer
        assert addr["city"] == "Recife"
        assert addr["city_source"] == "parsed"  # parser still ran despite the error


# ── backfill_batch: bounding, cursor resume, idempotency ──────────────────
class TestBackfillBatch:
    def test_bounded_batch_processes_exactly_limit(self):
        service, repository, rds_store, admin_config = _make()
        for i in range(1, 6):
            repository.upsert_venue(_venue(f"v{i}", _RECIFE_RAW.format(n=i)))

        result = asyncio.run(service.backfill_batch(limit=2))

        assert result["processed"] == 2
        assert result["done"] is False
        cursor = admin_config.get(ADMIN_CONFIG_CURSOR_KEY)
        assert cursor["last_venue_id"] == "v2"
        assert cursor["processed_total"] == 2

    def test_resumes_and_reaches_every_venue_servable_and_non_servable_alike(self):
        service, repository, rds_store, _ = _make()
        for i in range(1, 6):
            repository.upsert_venue(_venue(f"v{i}", _RECIFE_RAW.format(n=i)))
        # v3 is deprecated (non-servable) — must still be reached, not skipped.
        rds_store.soft_delete_venue("v3", reason="test", source="test")

        results = []
        while True:
            result = asyncio.run(service.backfill_batch(limit=2))
            results.append(result)
            if result["done"]:
                break

        assert sum(r["processed"] for r in results) == 5
        for i in range(1, 6):
            addr = rds_store.get_address(f"v{i}")
            assert addr["city"] == "Recife", f"v{i} was left unresolved: {addr}"

    def test_rerun_over_already_filled_venues_writes_nothing_new(self):
        service, repository, rds_store, _ = _make()
        repository.upsert_venue(_venue("v8", _RECIFE_RAW.format(n=8)))
        first = asyncio.run(service.backfill_batch(limit=10))
        assert first["processed"] == 1
        before = dict(rds_store.get_address("v8"))

        second = asyncio.run(service.backfill_batch(limit=10))

        assert second["processed"] == 0  # already-filled row excluded by the WHERE clause
        assert rds_store.get_address("v8") == before

    def test_cursor_persists_across_a_fresh_service_instance(self):
        """Simulates a process restart: only the durable admin-config +
        RDS survive, not the service object itself."""
        service1, repository, rds_store, admin_config = _make()
        repository.upsert_venue(_venue("va", _RECIFE_RAW.format(n=1)))
        repository.upsert_venue(_venue("vb", _RECIFE_RAW.format(n=2)))
        asyncio.run(service1.backfill_batch(limit=1))

        service2 = VenueAddressBackfillService(repository, None, admin_config)
        result = asyncio.run(service2.backfill_batch(limit=1))

        assert result["processed"] == 1
        assert rds_store.get_address("vb")["city"] == "Recife"

    def test_no_geocoding_calls_while_the_switch_is_off(self):
        client = AsyncMock()
        service, repository, rds_store, _ = _make(google_places_client=client)
        repository.upsert_venue(_venue("vc", _RECIFE_RAW.format(n=3)))
        repository.set_vibe_attributes(VibeAttributes(venue_id="vc", google_place_id="ChIJc"))

        asyncio.run(service.backfill_batch(limit=10))

        client.geocode_by_place_id.assert_not_called()
        assert rds_store.get_address("vc")["city"] == "Recife"

    def test_a_cancelled_batch_leaves_no_partial_cursor_and_resumes_cleanly(self):
        """Proves the batch cannot run away: it yields control
        (`await asyncio.sleep(0)`) once per venue, so the existing generic
        `POST /admin/trigger/address_components_backfill/stop` (an
        asyncio task.cancel()) can interrupt it BETWEEN venues even in the
        default parser-only mode, where no Geocoding call would otherwise
        ever await. A cancellation mid-batch writes no cursor at all (it
        is persisted once, at the very end) — the next run resumes from
        the SAME starting point, safely re-processing (idempotent,
        precedence-guarded) rather than skipping or duplicating work."""
        service, repository, rds_store, admin_config = _make()
        for i in range(1, 6):
            repository.upsert_venue(_venue(f"vcancel{i}", _RECIFE_RAW.format(n=i)))

        processed: list[str] = []
        real_process_one = service.process_one

        async def _counting_process_one(venue_id):
            result = await real_process_one(venue_id)
            processed.append(venue_id)
            if len(processed) == 2:
                asyncio.current_task().cancel()
            return result

        async def _run():
            service.process_one = _counting_process_one
            await service.backfill_batch(limit=5)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run())

        assert len(processed) == 2
        assert admin_config.get(ADMIN_CONFIG_CURSOR_KEY) is None
        for vid in processed:
            assert rds_store.get_address(vid)["city"] == "Recife"

        # A later, uncancelled run starts fresh (no cursor was ever saved)
        # and correctly reaches every venue — the 2 already filled are a
        # safe no-op, the 3 cancellation never reached are newly filled.
        service.process_one = real_process_one
        result = asyncio.run(service.backfill_batch(limit=10))
        assert result["processed"] == 5
        for i in range(1, 6):
            assert rds_store.get_address(f"vcancel{i}")["city"] == "Recife"

    def test_updates_the_remaining_gauge_backing_data(self):
        """Not asserting on the Prometheus registry directly (shared
        global state across the test process); asserts the underlying
        count the gauge is set FROM, which is what the service actually
        computes and reports."""
        service, repository, rds_store, _ = _make()
        repository.upsert_venue(_venue("vd", _RECIFE_RAW.format(n=4)))
        repository.upsert_venue(_venue("ve", "1 Sky Garden Walk London EC3M 8AF United Kingdom"))

        result = asyncio.run(service.backfill_batch(limit=10))

        # "vd" resolves city+postal; "ve" resolves nothing at all.
        assert result["remaining"]["city"] == 1
        assert result["remaining"]["neighborhood"] == 2
