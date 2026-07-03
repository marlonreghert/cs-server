"""VenueRepository.set_venue_instagram signature parity with RedisVenueDAO.

Pins the 2026-07-03 prod regression: every Instagram enrichment caller
(GooglePlacesEnrichmentService + InstagramEnrichmentService) passes
cache_ttl_days/not_found_ttl_days — the RedisVenueDAO cache signature. The
container wires VenueRepository (RDS write boundary), which rejected those
kwargs, so every new venue silently skipped Instagram data
("set_venue_instagram() got an unexpected keyword argument 'cache_ttl_days'").
"""
import fakeredis

from app.dao.venue_repository import VenueRepository
from app.db.geo_redis_client import GeoRedisClient
from app.models.instagram import VenueInstagram
from tests.rds_fake import InMemoryRdsVenueStore


def _repo():
    store = InMemoryRdsVenueStore()
    repo = VenueRepository(
        GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)),
        rds_store=store,
    )
    return repo, store


def test_set_venue_instagram_accepts_redis_dao_ttl_kwargs():
    repo, store = _repo()
    ig = VenueInstagram(
        venue_id="ven_ig_parity",
        instagram_handle="cariri_oficial",
        instagram_url="https://instagram.com/cariri_oficial",
        confidence_score=1.0,
        status="found",
    )
    # The exact call shape every enrichment service uses — must not raise.
    repo.set_venue_instagram(ig, cache_ttl_days=30, not_found_ttl_days=7)

    row = store.enrichment["instagram.handle"]["ven_ig_parity"]
    assert row["instagram_handle"] == "cariri_oficial"


def test_set_venue_instagram_still_works_without_kwargs():
    repo, store = _repo()
    ig = VenueInstagram(venue_id="ven_ig_plain", status="not_found", confidence_score=0.0)
    repo.set_venue_instagram(ig)
    assert "ven_ig_plain" in store.enrichment["instagram.handle"]
