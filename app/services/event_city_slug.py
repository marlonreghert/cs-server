"""Derive an occurrence's `city_slug` from the `admin.geo_fence_city` circles
that already gate serving. plans/260905_events-serving-projection.md, Phase
4 §5. Pure: no I/O.

There is no per-venue city column to read (`venues.address.city` is null
catalog-wide and Phase 2 fills it only opportunistically), so `city_slug` is
derived instead from the SAME circles `serving.eligible_venue` already uses
to decide whether a venue is servable at all. Every servable venue sits
inside SOME circle when the fence is enabled and configured (the normal
production state); this module also degrades sensibly when it does not (the
fence disabled, or a venue's coordinates simply outside every configured
radius): rather than two separate code paths ("the containing circle" vs "no
containing circle"), the NEAREST circle by great-circle distance is picked
in every case — which is also exactly what "pick the nearest centre when
circles overlap" already requires, so one rule covers both requirements.

Reuses `venue_eligibility.haversine_km` — the SAME formula, on the SAME mean
Earth radius, the serving view's own geo term and `geo_excluded()` already
use — rather than a second definition (app/models/event_kind.py's docstring:
two definitions of one thing is exactly the drift that has already cost
this repo real time).
"""
from __future__ import annotations

from typing import Optional

from app.services.venue_eligibility import haversine_km


def nearest_city_slug(lat: Optional[float], lng: Optional[float], cities: list[dict]) -> Optional[str]:
    """`cities` is `admin.geo_fence_city`'s row list (each carrying at least
    `slug`, `lat`, `lng`). Returns the slug of the NEAREST city centre to
    `(lat, lng)`, or None when there is nothing to derive from at all (no
    coordinates, or no configured city). Deterministic on a tie (Python's
    `min` keeps the first minimal element in `cities`' own order)."""
    if lat is None or lng is None or not cities:
        return None
    nearest = min(cities, key=lambda c: haversine_km(lat, lng, c["lat"], c["lng"]))
    return nearest["slug"]


__all__ = ["nearest_city_slug"]
