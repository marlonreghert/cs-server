"""Unit tests for the Ex3 venues.address dual-write + read-source contract."""
from app.dao.venue_row import venue_from_row
from app.models import Venue
from tests.rds_fake import InMemoryRdsVenueStore


def _venue(vid="a", addr="Rua X, 1", lat=-8.05, lng=-34.88) -> Venue:
    return Venue(venue_id=vid, venue_name=f"Bar {vid}", venue_address=addr,
                 venue_lat=lat, venue_lng=lng, venue_type="BAR")


def test_upsert_dual_writes_address_with_null_components():
    s = InMemoryRdsVenueStore()
    s.upsert_venue(_venue("a"))
    addr = s.get_address("a")
    assert addr["raw_text"] == "Rua X, 1"
    assert addr["lat"] == -8.05 and addr["lng"] == -34.88
    assert addr["street"] is None and addr["neighborhood"] is None
    assert addr["city"] is None and addr["postal_code"] is None


def test_reconstruction_sources_address_from_the_table():
    s = InMemoryRdsVenueStore()
    s.upsert_venue(_venue("a"))
    # The address table is the read source: override it and reconstruction follows.
    s.addresses["a"]["raw_text"] = "Nova Rua, 99"
    s.addresses["a"]["lat"] = -8.10
    v = venue_from_row(s.get_venue("a"))
    assert v.venue_address == "Nova Rua, 99"
    assert v.venue_lat == -8.10


def test_enrichment_components_survive_reupsert():
    s = InMemoryRdsVenueStore()
    s.upsert_venue(_venue("a"))
    s.addresses["a"]["city"] = "Recife"  # simulate a later enrichment write
    s.upsert_venue(_venue("a", addr="Rua X, 1 (updated)"))  # plain re-upsert
    assert s.get_address("a")["city"] == "Recife"           # not clobbered
    assert s.get_address("a")["raw_text"] == "Rua X, 1 (updated)"
