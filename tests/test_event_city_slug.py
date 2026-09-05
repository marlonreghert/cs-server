"""Unit coverage for app.services.event_city_slug (plans/260905_events-
serving-projection.md, Phase 4 pytest list): inside one circle, inside two
overlapping circles (nearest centre wins), and the degenerate cases.
"""
from __future__ import annotations

from app.services.event_city_slug import nearest_city_slug

_RECIFE = {"slug": "recife", "name": "Recife", "lat": -8.0476, "lng": -34.8770, "radius_km": 40.0}
_SALVADOR = {"slug": "salvador", "name": "Salvador", "lat": -12.9714, "lng": -38.5014, "radius_km": 40.0}


def test_inside_one_circle_returns_that_circles_slug():
    # A point close to Recife's own centre.
    assert nearest_city_slug(-8.05, -34.88, [_RECIFE, _SALVADOR]) == "recife"


def test_picks_the_nearest_centre_when_two_circles_overlap():
    # A point roughly midway, nudged nearer to Recife.
    lat, lng = -10.0, -36.5
    from app.services.venue_eligibility import haversine_km
    assert haversine_km(lat, lng, _RECIFE["lat"], _RECIFE["lng"]) < haversine_km(
        lat, lng, _SALVADOR["lat"], _SALVADOR["lng"]
    )
    assert nearest_city_slug(lat, lng, [_RECIFE, _SALVADOR]) == "recife"

    # And the reverse: nudged nearer to Salvador.
    lat2, lng2 = -12.0, -37.5
    assert nearest_city_slug(lat2, lng2, [_RECIFE, _SALVADOR]) == "salvador"


def test_derivation_is_deterministic_across_repeated_calls():
    lat, lng = -10.0, -36.5
    results = {nearest_city_slug(lat, lng, [_RECIFE, _SALVADOR]) for _ in range(5)}
    assert results == {"recife"}


def test_no_coordinates_returns_none():
    assert nearest_city_slug(None, -34.88, [_RECIFE]) is None
    assert nearest_city_slug(-8.05, None, [_RECIFE]) is None
    assert nearest_city_slug(None, None, [_RECIFE]) is None


def test_no_configured_cities_returns_none():
    assert nearest_city_slug(-8.05, -34.88, []) is None
