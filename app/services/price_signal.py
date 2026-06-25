"""Shared price-tier derivation — the single source of the "never 0" rule.

All three write paths (Google Places enrichment, BestTime refresh, add-venue) route
their price signals through `derive_price_signal` so the served `price_level` is an
int 1..4 or NULL (never 0) and the chosen source is recorded consistently. See
plans/260625_price-signal-google-source.md.

Derivation order (priceLevel enum PRIMARY; objective range fills enum-less gaps):
  1. Google `priceLevel` enum  -> 1..4  (source "google_enum")
  2. else bucket Google `priceRange` by per-currency thresholds -> 1..4 ("google_range")
  3. else BestTime price        -> 1..4  (source "besttime")
  4. else NULL
`PRICE_LEVEL_FREE` / `PRICE_LEVEL_UNSPECIFIED` carry no enum tier (the map omits
them) so they fall through — a free/unknown venue renders no price pip.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from app.models.venue import PriceRange

# Google Places (New) priceLevel enum -> 1..4 tier. `_FREE` / `_UNSPECIFIED` are
# intentionally absent (they map to NULL, not a tier).
PRICE_LEVEL_ENUM_TO_INT: dict[str, int] = {
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}

SOURCE_GOOGLE_ENUM = "google_enum"
SOURCE_GOOGLE_RANGE = "google_range"
SOURCE_BESTTIME = "besttime"

# Sources that mean "Google produced the served tier" — used by write paths to
# decide when a later BestTime-only run must NOT clobber the tier.
GOOGLE_SOURCES = (SOURCE_GOOGLE_ENUM, SOURCE_GOOGLE_RANGE)


class PriceSignal(NamedTuple):
    """The derived served tier and the rule that produced it."""
    price_level: Optional[int]
    source: Optional[str]


def normalize_legacy_price_level(value: Optional[int]) -> Optional[int]:
    """Map a legacy `price_level` to the 1..4/NULL contract: `0 -> NULL`, else
    unchanged. Encodes the migration-0013 data step for the deterministic BDD
    simulation (the real DDL is integration-validated post-provisioning)."""
    return None if value == 0 else value


def price_level_from_enum(google_enum: Optional[str]) -> Optional[int]:
    """Map Google's priceLevel enum string to a 1..4 tier (None when unmapped)."""
    if not google_enum:
        return None
    return PRICE_LEVEL_ENUM_TO_INT.get(google_enum)


def _normalize_besttime(value: Optional[int]) -> Optional[int]:
    """Coerce a raw BestTime price to the 1..4/NULL contract — `0` and any
    out-of-range value become NULL (never serve 0)."""
    if value is None:
        return None
    if 1 <= value <= 4:
        return value
    return None


def bucket_price_range(
    price_range: Optional[PriceRange],
    thresholds_by_currency: Optional[dict],
) -> Optional[int]:
    """Bucket an objective `priceRange` into a 1..4 tier on its midpoint.

    The Google enum bands overlap in raw money, so we bucket a single robust
    statistic: the range midpoint `(min+max)/2`, or `min`/startPrice when the
    upper bound is unbounded. A missing currency table or unusable range yields
    None (the derivation falls through).
    """
    if price_range is None or not price_range.currency:
        return None
    cuts = (thresholds_by_currency or {}).get(price_range.currency)
    if not cuts:
        return None
    pmin, pmax = price_range.min, price_range.max
    if pmin is None and pmax is None:
        return None
    if pmin is None:
        stat = pmax
    elif pmax is None:
        stat = pmin
    else:
        stat = (pmin + pmax) / 2.0
    for tier, cut in enumerate(cuts, start=1):
        if stat < cut:
            return tier
    return len(cuts) + 1


def derive_price_signal(
    google_enum: Optional[str],
    price_range: Optional[PriceRange],
    besttime_price: Optional[int],
    thresholds: Optional[dict] = None,
) -> PriceSignal:
    """Derive the served (tier, source) following enum > range > besttime > null.

    Never returns 0. `thresholds` defaults to the per-currency table in settings.
    """
    if thresholds is None:
        from app.config import settings

        thresholds = settings.price_range_tier_thresholds

    tier = price_level_from_enum(google_enum)
    if tier is not None:
        return PriceSignal(tier, SOURCE_GOOGLE_ENUM)

    tier = bucket_price_range(price_range, thresholds)
    if tier is not None:
        return PriceSignal(tier, SOURCE_GOOGLE_RANGE)

    tier = _normalize_besttime(besttime_price)
    if tier is not None:
        return PriceSignal(tier, SOURCE_BESTTIME)

    return PriceSignal(None, None)
