"""Discovery must not create a second row for a place already stored under a
different venue_id (the duplicate-cards bug: "Adega do Futuro" shown twice).

`_find_stored_duplicate` is the guard: same place = within the tight dedup
radius AND a folded-name match, under a DIFFERENT venue_id. It must NOT merge
two genuinely different venues that happen to sit close together (the Galetus /
Varanda do Rock pair), which would delete a real venue.
"""

from app.models import Venue
from app.services.venues_refresher_service import (
    VenuesRefresherService,
    DISCOVERY_DEDUP_RADIUS_M,
)


class _FakeDao:
    def __init__(self, nearby):
        self._nearby = nearby
        self.calls = []

    def get_nearby_venues(self, lat, lon, radius, include_deprecated=False):
        self.calls.append((lat, lon, radius))
        return list(self._nearby)


def _venue(**kw) -> Venue:
    base = dict(
        venue_id="id",
        venue_name="Bar X",
        venue_address="R. 1",
        venue_lat=-8.05,
        venue_lng=-34.9,
    )
    base.update(kw)
    return Venue(**base)


def _svc(nearby) -> VenuesRefresherService:
    return VenuesRefresherService(venue_dao=_FakeDao(nearby), besttime_api=None)


def test_matching_name_nearby_under_different_id_is_a_duplicate():
    stored = _venue(venue_id="A", venue_name="Adega do Futuro")
    svc = _svc([stored])
    discovered = _venue(venue_id="B", venue_name="Adega do Futuro - Gastronomia")
    assert svc._find_stored_duplicate(discovered) is stored


def test_same_venue_id_is_not_a_duplicate_of_itself():
    stored = _venue(venue_id="A", venue_name="Adega do Futuro")
    svc = _svc([stored])
    same = _venue(venue_id="A", venue_name="Adega do Futuro")
    assert svc._find_stored_duplicate(same) is None


def test_different_name_nearby_is_not_merged():
    # Galetus sits near Varanda do Rock but is a genuinely different venue —
    # merging them would drop a real venue, so this must NOT be a duplicate.
    stored = _venue(venue_id="A", venue_name="Galetus | DuGalego Bar")
    svc = _svc([stored])
    discovered = _venue(venue_id="B", venue_name="Restaurante Varanda do Rock")
    assert svc._find_stored_duplicate(discovered) is None


def test_deprecated_nearby_match_is_ignored():
    stored = _venue(
        venue_id="A", venue_name="Adega do Futuro", lifecycle_status="deprecated"
    )
    svc = _svc([stored])
    discovered = _venue(venue_id="B", venue_name="Adega do Futuro")
    assert svc._find_stored_duplicate(discovered) is None


def test_dedup_uses_the_tight_radius():
    svc = _svc([])
    svc._find_stored_duplicate(_venue(venue_id="B"))
    _lat, _lon, radius = svc.venue_dao.calls[0]
    assert radius == DISCOVERY_DEDUP_RADIUS_M / 1000.0


def test_geo_lookup_failure_falls_back_to_no_dedup():
    class _Boom:
        def get_nearby_venues(self, *a, **k):
            raise RuntimeError("redis down")

    svc = VenuesRefresherService(venue_dao=_Boom(), besttime_api=None)
    # Must swallow and return None (no dedup) rather than abort the refresh.
    assert svc._find_stored_duplicate(_venue(venue_id="B")) is None
