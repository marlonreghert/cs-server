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
    SOURCE_SEARCHAPI_PHOTOS,
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
        assert {"language"} <= names
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

    def test_apify_requests_exactly_the_operator_cap(self):
        """Asking for more than the cap means storing more than was asked for.

        The actor returns photos in Google Maps' display order, so the cap
        takes the TOP N — over-fetching a "pool" would just archive extra
        photos the operator never requested.
        """
        import asyncio

        seen = {}

        class _Client:
            async def fetch_venue_photos(self, query, max_photos=20, language="pt-BR"):
                seen["max_photos"] = max_photos
                return {"photos": [], "info": {}}

        source = get_source(SOURCE_APIFY_GMAPS)
        asyncio.run(source.fetch(
            _Client(), {"search_query": "Venue X"},
            {"max_photos_per_venue": 7, "source_config": {}},
        ))
        assert seen["max_photos"] == 7

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


class TestCancellation:
    """Stopping a run must be possible without restarting the container.

    Before this, an operator who realised a 250-venue run was spending on the
    wrong source had no way to stop it from the panel.
    """

    def test_a_cancelled_run_records_what_it_managed(self):
        import asyncio

        from app.services.venue_photo_archive_service import VenuePhotoArchiveService

        class _Dao:
            def list_active_venue_ids(self):
                return [f"v{i}" for i in range(50)]

            def get_venue(self, vid):
                return type("V", (), {"venue_lat": -8.0, "venue_lng": -34.9,
                                      "venue_name": vid, "venue_address": "a"})()

            def get_vibe_attributes(self, vid):
                return type("A", (), {"google_place_id": f"place_{vid}"})()

        class _Store:
            async def list_run_prefixes(self, source):
                return []

            async def list_day_partitions(self, source):
                return []

            async def exists_for_venue(self, prefix, vid):
                return False

        class _SlowGoogle:
            async def get_place_photos(self, *a, **kw):
                await asyncio.sleep(10)   # never completes before the cancel
                return []

        svc = VenuePhotoArchiveService(
            google_places_client=_SlowGoogle(), venue_dao=_Dao(),
            media_store=_Store(), downloader=object(),
        )

        async def go():
            task = asyncio.ensure_future(svc.run({"job_id": "jid", "max_venues": 50}))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(go())
        record = svc.get_run_record("jid")
        assert record is not None, "a cancelled run left no record"
        assert record["aborted"] is True
        assert "duration_seconds" in record


class TestPhotoCategories:
    """Google exposes photo tabs but tags no individual image with one.

    `imageCategories` is a place-level list of which tabs exist. The one tab
    that IS derivable is "By owner" — compare the uploader to the venue name —
    and it is the useful one, since owners upload the official shots.
    """

    def _client(self):
        from app.api.apify_gmaps_extractor_client import ApifyGMapsExtractorClient
        return ApifyGMapsExtractorClient(api_token="t")

    def test_owner_photos_are_labelled_by_owner(self):
        photos = self._client()._archive_photos([{
            "title": "Tasquinha do Tio",
            "images": [
                {"imageUrl": "u1", "authorName": "Tasquinha do Tio"},
                {"imageUrl": "u2", "authorName": "Maria das Gracas"},
            ],
        }], 10)
        assert [p["category"] for p in photos] == ["by_owner", "by_visitor"]

    def test_matching_ignores_case_and_accents(self):
        photos = self._client()._archive_photos([{
            "title": "Café Central",
            "images": [{"imageUrl": "u1", "authorName": "CAFE CENTRAL"}],
        }], 10)
        assert photos[0]["category"] == "by_owner"

    def test_uploaded_at_is_kept(self):
        photos = self._client()._archive_photos([{
            "title": "X",
            "images": [{"imageUrl": "u1", "authorName": "A",
                        "uploadedAt": "2017-05-30T00:00:00.000Z"}],
        }], 10)
        assert photos[0]["uploaded_at"] == "2017-05-30T00:00:00.000Z"

    def test_a_bare_url_list_still_yields_a_category(self):
        photos = self._client()._archive_photos(
            [{"title": "X", "imageUrls": ["u1", "u2"]}], 10
        )
        assert all(p["category"] == "by_visitor" for p in photos)


class TestCategoryFolders:
    def test_a_category_becomes_its_own_folder(self):
        import asyncio

        from app.dao.media_archive_store import MediaArchiveStore

        class _S3:
            def __init__(self): self.objects = {}
            def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None, **k):
                self.objects[Key] = Body
                return {}

        s3 = _S3()
        store = MediaArchiveStore(bucket="b", region="us-east-1", s3_client=s3)
        key = asyncio.run(store.put_image(
            prefix="retrieved/source=s/run_id=r/", venue_id="v1",
            photo_id="p1", data=b"x", content_type="image/jpeg",
            category="by_owner",
        ))
        assert key.endswith("venue_id=v1/media/by_owner/p1.jpg")

    def test_an_uncategorised_photo_still_lands_under_media(self):
        import asyncio

        from app.dao.media_archive_store import MediaArchiveStore

        class _S3:
            def __init__(self): self.objects = {}
            def put_object(self, **k): return {}

        store = MediaArchiveStore(bucket="b", region="us-east-1", s3_client=_S3())
        key = asyncio.run(store.put_image(
            prefix="p/", venue_id="v1", photo_id="p1", data=b"x",
            content_type="image/jpeg", category=None,
        ))
        assert key.endswith("venue_id=v1/media/p1.jpg")

    def test_a_hostile_category_cannot_escape_the_key(self):
        from app.dao.media_archive_store import _safe_category
        assert "/" not in _safe_category("../../raw")
        assert _safe_category("../../raw") == "raw"
        assert _safe_category("") == "uncategorised"


class TestCatalogResolvesFromTheDispatcher:
    """Availability must be answered by whatever performs the fetch.

    The container calls the Google client `google_places_api`; the service
    calls it `google_places_client`. Asking the container reported Google as
    unavailable in the panel while runs against it worked perfectly.
    """

    def test_google_is_available_when_the_service_holds_the_client(self):
        service = SimpleNamespace(
            google_places_client=object(), apify_gmaps_extractor_client=None
        )
        container = SimpleNamespace(
            google_places_api=object(),          # the container's own name
            venue_photo_archive_service=service,
        )
        catalog = {s["id"]: s for s in public_catalog(container)}
        assert catalog[SOURCE_GOOGLE_PHOTOS]["available"] is True
        assert catalog[SOURCE_APIFY_GMAPS]["available"] is False

    def test_it_falls_back_to_the_container_when_no_service_is_wired(self):
        # Every source's client present, so every source should be available —
        # which also fails loudly if a new source is added without wiring.
        container = SimpleNamespace(**{
            src.requires_attr: object() for src in ARCHIVE_SOURCES.values()
        })
        catalog = {s["id"]: s for s in public_catalog(container)}
        assert all(s["available"] for s in catalog.values()), catalog


class TestSearchApiCategorySource:
    """The only source that can name a photo's Google tab.

    Category ids are constants baked into the client: the engine returns no
    categories array (verified against a live response), so they cannot be
    discovered at runtime.
    """

    def _client(self, per_category):
        class _C:
            PHOTO_CATEGORIES = {"menu": "a", "food_drink": "b", "vibe": "c", "latest": "d"}
            calls = []

            async def fetch_venue_photos(self, place_id, categories=None,
                                         max_photos=20, hl="pt-BR"):
                self.calls.append((place_id, tuple(categories or ()), hl))
                photos, found = [], []
                for c in categories or []:
                    n = per_category.get(c, 0)
                    if not n:
                        continue          # a tab this venue does not have
                    found.append(c)
                    photos += [{"url": f"{c}{i}", "category": c,
                                "author_name": None, "photo_name": None}
                               for i in range(n)]
                if not photos:
                    return None
                return {"photos": photos,
                        "info": {"google_place_id": place_id,
                                 "categories_found": found}}
        return _C()

    def _fetch(self, client, categories, max_photos=10):
        import asyncio
        source = get_source(SOURCE_SEARCHAPI_PHOTOS)
        return asyncio.run(source.fetch(
            client, {"google_place_id": "ChIJ_x"},
            {"max_photos_per_venue": max_photos,
             "source_config": {"categories": categories}},
        ))

    def test_photos_are_tagged_with_their_category(self):
        result = self._fetch(self._client({"menu": 2, "vibe": 3}), ["menu", "vibe"])
        by_cat = {}
        for p in result["photos"]:
            by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
        assert by_cat == {"menu": 2, "vibe": 3}

    def test_a_category_the_venue_lacks_is_skipped_not_failed(self):
        # Most places carry only a couple of tabs; absence is the normal case.
        result = self._fetch(self._client({"menu": 2}), ["menu", "vibe", "latest"])
        assert result is not None
        assert {p["category"] for p in result["photos"]} == {"menu"}
        assert result["info"]["categories_found"] == ["menu"]

    def test_a_venue_with_no_place_id_costs_nothing(self):
        import asyncio
        source = get_source(SOURCE_SEARCHAPI_PHOTOS)
        client = self._client({"menu": 5})
        out = asyncio.run(source.fetch(client, {"google_place_id": None},
                                       {"max_photos_per_venue": 5, "source_config": {}}))
        assert out is None
        assert client.calls == [], "a venue without a place id must not be fetched"

    def test_it_is_billed_per_category_per_venue(self):
        source = get_source(SOURCE_SEARCHAPI_PHOTOS)
        one, label = source.estimate_units(250, {"source_config": {"categories": ["menu"]}})
        three, _ = source.estimate_units(
            250, {"source_config": {"categories": ["menu", "vibe", "food_drink"]}}
        )
        assert one == 250 and three == 750
        assert "search" in label
        # 750 searches at $4/1k — the number the live probe implied.
        assert three * source.unit_cost_usd(SETTINGS_SEARCHAPI, {}) == pytest.approx(3.0)

    def test_categories_may_arrive_as_a_comma_separated_string(self):
        # The admin panel can submit either shape.
        result = self._fetch(self._client({"menu": 1, "vibe": 1}), "menu,vibe")
        assert {p["category"] for p in result["photos"]} == {"menu", "vibe"}

    def test_it_declares_the_categories_it_can_fetch(self):
        source = get_source(SOURCE_SEARCHAPI_PHOTOS)
        field = next(f for f in source.config_schema if f.name == "categories")
        assert field.type == "multiselect"
        assert set(field.options) == {"menu", "food_drink", "vibe", "latest", "all"}


SETTINGS_SEARCHAPI = SimpleNamespace(searchapi_cost_per_1k_usd=4.0)


class TestAllCatchAllCategory:
    """`all` is the unfiltered view, and it is NOT a superset.

    Measured on a live place: the four named tabs plus `all` yielded 84
    distinct photos, of which `all` held 20 and `menu` shared none. Each tab
    surfaces its own photos, so `all` is what catches the rest — including the
    place-specific dish tabs whose ids cannot be enumerated.
    """

    def _client(self):
        import app.api.serpapi_client as m
        cls = [v for k, v in vars(m).items()
               if isinstance(v, type) and hasattr(v, "PHOTO_CATEGORIES")][0]

        class _C(cls):
            def __init__(self):
                self.seen_category_ids = []

            async def fetch_photos(self, place_id=None, data_id=None,
                                   category_id=None, hl="pt-BR"):
                self.seen_category_ids.append(category_id)
                tag = category_id or "ALL"
                return {"photos": [{"image": f"{tag}-{i}"} for i in range(2)]}
        return _C()

    def _run(self, client, categories):
        import asyncio
        return asyncio.run(client.fetch_venue_photos(
            "ChIJ_x", categories=categories, max_photos=10
        ))

    def test_all_is_fetched_without_a_category_filter(self):
        client = self._client()
        self._run(client, ["all"])
        assert client.seen_category_ids == [None], client.seen_category_ids

    def test_named_tabs_are_fetched_before_the_catch_all(self):
        # So a photo in both is filed under the specific tab, not `all`.
        client = self._client()
        self._run(client, ["all", "menu"])
        assert client.seen_category_ids[-1] is None
        assert client.seen_category_ids[0] == client.PHOTO_CATEGORIES["menu"]

    def test_a_photo_in_both_is_filed_under_the_specific_tab(self):
        import asyncio

        import app.api.serpapi_client as m
        cls = [v for k, v in vars(m).items()
               if isinstance(v, type) and hasattr(v, "PHOTO_CATEGORIES")][0]

        class _Dup(cls):
            def __init__(self): pass
            async def fetch_photos(self, place_id=None, data_id=None,
                                   category_id=None, hl="pt-BR"):
                # The same photo appears in the menu tab and in `all`.
                return {"photos": [{"image": "shared.jpg"}]}

        out = asyncio.run(_Dup().fetch_venue_photos(
            "p", categories=["all", "menu"], max_photos=10
        ))
        assert len(out["photos"]) == 1, "the duplicate was stored twice"
        assert out["photos"][0]["category"] == "menu"

    def test_all_is_offered_in_the_picker(self):
        source = get_source(SOURCE_SEARCHAPI_PHOTOS)
        field = next(f for f in source.config_schema if f.name == "categories")
        assert "all" in field.options

    def test_the_catch_all_is_billed_like_any_other_category(self):
        source = get_source(SOURCE_SEARCHAPI_PHOTOS)
        units, _ = source.estimate_units(
            100, {"source_config": {"categories": ["menu", "all"]}}
        )
        assert units == 200
