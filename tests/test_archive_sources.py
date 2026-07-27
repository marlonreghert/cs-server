"""Unit tests for the archive source registry.

The registry exists so that adding a source is one entry rather than edits
scattered across the service, the router and the admin UI. These tests pin the
two things that would quietly break that: every source declaring a complete
descriptor, and each source's cost model being its own.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.archive_sources import (
    ARCHIVE_SOURCES,
    SOURCE_APIFY_GMAPS,
    SOURCE_GOOGLE_PHOTOS,
    SUPPORTED_SOURCES,
    get_source,
    public_catalog,
)

SETTINGS = SimpleNamespace(
    google_photo_cost_per_1k_usd=7.0,
    apify_place_scraped_cost_usd=0.004,
    apify_place_details_cost_usd=0.002,
)


class TestRegistryIntegrity:
    def test_supported_sources_derives_from_the_registry(self):
        # Otherwise a new source is added in one place and silently rejected in
        # another.
        assert set(SUPPORTED_SOURCES) == set(ARCHIVE_SOURCES)

    @pytest.mark.parametrize("source_id", list(ARCHIVE_SOURCES))
    def test_every_source_declares_a_complete_descriptor(self, source_id):
        s = get_source(source_id)
        assert s.label and s.description
        assert s.requires_attr and s.unavailable_reason
        assert callable(s.fetch)
        assert callable(s.estimate_units) and callable(s.unit_cost_usd)
        assert s.cost_note, "a source must say how it bills"

    def test_both_expected_sources_are_present(self):
        assert SOURCE_GOOGLE_PHOTOS in ARCHIVE_SOURCES
        assert SOURCE_APIFY_GMAPS in ARCHIVE_SOURCES

    def test_an_unknown_source_raises(self):
        with pytest.raises(KeyError):
            get_source("carrier_pigeon")

    def test_config_schema_entries_are_renderable(self):
        for source in ARCHIVE_SOURCES.values():
            for f in source.config_schema:
                pub = f.to_public()
                assert pub["name"] and pub["label"] and pub["type"]
                assert "default" in pub


class TestCatalog:
    def test_a_wired_source_is_available(self):
        container = SimpleNamespace(
            google_places_client=object(), apify_gmaps_extractor_client=object()
        )
        catalog = {s["id"]: s for s in public_catalog(container)}
        assert catalog[SOURCE_GOOGLE_PHOTOS]["available"] is True
        assert catalog[SOURCE_APIFY_GMAPS]["available"] is True

    def test_an_unwired_source_says_why(self):
        container = SimpleNamespace(
            google_places_client=object(), apify_gmaps_extractor_client=None
        )
        catalog = {s["id"]: s for s in public_catalog(container)}
        entry = catalog[SOURCE_APIFY_GMAPS]
        assert entry["available"] is False
        assert "APIFY_API_TOKEN" in entry["unavailable_reason"]

    def test_the_catalog_carries_each_source_config_schema(self):
        container = SimpleNamespace(
            google_places_client=object(), apify_gmaps_extractor_client=object()
        )
        catalog = {s["id"]: s for s in public_catalog(container)}
        names = {f["name"] for f in catalog[SOURCE_APIFY_GMAPS]["config_schema"]}
        assert {"photo_pool", "language"} <= names
        # Google needs no extra fields; an empty schema is valid, not missing.
        assert catalog[SOURCE_GOOGLE_PHOTOS]["config_schema"] == []


class TestCostModels:
    """The whole reason sources own their pricing: the models differ."""

    def test_google_counts_the_place_details_call_per_venue(self):
        source = get_source(SOURCE_GOOGLE_PHOTOS)
        units, label = source.estimate_units(250, {"max_photos_per_venue": 3})
        assert units == 250 * 4, "the Place Details call must be counted"
        assert "request" in label.lower()
        assert source.unit_cost_usd(SETTINGS, {}) == pytest.approx(0.007)

    def test_google_cost_scales_with_photos(self):
        source = get_source(SOURCE_GOOGLE_PHOTOS)
        few, _ = source.estimate_units(100, {"max_photos_per_venue": 1})
        many, _ = source.estimate_units(100, {"max_photos_per_venue": 10})
        assert many > few

    def test_apify_is_priced_per_place_not_per_photo(self):
        source = get_source(SOURCE_APIFY_GMAPS)
        few, label = source.estimate_units(250, {"max_photos_per_venue": 1})
        many, _ = source.estimate_units(250, {"max_photos_per_venue": 10})
        assert few == many == 250, "photo count must not change the bill"
        assert "place" in label.lower()

    def test_apify_unit_cost_is_place_plus_details(self):
        source = get_source(SOURCE_APIFY_GMAPS)
        assert source.unit_cost_usd(SETTINGS, {}) == pytest.approx(0.006)

    def test_apify_is_materially_cheaper_for_a_real_job(self):
        # 2,000 venues x 10 photos — the job that motivated the second source.
        g, a = get_source(SOURCE_GOOGLE_PHOTOS), get_source(SOURCE_APIFY_GMAPS)
        cfg = {"max_photos_per_venue": 10}
        g_cost = g.estimate_units(2000, cfg)[0] * g.unit_cost_usd(SETTINGS, cfg)
        a_cost = a.estimate_units(2000, cfg)[0] * a.unit_cost_usd(SETTINGS, cfg)
        assert g_cost == pytest.approx(154.0)
        assert a_cost == pytest.approx(12.0)
        assert a_cost < g_cost / 10

    def test_missing_settings_fall_back_to_documented_defaults(self):
        # A source must price itself even when nothing is injected.
        for source in ARCHIVE_SOURCES.values():
            assert source.unit_cost_usd(None, {}) > 0
