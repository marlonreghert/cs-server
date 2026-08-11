"""Unit tests for plans/260811_expose-time-known.md.

`time_known` was already computed by `app.services.event_date_resolver.
resolve_event_datetime` and already persisted inside the `raw_extraction`
JSONB blob before this plan — `EventOut(**row)` just silently dropped it,
since Pydantic discards undeclared keys. Three layers are covered here:

  - `TestEventOutSerializesTimeKnown`: the model itself carries the flag for
    both values, and a row with no `time_known` key (every row written
    before migration 0035) serialises False, never True — the plan's
    explicit "default to False, never True" instruction. BDD (tests/bdd/
    enrichment/expose-time-known.feature) proves the same thing through the
    real admin API response; this is the fast, isolated Pydantic-level
    version of the identical claim.
  - `TestResolverTimeKnownAcrossPaths`: the resolver's OWN outputs
    (unmodified — this plan changes no date-resolution code) mapped for the
    plain, recurring, and date-range paths, since those are the paths a
    later edit to event_date_resolver.py is most likely to touch without
    realising `time_known` rides along.
  - `TestExtractionServiceThreadsTimeKnownFromResolver`: the actual wiring
    this plan added (`app/services/event_extraction_service.py`'s
    `prepared_events.append`) — proves `resolved.time_known` reaches the
    persisted row unchanged, for a recurring event and a date-range event,
    end to end through the real service and the DAO fake.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.routers.admin_events_router import EventOut
from app.services.event_date_resolver import resolve_event_datetime
from app.services.event_extraction_service import ArchivedPost, EventExtractionService
from tests.rds_fake import InMemoryRdsVenueStore


def _run(coro):
    return asyncio.run(coro)


def _post_at(year, month, day, hour=12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _base_event_kwargs(**overrides) -> dict:
    kwargs = dict(
        event_id="evt_1", source_handle="h", source_shortcode="sc",
        starts_at=None, status="pending_review",
    )
    kwargs.update(overrides)
    return kwargs


class TestEventOutSerializesTimeKnown:
    """Plan's own test plan, bullet 1 & 2: "Serialisation carries the flag
    for both values" and "A missing flag serialises as False, never True"."""

    def test_a_known_time_serialises_true(self):
        event = EventOut(**_base_event_kwargs(time_known=True))
        assert event.time_known is True

    def test_an_unknown_time_serialises_false(self):
        event = EventOut(**_base_event_kwargs(time_known=False))
        assert event.time_known is False

    def test_a_row_with_no_time_known_key_defaults_false(self):
        """The exact shape of a row persisted before migration 0035 backfilled
        it, or read through a code path that forgot the column — EventOut
        must never invent True from silence."""
        row = _base_event_kwargs()
        assert "time_known" not in row
        event = EventOut(**row)
        assert event.time_known is False

class TestResolverTimeKnownAcrossPaths:
    """Plan's test plan, bullet 3: "The resolver's existing outputs map to
    the flag as expected, including the recurrence and range paths added
    recently." Calls `resolve_event_datetime` directly and only reads its
    existing public output — no date-resolution behavior is touched."""

    def test_plain_known_time(self):
        resolved = resolve_event_datetime(
            date_text="15/08", time_text="22h", post_timestamp=_post_at(2026, 7, 1),
        )
        assert resolved.time_known is True

    def test_plain_date_only_is_unknown(self):
        resolved = resolve_event_datetime(
            date_text="15/08", time_text=None, post_timestamp=_post_at(2026, 7, 1),
        )
        assert resolved.time_known is False

    def test_recurring_with_a_stated_time_is_known(self):
        resolved = resolve_event_datetime(
            date_text="toda quinta", time_text="22h", post_timestamp=_post_at(2026, 7, 1),
        )
        assert resolved.is_recurring is True
        assert resolved.time_known is True

    def test_recurring_with_no_stated_time_is_unknown(self):
        resolved = resolve_event_datetime(
            date_text="toda quinta", time_text=None, post_timestamp=_post_at(2026, 7, 1),
        )
        assert resolved.is_recurring is True
        assert resolved.time_known is False

    def test_date_range_with_a_stated_time_is_known(self):
        # §B: a range keeps only its first day (tests/test_event_date_
        # resolver.py::TestDateRangeYieldsFirstDay) — time_known is
        # independent of that rule and must still reflect time_text alone.
        resolved = resolve_event_datetime(
            date_text="01, 02 e 03 de julho", time_text="21h",
            post_timestamp=_post_at(2026, 6, 1),
        )
        assert resolved.date_range is True
        assert resolved.time_known is True

    def test_date_range_with_no_stated_time_is_unknown(self):
        resolved = resolve_event_datetime(
            date_text="01, 02 e 03 de julho", time_text=None,
            post_timestamp=_post_at(2026, 6, 1),
        )
        assert resolved.date_range is True
        assert resolved.time_known is False


# ── end-to-end wiring: resolver -> prepared_events -> DAO row ────────────────
class _FakePostSource:
    def __init__(self, posts_by_venue: dict[str, list[ArchivedPost]]):
        self.posts_by_venue = posts_by_venue

    async def posts_for_venue(self, venue_id, since):
        return self.posts_by_venue.get(venue_id, [])

    async def image_data_uri(self, key):
        return None


class _FakeOpenAIClient:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        self.calls += 1
        item = self._responses.pop(0)
        flat = json.loads(item)
        return json.dumps({"events": [flat]}), False


def _post(shortcode: str, timestamp: datetime) -> ArchivedPost:
    return ArchivedPost(
        shortcode=shortcode, permalink=f"https://instagram.com/p/{shortcode}",
        caption="Ingressos abertos!", timestamp=timestamp,
        flyer_photo_key=f"{shortcode}.jpg", flyer_confidence=0.9,
        any_photo_key=f"{shortcode}.jpg",
    )


def _dao_with_venue(vid: str, handle: str) -> VenueRepository:
    dao = VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())
    dao.upsert_venue(Venue(venue_id=vid, venue_name="V", venue_lat=-8.05, venue_lng=-34.88))
    dao.set_venue_instagram(VenueInstagram(venue_id=vid, instagram_handle=handle, status="found"))
    return dao


def _extraction_payload(**overrides) -> str:
    payload = {
        "title": "Festa", "description": None, "date_text": "15/08",
        "time_text": "22h", "is_recurring": False, "recurrence_text": None,
        "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestExtractionServiceThreadsTimeKnownFromResolver:
    def test_a_recurring_event_with_a_stated_time_persists_time_known_true(self):
        dao = _dao_with_venue("v1", "v1_handle")
        ts = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        posts = {"v1": [_post("etk_recurring_known", ts)]}
        client = _FakeOpenAIClient([_extraction_payload(
            date_text="toda quinta", time_text="22h",
            is_recurring=True, recurrence_text="toda quinta",
        )])
        service = EventExtractionService(dao, _FakePostSource(posts), client)

        _run(service.run({"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}))

        stored = dao.get_event_by_source("v1_handle", "etk_recurring_known")
        assert stored["is_recurring"] is True
        assert stored["time_known"] is True

    def test_a_date_range_event_with_no_stated_time_persists_time_known_false(self):
        dao = _dao_with_venue("v1", "v1_handle")
        ts = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
        posts = {"v1": [_post("etk_range_unknown", ts)]}
        client = _FakeOpenAIClient([_extraction_payload(
            date_text="01, 02 e 03 de julho", time_text=None,
        )])
        service = EventExtractionService(dao, _FakePostSource(posts), client)

        _run(service.run({"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}))

        stored = dao.get_event_by_source("v1_handle", "etk_range_unknown")
        assert stored["starts_at"].date().isoformat() == "2026-07-01"
        assert stored["time_known"] is False
