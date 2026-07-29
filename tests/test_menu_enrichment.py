"""Unit tests for menu photo enrichment and extraction.

Covers:
- RedisVenueDAO menu methods (set/get/delete/list/count)
- OpenAIMenuClient (extraction parsing, photo classification parsing)
- SerpApiClient (category matching)
- MenuPhotoEnrichmentService (Instagram highlights primary + GMaps extractor fallback)
- MenuExtractionService (orchestration with GPT-4o-mini pre-filter)
- Pydantic models (serialization, defaults)
"""
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch

from app.models.venue import Venue
from app.models.menu import (
    MenuPhoto,
    VenueMenuPhotos,
    MenuItem,
    MenuSection,
    VenueMenuData,
)
from app.api.openai_menu_client import OpenAIMenuClient
from app.api.serpapi_client import SerpApiClient
from app.services.menu_photo_enrichment_service import MenuPhotoEnrichmentService
from app.services.menu_extraction_service import MenuExtractionService
from app.dao import RedisVenueDAO


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_redis_client():
    return Mock()


@pytest.fixture
def venue_dao(mock_redis_client):
    return RedisVenueDAO(mock_redis_client)


@pytest.fixture
def sample_venue():
    return Venue(
        venue_id="v1",
        venue_name="Restaurante Bom Sabor",
        venue_address="R. da Aurora, 100 - Boa Vista, Recife",
        venue_lat=-8.05,
        venue_lng=-34.87,
        venue_type="RESTAURANT",
    )


@pytest.fixture
def sample_menu_photos():
    return VenueMenuPhotos(
        venue_id="v1",
        photos=[
            MenuPhoto(
                photo_id="photo-uuid-1",
                s3_url="https://vibesense.s3.us-east-1.amazonaws.com/places/v1/photos/menu/photo-uuid-1.jpg",
                s3_key="places/v1/photos/menu/photo-uuid-1.jpg",
                source_url="https://lh5.googleusercontent.com/p/menu1.jpg",
                author_name="John Doe",
            ),
            MenuPhoto(
                photo_id="photo-uuid-2",
                s3_url="https://vibesense.s3.us-east-1.amazonaws.com/places/v1/photos/menu/photo-uuid-2.jpg",
                s3_key="places/v1/photos/menu/photo-uuid-2.jpg",
                source_url="https://lh5.googleusercontent.com/p/menu2.jpg",
            ),
        ],
        total_images_on_maps=2,
    )


@pytest.fixture
def sample_menu_data():
    return VenueMenuData(
        venue_id="v1",
        sections=[
            MenuSection(
                name="Hamburgueres",
                items=[
                    MenuItem(
                        name="X-Burguer",
                        description="Pão, hambúrguer, queijo",
                        prices=[{"label": "Individual", "price": 22.00}],
                    ),
                    MenuItem(
                        name="X-Bacon",
                        description="Pão, hambúrguer, queijo, bacon",
                        prices=[{"label": "Individual", "price": 28.00}],
                    ),
                ],
            ),
            MenuSection(
                name="Bebidas",
                items=[
                    MenuItem(
                        name="Coca-Cola",
                        prices=[{"label": "Lata", "price": 6.00}],
                    ),
                ],
            ),
        ],
        currency_detected="BRL",
        source_photo_ids=["photo-uuid-1", "photo-uuid-2"],
        extraction_model="gpt-4o",
        raw_response='{"menu_sections": []}',
    )


@pytest.fixture
def mock_instagram_highlights_client():
    """Primary menu-photo source: a venue's own Instagram highlight reels."""
    client = Mock()
    client.fetch_menu_highlights = AsyncMock(return_value=[
        {"image_url": "https://instagram.com/p/ig1.jpg", "highlight_title": "Cardápio"},
        {"image_url": "https://instagram.com/p/ig2.jpg", "highlight_title": "Cardápio"},
    ])
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_gmaps_extractor_client():
    """Fallback source: the compass Google Maps extractor, via Apify."""
    client = Mock()
    client.fetch_venue_menu_photos = AsyncMock(return_value=[
        {"image_url": "https://lh5.googleusercontent.com/p/gm1.jpg", "category": "Menu"},
        {"image_url": "https://lh5.googleusercontent.com/p/gm2.jpg", "category": "Menu"},
    ])
    client.close = AsyncMock()
    return client

@pytest.fixture
def mock_s3_client():
    client = Mock()
    client.upload_photo_bytes = AsyncMock(
        return_value=("photo-uuid-1", "places/v1/photos/menu/photo-uuid-1.jpg",
                      "https://vibesense.s3.us-east-1.amazonaws.com/places/v1/photos/menu/photo-uuid-1.jpg")
    )
    client.generate_presigned_url = AsyncMock(
        return_value="https://vibesense.s3.us-east-1.amazonaws.com/places/v1/photos/menu/photo-uuid-1.jpg?X-Amz-Signature=abc"
    )
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_openai_client():
    client = Mock()
    client.extract_menu_from_photos = AsyncMock(
        return_value=(
            [MenuSection(name="Hamburgueres", items=[
                MenuItem(name="X-Burguer", prices=[{"label": "Individual", "price": 22.00}])
            ])],
            "BRL",
            '{"menu_sections": []}',
        )
    )
    client.classify_menu_photos = AsyncMock(return_value=[0, 1])
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_venue_dao():
    dao = Mock()
    dao.get_venue.return_value = Venue(
        venue_id="v1",
        venue_name="Restaurante Bom Sabor",
        venue_address="R. da Aurora, 100 - Boa Vista, Recife",
        venue_lat=-8.05,
        venue_lng=-34.87,
        venue_type="RESTAURANT",
    )
    dao.get_venue_menu_photos.return_value = None
    dao.get_venue_menu_data.return_value = None
    dao.set_venue_menu_photos = Mock()
    dao.set_venue_menu_data = Mock()
    dao.list_all_venue_ids.return_value = ["v1", "v2", "v3"]
    dao.list_active_venue_ids.return_value = ["v1", "v2", "v3"]
    # The gate extraction actually reads: active AND eligible. Ineligible venues
    # are excluded so junk never burns OpenAI budget.
    dao.list_servable_venue_ids.return_value = ["v1", "v2", "v3"]
    dao.list_cached_menu_photos_venue_ids.return_value = ["v1"]
    dao.count_venues_with_menu_photos.return_value = 0
    return dao


def _fake_downloader():
    """Stand-in for the service's httpx client.

    `_download_and_upload` holds a REAL AsyncClient, so a suite that does not
    replace it makes live requests to instagram.com and googleusercontent.com —
    slow, flaky, and dependent on somebody else's uptime.
    """
    client = Mock()
    response = Mock()
    response.content = b"\xff\xd8\xff-not-really-a-jpeg"
    response.headers = {"content-type": "image/jpeg"}
    response.raise_for_status = Mock()
    client.get = AsyncMock(return_value=response)
    return client


@pytest.fixture
def photo_enrichment_service(
    mock_instagram_highlights_client, mock_gmaps_extractor_client,
    mock_s3_client, mock_venue_dao,
):
    service = MenuPhotoEnrichmentService(
        instagram_highlights_client=mock_instagram_highlights_client,
        gmaps_extractor_client=mock_gmaps_extractor_client,
        s3_client=mock_s3_client,
        venue_dao=mock_venue_dao,
        enrichment_limit=5,
        photos_per_venue=2,
        menu_categories=["menu", "cardápio", "cardapio", "preços", "valores"],
    )
    service._download_client = _fake_downloader()
    return service


@pytest.fixture
def photo_enrichment_service_gmaps_only(
    mock_gmaps_extractor_client, mock_s3_client, mock_venue_dao,
):
    """No Instagram client at all — the fallback must carry the venue alone."""
    service = MenuPhotoEnrichmentService(
        instagram_highlights_client=None,
        gmaps_extractor_client=mock_gmaps_extractor_client,
        s3_client=mock_s3_client,
        venue_dao=mock_venue_dao,
        enrichment_limit=5,
        photos_per_venue=2,
    )
    service._download_client = _fake_downloader()
    return service



# These exercise the REDIS photo path — the DAO, the presigning and the
# GPT pre-filter. `photo_source` has to be stated: it defaults to `archive`
# now, and an archive-sourced service with no media store finds no photos at
# all, which turned every assertion below into `assert None is not None`.
# The archive path has its own file, tests/test_menu_extraction_from_archive.py.
@pytest.fixture
def extraction_service(mock_openai_client, mock_s3_client, mock_venue_dao):
    return MenuExtractionService(
        openai_client=mock_openai_client,
        s3_client=mock_s3_client,
        venue_dao=mock_venue_dao,
        extraction_model="gpt-4o",
        photo_filter_enabled=True,
        photo_filter_confidence=0.6,
        photo_source="redis",
    )


@pytest.fixture
def extraction_service_no_filter(mock_openai_client, mock_s3_client, mock_venue_dao):
    return MenuExtractionService(
        openai_client=mock_openai_client,
        s3_client=mock_s3_client,
        venue_dao=mock_venue_dao,
        extraction_model="gpt-4o",
        photo_filter_enabled=False,
        photo_filter_confidence=0.6,
        photo_source="redis",
    )


# =============================================================================
# REDIS DAO MENU METHODS
# =============================================================================


class TestRedisVenueDAOMenuMethods:
    """Test Redis DAO methods for menu photos and menu data."""

    def test_set_venue_menu_photos(self, venue_dao, mock_redis_client, sample_menu_photos):
        venue_dao.set_venue_menu_photos(sample_menu_photos)
        mock_redis_client.set.assert_called_once()
        key = mock_redis_client.set.call_args[0][0]
        assert key == "venue_menu_photos_v1:v1"

    def test_get_venue_menu_photos_hit(self, venue_dao, mock_redis_client, sample_menu_photos):
        mock_redis_client.get.return_value = sample_menu_photos.model_dump_json()
        result = venue_dao.get_venue_menu_photos("v1")
        assert result is not None
        assert result.venue_id == "v1"
        assert len(result.photos) == 2
        assert result.photos[0].photo_id == "photo-uuid-1"
        assert result.total_images_on_maps == 2

    def test_get_venue_menu_photos_miss(self, venue_dao, mock_redis_client):
        mock_redis_client.get.return_value = None
        result = venue_dao.get_venue_menu_photos("v1")
        assert result is None

    def test_delete_venue_menu_photos(self, venue_dao, mock_redis_client):
        venue_dao.delete_venue_menu_photos("v1")
        mock_redis_client.del_.assert_called_once_with("venue_menu_photos_v1:v1")

    def test_list_cached_menu_photos_venue_ids(self, venue_dao, mock_redis_client):
        mock_redis_client.keys.return_value = [
            "venue_menu_photos_v1:v1",
            "venue_menu_photos_v1:v2",
        ]
        ids = venue_dao.list_cached_menu_photos_venue_ids()
        assert ids == ["v1", "v2"]

    def test_count_venues_with_menu_photos(self, venue_dao, mock_redis_client):
        mock_redis_client.keys.return_value = ["venue_menu_photos_v1:v1", "venue_menu_photos_v1:v2"]
        count = venue_dao.count_venues_with_menu_photos()
        assert count == 2

    def test_set_venue_menu_data(self, venue_dao, mock_redis_client, sample_menu_data):
        venue_dao.set_venue_menu_data(sample_menu_data)
        mock_redis_client.set.assert_called_once()
        key = mock_redis_client.set.call_args[0][0]
        assert key == "venue_menu_raw_data_v1:v1"

    def test_get_venue_menu_data_hit(self, venue_dao, mock_redis_client, sample_menu_data):
        mock_redis_client.get.return_value = sample_menu_data.model_dump_json()
        result = venue_dao.get_venue_menu_data("v1")
        assert result is not None
        assert result.venue_id == "v1"
        assert len(result.sections) == 2
        assert result.sections[0].name == "Hamburgueres"
        assert len(result.sections[0].items) == 2

    def test_get_venue_menu_data_miss(self, venue_dao, mock_redis_client):
        mock_redis_client.get.return_value = None
        result = venue_dao.get_venue_menu_data("v1")
        assert result is None

    def test_delete_venue_menu_data(self, venue_dao, mock_redis_client):
        venue_dao.delete_venue_menu_data("v1")
        mock_redis_client.del_.assert_called_once_with("venue_menu_raw_data_v1:v1")


# =============================================================================
# OPENAI MENU CLIENT — RESPONSE PARSING
# =============================================================================


class TestOpenAIMenuClientParsing:
    """Test GPT-4o response parsing logic."""

    def test_parse_valid_json(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = json.dumps({
            "menu_sections": [
                {
                    "section_name": "Hamburgueres",
                    "items": [
                        {
                            "name": "X-Burguer",
                            "description": "Pão, carne, queijo",
                            "prices": [{"label": "Individual", "price": 22.00}],
                            "dietary_tags": [],
                        }
                    ],
                }
            ],
            "metadata": {
                "currency_detected": "BRL",
                "last_updated_date": None,
            },
        })

        sections, currency, raw_text = client._parse_response(raw)

        assert len(sections) == 1
        assert sections[0].name == "Hamburgueres"
        assert len(sections[0].items) == 1
        assert sections[0].items[0].name == "X-Burguer"
        assert sections[0].items[0].prices[0]["price"] == 22.00
        assert currency == "BRL"

    def test_parse_json_with_markdown_fences(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = '```json\n{"menu_sections": [{"section_name": "Bebidas", "items": []}], "metadata": {}}\n```'

        sections, currency, raw_text = client._parse_response(raw)

        assert len(sections) == 1
        assert sections[0].name == "Bebidas"

    def test_parse_invalid_json_returns_empty(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)

        sections, currency, raw_text = client._parse_response("this is not json")

        assert sections == []
        assert currency is None

    def test_parse_multiple_sections(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = json.dumps({
            "menu_sections": [
                {"section_name": "Entradas", "items": [
                    {"name": "Coxinha", "prices": [{"label": "", "price": 8.00}]},
                    {"name": "Bolinho", "prices": [{"label": "", "price": 10.00}]},
                ]},
                {"section_name": "Pratos", "items": [
                    {"name": "Feijoada", "prices": [{"label": "", "price": 35.00}]},
                ]},
            ],
            "metadata": {"currency_detected": "BRL"},
        })

        sections, currency, _ = client._parse_response(raw)

        assert len(sections) == 2
        assert sum(len(s.items) for s in sections) == 3

    def test_parse_items_with_dietary_tags_and_modifiers(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = json.dumps({
            "menu_sections": [
                {"section_name": "Vegano", "items": [
                    {
                        "name": "Bowl Vegano",
                        "description": "Arroz, legumes, tofu",
                        "prices": [{"label": "", "price": 28.00}],
                        "dietary_tags": ["VG", "Sem Glúten"],
                        "modifiers": [{"name": "Extra tofu", "price": 5.00}],
                    },
                ]},
            ],
            "metadata": {},
        })

        sections, _, _ = client._parse_response(raw)

        item = sections[0].items[0]
        assert item.dietary_tags == ["VG", "Sem Glúten"]
        assert item.modifiers == [{"name": "Extra tofu", "price": 5.00}]


# =============================================================================
# OPENAI MENU CLIENT — FILTER RESPONSE PARSING
# =============================================================================


class TestOpenAIMenuClientFilterParsing:
    """Test GPT-4o-mini photo classification response parsing."""

    def test_parse_valid_filter_response(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = json.dumps({
            "results": [
                {"index": 0, "is_menu": True, "confidence": 0.95},
                {"index": 1, "is_menu": False, "confidence": 0.10},
                {"index": 2, "is_menu": True, "confidence": 0.80},
            ]
        })

        indices = client._parse_filter_response(raw, count=3, confidence_threshold=0.6)

        assert indices == [0, 2]

    def test_parse_filter_below_threshold(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = json.dumps({
            "results": [
                {"index": 0, "is_menu": True, "confidence": 0.50},
                {"index": 1, "is_menu": True, "confidence": 0.40},
            ]
        })

        indices = client._parse_filter_response(raw, count=2, confidence_threshold=0.6)

        assert indices == []

    def test_parse_filter_invalid_json_returns_all(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)

        indices = client._parse_filter_response("not json", count=3, confidence_threshold=0.6)

        assert indices == [0, 1, 2]

    def test_parse_filter_empty_results_returns_all(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = json.dumps({"results": []})

        indices = client._parse_filter_response(raw, count=2, confidence_threshold=0.6)

        assert indices == [0, 1]

    def test_parse_filter_out_of_bounds_index_ignored(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = json.dumps({
            "results": [
                {"index": 0, "is_menu": True, "confidence": 0.90},
                {"index": 99, "is_menu": True, "confidence": 0.90},
            ]
        })

        indices = client._parse_filter_response(raw, count=2, confidence_threshold=0.6)

        assert indices == [0]

    def test_parse_filter_with_markdown_fences(self):
        client = OpenAIMenuClient.__new__(OpenAIMenuClient)
        raw = '```json\n{"results": [{"index": 0, "is_menu": true, "confidence": 0.9}]}\n```'

        indices = client._parse_filter_response(raw, count=1, confidence_threshold=0.6)

        assert indices == [0]


# =============================================================================
# SERPAPI CLIENT — CATEGORY MATCHING
# =============================================================================


class TestSerpApiClientCategoryMatching:
    """Test SerpApi static method for finding menu categories."""

    def test_find_menu_category_exact_match(self):
        categories = [
            {"id": "CgIYAg", "title": "Ambiente"},
            {"id": "CgIYIQ", "title": "Menu"},
        ]
        result = SerpApiClient.find_menu_category(
            categories, ["menu", "cardápio"]
        )
        assert result == "CgIYIQ"

    def test_find_menu_category_accent_insensitive(self):
        categories = [
            {"id": "CgIYAQ", "title": "Cardápio"},
        ]
        result = SerpApiClient.find_menu_category(
            categories, ["cardapio"]
        )
        assert result == "CgIYAQ"

    def test_find_menu_category_case_insensitive(self):
        categories = [
            {"id": "CgIYIQ", "title": "MENU"},
        ]
        result = SerpApiClient.find_menu_category(
            categories, ["menu"]
        )
        assert result == "CgIYIQ"

    def test_find_menu_category_precos(self):
        categories = [
            {"id": "CgIYBQ", "title": "Preços"},
        ]
        result = SerpApiClient.find_menu_category(
            categories, ["preços", "valores"]
        )
        assert result == "CgIYBQ"

    def test_find_menu_category_no_match(self):
        categories = [
            {"id": "CgIYAg", "title": "Ambiente"},
            {"id": "CgIYAw", "title": "Comida"},
        ]
        result = SerpApiClient.find_menu_category(
            categories, ["menu", "cardápio"]
        )
        assert result is None

    def test_find_menu_category_empty_categories(self):
        result = SerpApiClient.find_menu_category([], ["menu"])
        assert result is None


# =============================================================================
# MENU PHOTO ENRICHMENT  (Instagram highlights primary -> GMaps extractor)
# =============================================================================


@pytest.mark.asyncio
class TestMenuPhotoEnrichmentService:
    """The two-source cascade that finds a venue's menu photos.

    Rewritten in July 2026. The suite that lived here tested a SerpApi-primary
    cascade with a Google Places lookup, which the service stopped having in
    February — it had been failing at construction ever since, so none of it
    protected anything. The intent is preserved against the sources that exist.
    """

    def _with_instagram(self, dao, handle="bomsabor"):
        dao.get_venue_instagram.return_value = Mock(instagram_handle=handle)

    def _without_instagram(self, dao):
        dao.get_venue_instagram.return_value = None

    # ── the cascade ──────────────────────────────────────────────────────────
    async def test_instagram_is_preferred_and_stops_the_cascade(
        self, photo_enrichment_service, mock_venue_dao,
        mock_instagram_highlights_client, mock_gmaps_extractor_client,
    ):
        # A venue's own highlight reel is the venue telling us what its menu is;
        # the extractor is a guess from strangers' uploads. Never pay for the
        # second when the first answered.
        self._with_instagram(mock_venue_dao)

        result = await photo_enrichment_service.enrich_venue("v1")

        assert result.source == "instagram_highlights"
        mock_instagram_highlights_client.fetch_menu_highlights.assert_awaited_once()
        mock_gmaps_extractor_client.fetch_venue_menu_photos.assert_not_awaited()

    async def test_a_venue_with_no_instagram_handle_falls_through_to_gmaps(
        self, photo_enrichment_service, mock_venue_dao,
        mock_instagram_highlights_client, mock_gmaps_extractor_client,
    ):
        self._without_instagram(mock_venue_dao)

        result = await photo_enrichment_service.enrich_venue("v1")

        assert result.source == "gmaps_extractor"
        mock_instagram_highlights_client.fetch_menu_highlights.assert_not_awaited()
        mock_gmaps_extractor_client.fetch_venue_menu_photos.assert_awaited_once()

    async def test_instagram_finding_nothing_falls_through_to_gmaps(
        self, photo_enrichment_service, mock_venue_dao,
        mock_instagram_highlights_client, mock_gmaps_extractor_client,
    ):
        self._with_instagram(mock_venue_dao)
        mock_instagram_highlights_client.fetch_menu_highlights.return_value = []

        result = await photo_enrichment_service.enrich_venue("v1")

        assert result.source == "gmaps_extractor"
        mock_gmaps_extractor_client.fetch_venue_menu_photos.assert_awaited_once()

    async def test_an_instagram_error_falls_through_rather_than_failing(
        self, photo_enrichment_service, mock_venue_dao,
        mock_instagram_highlights_client,
    ):
        self._with_instagram(mock_venue_dao)
        mock_instagram_highlights_client.fetch_menu_highlights.side_effect = (
            RuntimeError("apify actor timed out")
        )

        result = await photo_enrichment_service.enrich_venue("v1")

        assert result.source == "gmaps_extractor"

    async def test_the_service_works_with_no_instagram_client_at_all(
        self, photo_enrichment_service_gmaps_only, mock_venue_dao,
    ):
        result = await photo_enrichment_service_gmaps_only.enrich_venue("v1")
        assert result.source == "gmaps_extractor"

    async def test_both_sources_empty_stores_an_empty_result(
        self, photo_enrichment_service, mock_venue_dao,
        mock_instagram_highlights_client, mock_gmaps_extractor_client,
    ):
        # Stored, not skipped: an empty result is what stops the venue being
        # re-fetched from two paid APIs on every run.
        self._with_instagram(mock_venue_dao)
        mock_instagram_highlights_client.fetch_menu_highlights.return_value = []
        mock_gmaps_extractor_client.fetch_venue_menu_photos.return_value = []

        result = await photo_enrichment_service.enrich_venue("v1")

        assert result.photos == []
        mock_venue_dao.set_venue_menu_photos.assert_called_once()

    async def test_exhausted_apify_credits_stop_the_run_rather_than_degrade(
        self, photo_enrichment_service, mock_venue_dao,
        mock_instagram_highlights_client,
    ):
        from app.api.apify_instagram_client import ApifyCreditExhaustedError

        self._with_instagram(mock_venue_dao)
        mock_instagram_highlights_client.fetch_menu_highlights.side_effect = (
            ApifyCreditExhaustedError("out of credits")
        )

        # Falling back here would spend the OTHER Apify budget on a venue we
        # already know we cannot afford, so this one exception is not swallowed.
        with pytest.raises(ApifyCreditExhaustedError):
            await photo_enrichment_service.enrich_venue("v1")

    # ── caching ──────────────────────────────────────────────────────────────
    async def test_a_cache_hit_calls_neither_source(
        self, photo_enrichment_service, mock_venue_dao, sample_menu_photos,
        mock_instagram_highlights_client, mock_gmaps_extractor_client,
    ):
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos

        result = await photo_enrichment_service.enrich_venue("v1")

        assert result == sample_menu_photos
        mock_instagram_highlights_client.fetch_menu_highlights.assert_not_awaited()
        mock_gmaps_extractor_client.fetch_venue_menu_photos.assert_not_awaited()

    async def test_force_refresh_bypasses_the_cache(
        self, photo_enrichment_service, mock_venue_dao, sample_menu_photos,
        mock_instagram_highlights_client,
    ):
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos
        self._with_instagram(mock_venue_dao)

        await photo_enrichment_service.enrich_venue("v1", force_refresh=True)

        mock_instagram_highlights_client.fetch_menu_highlights.assert_awaited_once()

    async def test_an_unknown_venue_is_reported_not_crashed(
        self, photo_enrichment_service, mock_venue_dao,
    ):
        mock_venue_dao.get_venue.return_value = None
        assert await photo_enrichment_service.enrich_venue("nope") is None

    # ── downloading ──────────────────────────────────────────────────────────
    async def test_every_photo_is_uploaded_and_recorded(
        self, photo_enrichment_service, mock_venue_dao, mock_s3_client,
    ):
        self._without_instagram(mock_venue_dao)

        result = await photo_enrichment_service.enrich_venue("v1")

        assert len(result.photos) == 2
        assert mock_s3_client.upload_photo_bytes.await_count == 2
        assert all(p.s3_key for p in result.photos)

    async def test_the_per_venue_photo_cap_is_respected(
        self, photo_enrichment_service, mock_venue_dao, mock_gmaps_extractor_client,
    ):
        self._without_instagram(mock_venue_dao)
        mock_gmaps_extractor_client.fetch_venue_menu_photos.return_value = [
            {"image_url": f"https://lh5.googleusercontent.com/p/gm{i}.jpg",
             "category": "Menu"}
            for i in range(10)
        ]

        result = await photo_enrichment_service.enrich_venue("v1")

        assert len(result.photos) == 2  # photos_per_venue

    async def test_one_failed_download_does_not_lose_the_others(
        self, photo_enrichment_service, mock_venue_dao, mock_s3_client,
    ):
        self._without_instagram(mock_venue_dao)
        mock_s3_client.upload_photo_bytes.side_effect = [
            RuntimeError("s3 refused"),
            ("photo-2", "places/v1/photos/menu/photo-2.jpg", "https://s3/photo-2.jpg"),
        ]

        result = await photo_enrichment_service.enrich_venue("v1")

        assert len(result.photos) == 1

    async def test_an_instagram_photo_keeps_its_highlight_title_as_a_category(
        self, photo_enrichment_service, mock_venue_dao,
    ):
        self._with_instagram(mock_venue_dao)

        result = await photo_enrichment_service.enrich_venue("v1")

        assert result.available_categories == ["Cardápio"]
        assert result.has_menu_category is True

    # ── the batch job ────────────────────────────────────────────────────────
    async def test_enrich_all_reads_the_servable_venues(
        self, photo_enrichment_service, mock_venue_dao,
    ):
        # The serving view — active AND eligible — so an ineligible venue never
        # burns a paid fetch.
        self._without_instagram(mock_venue_dao)
        # The loop paces itself between venues to be polite to the providers;
        # a unit test should not sit through it.
        with patch("app.services.menu_photo_enrichment_service.INTER_VENUE_DELAY", 0):
            await photo_enrichment_service.enrich_all_venues()
        mock_venue_dao.list_servable_venue_ids.assert_called_once()

    async def test_enrich_all_respects_the_enrichment_limit(
        self, mock_instagram_highlights_client, mock_gmaps_extractor_client,
        mock_s3_client, mock_venue_dao,
    ):
        mock_venue_dao.list_servable_venue_ids.return_value = [f"v{i}" for i in range(10)]
        mock_venue_dao.get_venue_instagram.return_value = None
        service = MenuPhotoEnrichmentService(
            instagram_highlights_client=mock_instagram_highlights_client,
            gmaps_extractor_client=mock_gmaps_extractor_client,
            s3_client=mock_s3_client,
            venue_dao=mock_venue_dao,
            enrichment_limit=3,
            photos_per_venue=1,
        )
        service._download_client = _fake_downloader()

        with patch("app.services.menu_photo_enrichment_service.INTER_VENUE_DELAY", 0):
            await service.enrich_all_venues()

        assert mock_gmaps_extractor_client.fetch_venue_menu_photos.await_count <= 3

    async def test_enrich_all_with_no_venues_is_not_an_error(
        self, photo_enrichment_service, mock_venue_dao,
    ):
        mock_venue_dao.list_servable_venue_ids.return_value = []
        assert await photo_enrichment_service.enrich_all_venues() == 0



class TestMenuExtractionService:
    """Test menu extraction orchestration with GPT-4o-mini pre-filter."""

    async def test_cache_hit_returns_cached(
        self, extraction_service, mock_venue_dao, sample_menu_data
    ):
        mock_venue_dao.get_venue_menu_data.return_value = sample_menu_data

        result = await extraction_service.extract_menu_for_venue("v1")

        assert result == sample_menu_data
        mock_venue_dao.get_venue_menu_photos.assert_not_called()

    async def test_force_refresh_bypasses_cache(
        self, extraction_service, mock_venue_dao, sample_menu_photos
    ):
        mock_venue_dao.get_venue_menu_data.return_value = sample_menu_data
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is not None
        mock_venue_dao.get_venue_menu_photos.assert_called_once()

    async def test_no_menu_photos_returns_none(self, extraction_service, mock_venue_dao):
        mock_venue_dao.get_venue_menu_photos.return_value = None

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is None

    async def test_empty_photos_returns_none(self, extraction_service, mock_venue_dao):
        mock_venue_dao.get_venue_menu_photos.return_value = VenueMenuPhotos(
            venue_id="v1", photos=[]
        )

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is None

    async def test_successful_extraction(
        self, extraction_service, mock_venue_dao, mock_openai_client, mock_s3_client, sample_menu_photos
    ):
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is not None
        assert result.venue_id == "v1"
        assert len(result.sections) == 1
        assert result.currency_detected == "BRL"
        assert result.extraction_model == "gpt-4o"
        assert result.source_photo_ids == ["photo-uuid-1", "photo-uuid-2"]
        mock_venue_dao.set_venue_menu_data.assert_called_once()

    async def test_prefilter_filters_non_menus(
        self, extraction_service, mock_venue_dao, mock_openai_client,
        mock_s3_client, sample_menu_photos,
    ):
        """Pre-filter keeps only menu photos (index 0), removes non-menu (index 1)."""
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos
        mock_openai_client.classify_menu_photos.return_value = [0]  # Only first photo is a menu

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is not None
        # classify_menu_photos called with 2 presigned URLs
        mock_openai_client.classify_menu_photos.assert_called_once()
        # extract_menu_from_photos called with 1 filtered URL
        call_args = mock_openai_client.extract_menu_from_photos.call_args[0][0]
        assert len(call_args) == 1
        # Source photo IDs should only contain the filtered photo
        assert result.source_photo_ids == ["photo-uuid-1"]

    async def test_prefilter_disabled(
        self, extraction_service_no_filter, mock_venue_dao, mock_openai_client,
        mock_s3_client, sample_menu_photos,
    ):
        """When filter is disabled, classify_menu_photos is not called."""
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos

        result = await extraction_service_no_filter.extract_menu_for_venue("v1", force_refresh=True)

        assert result is not None
        mock_openai_client.classify_menu_photos.assert_not_called()
        # All photos sent to extraction
        call_args = mock_openai_client.extract_menu_from_photos.call_args[0][0]
        assert len(call_args) == 2

    async def test_prefilter_all_filtered_stores_empty(
        self, extraction_service, mock_venue_dao, mock_openai_client,
        mock_s3_client, sample_menu_photos,
    ):
        """Pre-filter says no photos are menus → stores empty VenueMenuData."""
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos
        mock_openai_client.classify_menu_photos.return_value = []  # No menus found

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is not None
        assert len(result.sections) == 0
        assert result.extraction_model == "gpt-4o"
        mock_venue_dao.set_venue_menu_data.assert_called_once()
        # extract_menu_from_photos should NOT be called
        mock_openai_client.extract_menu_from_photos.assert_not_called()

    async def test_prefilter_error_graceful_degradation(
        self, extraction_service, mock_venue_dao, mock_openai_client,
        mock_s3_client, sample_menu_photos,
    ):
        """Pre-filter raises exception → proceeds with all photos."""
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos
        mock_openai_client.classify_menu_photos.side_effect = Exception("API timeout")

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is not None
        # All photos sent to extraction despite filter failure
        call_args = mock_openai_client.extract_menu_from_photos.call_args[0][0]
        assert len(call_args) == 2

    async def test_prefilter_skipped_for_single_photo(
        self, extraction_service, mock_venue_dao, mock_openai_client, mock_s3_client,
    ):
        """Pre-filter is skipped when there's only 1 photo (not worth filtering)."""
        single_photo = VenueMenuPhotos(
            venue_id="v1",
            photos=[MenuPhoto(
                photo_id="p1",
                s3_url="https://s3/p1.jpg",
                s3_key="places/v1/photos/menu/p1.jpg",
            )],
        )
        mock_venue_dao.get_venue_menu_photos.return_value = single_photo

        await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        mock_openai_client.classify_menu_photos.assert_not_called()

    async def test_presigned_urls_generated_for_each_photo(
        self, extraction_service, mock_venue_dao, mock_s3_client, sample_menu_photos
    ):
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos

        await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert mock_s3_client.generate_presigned_url.call_count == 2

    async def test_openai_error_returns_none(
        self, extraction_service, mock_venue_dao, mock_openai_client, sample_menu_photos
    ):
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos
        mock_openai_client.extract_menu_from_photos.side_effect = Exception("API error")

        result = await extraction_service.extract_menu_for_venue("v1", force_refresh=True)

        assert result is None

    async def test_extract_all_processes_venues_with_photos(
        self, extraction_service, mock_venue_dao, sample_menu_photos
    ):
        mock_venue_dao.list_cached_menu_photos_venue_ids.return_value = ["v1", "v2"]
        mock_venue_dao.get_venue_menu_data.return_value = None
        mock_venue_dao.get_venue_menu_photos.return_value = sample_menu_photos

        result = await extraction_service.extract_all_venues()

        assert result == 2

    async def test_extract_all_skips_already_extracted(
        self, extraction_service, mock_venue_dao, sample_menu_data
    ):
        mock_venue_dao.list_cached_menu_photos_venue_ids.return_value = ["v1"]
        mock_venue_dao.get_venue_menu_data.return_value = sample_menu_data

        result = await extraction_service.extract_all_venues()

        assert result == 1  # Counted as extracted (from cache)

    async def test_extract_all_no_venues(self, extraction_service, mock_venue_dao):
        mock_venue_dao.list_cached_menu_photos_venue_ids.return_value = []

        result = await extraction_service.extract_all_venues()

        assert result == 0


# =============================================================================
# PYDANTIC MODELS
# =============================================================================


class TestMenuModels:
    """Test Pydantic model serialization/deserialization."""

    def test_venue_menu_photos_has_photos(self):
        with_photos = VenueMenuPhotos(
            venue_id="v1",
            photos=[MenuPhoto(
                photo_id="p1", s3_url="url", s3_key="key"
            )],
        )
        assert with_photos.has_photos() is True

    def test_venue_menu_photos_no_photos(self):
        empty = VenueMenuPhotos(venue_id="v1", photos=[])
        assert empty.has_photos() is False

    def test_menu_photo_with_author(self):
        photo = MenuPhoto(
            photo_id="test-uuid",
            s3_url="https://bucket.s3.us-east-1.amazonaws.com/key.jpg",
            s3_key="places/v1/photos/menu/test-uuid.jpg",
            source_url="https://lh5.googleusercontent.com/p/photo",
            author_name="João Silva",
        )
        json_str = photo.model_dump_json()
        restored = MenuPhoto.model_validate_json(json_str)
        assert restored.photo_id == photo.photo_id
        assert restored.author_name == "João Silva"

    def test_menu_photo_defaults(self):
        photo = MenuPhoto(
            photo_id="test-uuid",
            s3_url="url",
            s3_key="key",
        )
        assert photo.uploaded_at is None
        assert photo.author_name is None
        assert photo.category == ""

    def test_venue_menu_photos_metadata(self):
        result = VenueMenuPhotos(
            venue_id="v1",
            total_images_on_maps=10,
        )
        json_str = result.model_dump_json()
        restored = VenueMenuPhotos.model_validate_json(json_str)
        assert restored.total_images_on_maps == 10

    def test_venue_menu_data_serialization_roundtrip(self, sample_menu_data):
        json_str = sample_menu_data.model_dump_json()
        restored = VenueMenuData.model_validate_json(json_str)
        assert restored.venue_id == "v1"
        assert len(restored.sections) == 2
        assert restored.sections[0].items[0].name == "X-Burguer"
        assert restored.currency_detected == "BRL"

    def test_menu_item_defaults(self):
        item = MenuItem(name="Test")
        assert item.description is None
        assert item.prices == []
        assert item.dietary_tags == []
        assert item.modifiers == []
