"""Unit tests for the shared price-tier derivation + Google priceRange parsing.

Covers the single never-0 rule (enum > range > besttime > null), per-currency
midpoint bucketing for the enum-less fallback, FREE/UNSPECIFIED -> NULL, unknown
currency fall-through, unbounded endPrice, the legacy 0 -> NULL rule, the Google
`priceRange` parser, and the promoted-column round-trip through the RDS store.
"""
import pytest

from app.api.google_places_client import _parse_price_range
from app.models import PriceRange, Venue
from app.services.price_signal import (
    SOURCE_BESTTIME,
    SOURCE_GOOGLE_ENUM,
    SOURCE_GOOGLE_RANGE,
    bucket_price_range,
    derive_price_signal,
    normalize_legacy_price_level,
)
from tests.rds_fake import InMemoryRdsVenueStore

# BRL: midpoint < 40 -> 1 | < 80 -> 2 | < 160 -> 3 | >= 160 -> 4
BRL = {"BRL": [40.0, 80.0, 160.0]}


class TestDeriveOrderAndSources:
    @pytest.mark.parametrize(
        "enum,tier",
        [
            ("PRICE_LEVEL_INEXPENSIVE", 1),
            ("PRICE_LEVEL_MODERATE", 2),
            ("PRICE_LEVEL_EXPENSIVE", 3),
            ("PRICE_LEVEL_VERY_EXPENSIVE", 4),
        ],
    )
    def test_enum_is_primary(self, enum, tier):
        sig = derive_price_signal(enum, None, None, thresholds=BRL)
        assert sig == (tier, SOURCE_GOOGLE_ENUM)

    def test_enum_wins_over_range(self):
        # Enum says VERY_EXPENSIVE (4); range midpoint would bucket to 3.
        sig = derive_price_signal(
            "PRICE_LEVEL_VERY_EXPENSIVE",
            PriceRange(currency="BRL", min=80, max=200),
            None,
            thresholds=BRL,
        )
        assert sig == (4, SOURCE_GOOGLE_ENUM)

    def test_range_fallback_when_enum_absent(self):
        sig = derive_price_signal(
            None, PriceRange(currency="BRL", min=80, max=200), None, thresholds=BRL
        )
        assert sig == (3, SOURCE_GOOGLE_RANGE)  # midpoint 140 -> tier 3

    def test_besttime_is_last_resort(self):
        sig = derive_price_signal(None, None, 2, thresholds=BRL)
        assert sig == (2, SOURCE_BESTTIME)

    def test_no_signal_is_null(self):
        assert derive_price_signal(None, None, None, thresholds=BRL) == (None, None)


class TestNeverZero:
    def test_free_enum_falls_through_to_null(self):
        assert derive_price_signal("PRICE_LEVEL_FREE", None, None, thresholds=BRL) == (
            None,
            None,
        )

    def test_unspecified_enum_falls_through_to_null(self):
        assert derive_price_signal(
            "PRICE_LEVEL_UNSPECIFIED", None, None, thresholds=BRL
        ) == (None, None)

    def test_besttime_zero_is_null_never_zero(self):
        sig = derive_price_signal(None, None, 0, thresholds=BRL)
        assert sig == (None, None)
        assert sig.price_level != 0

    def test_besttime_out_of_range_is_null(self):
        assert derive_price_signal(None, None, 9, thresholds=BRL) == (None, None)

    def test_free_with_besttime_falls_to_besttime(self):
        # FREE carries no enum tier, so the order continues to BestTime.
        assert derive_price_signal("PRICE_LEVEL_FREE", None, 2, thresholds=BRL) == (
            2,
            SOURCE_BESTTIME,
        )


class TestBucketing:
    @pytest.mark.parametrize(
        "pmin,pmax,tier",
        [
            (20, 40, 1),    # midpoint 30 -> 1
            (40, 100, 2),   # midpoint 70 -> 2
            (80, 200, 3),   # midpoint 140 -> 3
            (160, 260, 4),  # midpoint 210 -> 4
            (160, 160, 4),  # midpoint exactly 160 -> 4 (>= upper cut)
        ],
    )
    def test_midpoint_buckets(self, pmin, pmax, tier):
        assert bucket_price_range(PriceRange(currency="BRL", min=pmin, max=pmax), BRL) == tier

    def test_unbounded_upper_buckets_on_min(self):
        # startPrice 180, no endPrice -> stat = 180 -> tier 4.
        assert bucket_price_range(PriceRange(currency="BRL", min=180, max=None), BRL) == 4

    def test_unbounded_low_min_buckets_on_min(self):
        assert bucket_price_range(PriceRange(currency="BRL", min=50, max=None), BRL) == 2

    def test_unknown_currency_yields_no_tier(self):
        assert bucket_price_range(PriceRange(currency="USD", min=80, max=200), BRL) is None

    def test_missing_currency_yields_no_tier(self):
        assert bucket_price_range(PriceRange(currency=None, min=80, max=200), BRL) is None

    def test_empty_range_yields_no_tier(self):
        assert bucket_price_range(PriceRange(currency="BRL", min=None, max=None), BRL) is None

    def test_unknown_currency_derivation_falls_through_to_besttime(self):
        sig = derive_price_signal(
            None, PriceRange(currency="USD", min=80, max=200), 1, thresholds=BRL
        )
        assert sig == (1, SOURCE_BESTTIME)


class TestLegacyNormalization:
    @pytest.mark.parametrize("value,expected", [(0, None), (1, 1), (2, 2), (3, 3), (4, 4), (None, None)])
    def test_zero_to_null_others_unchanged(self, value, expected):
        assert normalize_legacy_price_level(value) == expected


class TestParsePriceRange:
    def test_full_range(self):
        pr = _parse_price_range(
            {
                "startPrice": {"currencyCode": "BRL", "units": "80"},
                "endPrice": {"currencyCode": "BRL", "units": "200"},
            }
        )
        assert pr == PriceRange(currency="BRL", min=80, max=200)

    def test_missing_end_price_yields_null_max(self):
        pr = _parse_price_range({"startPrice": {"currencyCode": "BRL", "units": "180"}})
        assert pr.currency == "BRL"
        assert pr.min == 180
        assert pr.max is None

    def test_currency_sourced_from_end_when_start_absent(self):
        pr = _parse_price_range({"endPrice": {"currencyCode": "BRL", "units": "200"}})
        assert pr.currency == "BRL"
        assert pr.min is None
        assert pr.max == 200

    def test_missing_units_yields_none_amounts(self):
        pr = _parse_price_range({"startPrice": {"currencyCode": "BRL"}})
        assert pr.currency == "BRL"
        assert pr.min is None and pr.max is None

    def test_none_and_non_dict_return_none(self):
        assert _parse_price_range(None) is None
        assert _parse_price_range("nope") is None
        assert _parse_price_range({}) is None


class TestRdsRoundTrip:
    """The four promoted price columns + a 1..4/NULL tier round-trip through the
    store (upsert -> get_venue reconstruction), preserving the structured range."""

    def test_promoted_price_columns_round_trip(self):
        store = InMemoryRdsVenueStore()
        venue = Venue(
            venue_id="v-rt",
            venue_name="Vasto",
            venue_address="Av. Boa Viagem 1",
            venue_lat=-8.12,
            venue_lng=-34.90,
            price_level=3,
            price_range=PriceRange(currency="BRL", min=80, max=200),
            google_price_level="PRICE_LEVEL_VERY_EXPENSIVE",
            besttime_price_level=2,
            price_level_source=SOURCE_GOOGLE_RANGE,
        )
        store.upsert_venue(venue)
        out = store.get_venue("v-rt")
        from app.dao.venue_row import venue_from_row

        rebuilt = venue_from_row(out)
        assert rebuilt.price_level == 3
        assert rebuilt.price_level_source == SOURCE_GOOGLE_RANGE
        assert rebuilt.google_price_level == "PRICE_LEVEL_VERY_EXPENSIVE"
        assert rebuilt.besttime_price_level == 2
        assert rebuilt.price_range == PriceRange(currency="BRL", min=80, max=200)

    def test_null_tier_and_range_round_trip(self):
        store = InMemoryRdsVenueStore()
        venue = Venue(
            venue_id="v-null",
            venue_name="Mall",
            venue_address="Shopping",
            venue_lat=-8.0,
            venue_lng=-34.0,
            price_level=None,
            price_range=None,
            price_level_source=None,
        )
        store.upsert_venue(venue)
        from app.dao.venue_row import venue_from_row

        rebuilt = venue_from_row(store.get_venue("v-null"))
        assert rebuilt.price_level is None
        assert rebuilt.price_range is None
        assert rebuilt.price_level_source is None
