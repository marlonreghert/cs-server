"""Unit coverage for PromoterRegistryService: lifecycle CRUD and discovery
idempotency. See plans/260804_instagram-promoter-events.md §B.

BDD (instagram-promoter-events.feature) proves discovery end-to-end against
one handle; this isolates the idempotency guarantee across repeated runs,
which is what makes an unattended discovery job safe to schedule.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_extraction_service import ArchivedPost
from app.services.promoter_registry_service import (
    ALL_STATUSES,
    DISCOVERY_MANUAL,
    DISCOVERY_MENTION,
    InvalidPromoterAccount,
    PromoterRegistryService,
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_REJECTED,
    validate_status,
)
from tests.rds_fake import InMemoryRdsVenueStore


class _FakePostSource:
    def __init__(self):
        self.posts_by_venue: dict[str, list[ArchivedPost]] = {}

    async def posts_for_venue(self, venue_id, since):
        return self.posts_by_venue.get(venue_id, [])


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def dao():
    store = InMemoryRdsVenueStore()
    return VenueRepository(client=None, rds_store=store)


@pytest.fixture
def post_source():
    return _FakePostSource()


def _venue_event(dao, post_source, venue_id, shortcode, caption, handle="hostvenue_ig"):
    dao.insert_event({
        "event_id": f"evt_{shortcode}", "venue_id": venue_id, "source_kind": "venue_post",
        "source_handle": handle, "source_shortcode": shortcode, "status": "confirmed",
    })
    post_source.posts_by_venue.setdefault(venue_id, []).append(ArchivedPost(
        shortcode=shortcode, permalink=f"https://instagram.com/p/{shortcode}",
        caption=caption, timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc),
        flyer_photo_key=None, flyer_confidence=None, any_photo_key=None,
    ))


class TestValidateStatus:
    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_every_documented_status_is_valid(self, status):
        assert validate_status(status) == status

    def test_an_unknown_status_is_rejected(self):
        with pytest.raises(InvalidPromoterAccount):
            validate_status("banned")


class TestLifecycle:
    def test_register_defaults_to_candidate(self, dao):
        service = PromoterRegistryService(dao)
        row = service.register("somehandle")
        assert row["status"] == STATUS_CANDIDATE
        assert row["discovery_source"] == DISCOVERY_MANUAL

    def test_register_normalizes_the_handle(self, dao):
        service = PromoterRegistryService(dao)
        row = service.register("@SomeHandle")
        assert row["handle"] == "somehandle"

    def test_update_to_an_invalid_status_raises_and_does_not_write(self, dao):
        service = PromoterRegistryService(dao)
        service.register("somehandle")
        with pytest.raises(InvalidPromoterAccount):
            service.update("somehandle", {"status": "banned"})
        assert service.get("somehandle")["status"] == STATUS_CANDIDATE

    def test_reject_is_a_soft_transition_not_a_delete(self, dao):
        service = PromoterRegistryService(dao)
        service.register("somehandle")
        row = service.reject("somehandle")
        assert row["status"] == STATUS_REJECTED
        assert service.get("somehandle") is not None  # the row still exists

    def test_reject_unknown_handle_returns_none(self, dao):
        service = PromoterRegistryService(dao)
        assert service.reject("neverregistered") is None

    def test_list_filters_by_status(self, dao):
        service = PromoterRegistryService(dao)
        service.register("a", status=STATUS_CANDIDATE)
        service.register("b", status=STATUS_ACTIVE)
        assert [r["handle"] for r in service.list(status=STATUS_ACTIVE)] == ["b"]


class TestDiscoveryIdempotency:
    def test_a_handle_clearing_the_threshold_is_proposed_as_candidate(self, dao, post_source):
        venue_id = "v1"
        dao.upsert_venue(Venue(venue_id=venue_id, venue_name="Host Venue", venue_lat=-8.05, venue_lng=-34.88))
        dao.set_venue_instagram(VenueInstagram(venue_id=venue_id, instagram_handle="hostvenue_ig", status="found"))
        for i in range(3):
            _venue_event(dao, post_source, venue_id, f"post_{i}", "Bora pro @promoter1! Ingressos na entrada.")

        service = PromoterRegistryService(dao, post_source=post_source)
        result = _run(service.run_discovery({"mention_threshold": 3}))

        assert "promoter1" in result["candidates_proposed"]
        row = dao.get_promoter_account("promoter1")
        assert row["status"] == STATUS_CANDIDATE
        assert row["discovery_source"] == DISCOVERY_MENTION
        assert row["mention_count"] == 3

    def test_below_threshold_is_not_proposed(self, dao, post_source):
        venue_id = "v1"
        dao.upsert_venue(Venue(venue_id=venue_id, venue_name="Host Venue", venue_lat=-8.05, venue_lng=-34.88))
        _venue_event(dao, post_source, venue_id, "post_0", "Bora pro @rareguy! Ingressos.")

        service = PromoterRegistryService(dao, post_source=post_source)
        result = _run(service.run_discovery({"mention_threshold": 3}))

        assert "rareguy" not in result["candidates_proposed"]
        assert dao.get_promoter_account("rareguy") is None

    def test_a_known_venue_handle_is_never_proposed(self, dao, post_source):
        """Rung 1's whole premise: a mention of a KNOWN venue is an identity,
        not a promoter candidate."""
        host_id, other_id = "v1", "v2"
        dao.upsert_venue(Venue(venue_id=host_id, venue_name="Host Venue", venue_lat=-8.05, venue_lng=-34.88))
        dao.upsert_venue(Venue(venue_id=other_id, venue_name="Other Venue", venue_lat=-8.05, venue_lng=-34.88))
        dao.set_venue_instagram(VenueInstagram(venue_id=other_id, instagram_handle="othervenue", status="found"))
        for i in range(3):
            _venue_event(dao, post_source, host_id, f"post_{i}", "Bora pro @othervenue! Ingressos.")

        service = PromoterRegistryService(dao, post_source=post_source)
        result = _run(service.run_discovery({"mention_threshold": 3}))

        assert "othervenue" not in result["candidates_proposed"]
        assert dao.get_promoter_account("othervenue") is None

    def test_repeated_discovery_runs_never_re_propose_an_already_registered_handle(self, dao, post_source):
        venue_id = "v1"
        dao.upsert_venue(Venue(venue_id=venue_id, venue_name="Host Venue", venue_lat=-8.05, venue_lng=-34.88))
        for i in range(3):
            _venue_event(dao, post_source, venue_id, f"post_{i}", "Bora pro @promoter1! Ingressos.")

        service = PromoterRegistryService(dao, post_source=post_source)
        first = _run(service.run_discovery({"mention_threshold": 3}))
        assert "promoter1" in first["candidates_proposed"]

        # An operator activates it between runs — discovery must not clobber
        # that decision on a re-run.
        service.update("promoter1", {"status": STATUS_ACTIVE})

        second = _run(service.run_discovery({"mention_threshold": 3}))
        assert "promoter1" not in second["candidates_proposed"]
        assert dao.get_promoter_account("promoter1")["status"] == STATUS_ACTIVE

    def test_a_caption_repeating_the_same_handle_counts_once_per_post(self, dao, post_source):
        """A single post must not be able to clear the threshold alone by
        repeating the same @-mention several times in one caption."""
        venue_id = "v1"
        dao.upsert_venue(Venue(venue_id=venue_id, venue_name="Host Venue", venue_lat=-8.05, venue_lng=-34.88))
        _venue_event(
            dao, post_source, venue_id, "post_0",
            "@promoter1 @promoter1 @promoter1 vem pra festa! Ingressos.",
        )
        service = PromoterRegistryService(dao, post_source=post_source)
        result = _run(service.run_discovery({"mention_threshold": 3}))
        assert "promoter1" not in result["candidates_proposed"]

    def test_no_post_source_degrades_to_nothing_considered_not_an_error(self, dao):
        service = PromoterRegistryService(dao, post_source=None)
        result = _run(service.run_discovery({}))
        assert result == {"considered_events": 0, "candidates_proposed": []}
