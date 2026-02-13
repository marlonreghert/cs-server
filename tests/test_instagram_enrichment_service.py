"""Unit tests for InstagramEnrichmentService."""
import pytest
from unittest.mock import Mock, AsyncMock, patch

from app.models.venue import Venue
from app.models.instagram import (
    InstagramProfile,
    InstagramValidationResult,
    VenueInstagram,
)
from app.services.instagram_enrichment_service import InstagramEnrichmentService
from app.api.apify_instagram_client import ApifyCreditExhaustedError


@pytest.fixture
def mock_apify_client():
    client = Mock()
    # search_users now returns InstagramProfile objects directly
    client.search_users = AsyncMock(return_value=[])
    client.get_profile = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_venue_dao():
    dao = Mock()
    dao.get_venue_instagram.return_value = None
    dao.get_venue.return_value = Venue(
        venue_id="v1",
        venue_name="Bar Conchittas",
        venue_address="R. do Hospício, 51 - Boa Vista, Recife",
        venue_lat=-8.05,
        venue_lng=-34.87,
        venue_type="BAR",
    )
    dao.set_venue_instagram = Mock()
    dao.list_all_venue_ids.return_value = ["v1", "v2"]
    dao.count_venues_with_instagram.return_value = 0
    return dao


@pytest.fixture
def mock_validator():
    v = Mock()
    v.auto_accept_threshold = 0.75
    v.low_confidence_threshold = 0.50
    v.validate.return_value = InstagramValidationResult(
        username="barconchittas",
        confidence_score=0.85,
        signals={"name_similarity": 1.0},
        is_match=True,
    )
    return v


@pytest.fixture
def service(mock_apify_client, mock_venue_dao, mock_validator):
    return InstagramEnrichmentService(
        apify_client=mock_apify_client,
        venue_dao=mock_venue_dao,
        validator=mock_validator,
        search_candidates=3,
        cache_ttl_days=30,
        not_found_ttl_days=7,
    )


class TestDiscoverInstagramForVenue:
    """Test single-venue discovery."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self, service, mock_venue_dao):
        """Cached result is returned without API calls."""
        cached = VenueInstagram(
            venue_id="v1",
            instagram_handle="barconchittas",
            instagram_url="https://instagram.com/barconchittas",
            status="found",
            confidence_score=0.85,
        )
        mock_venue_dao.get_venue_instagram.return_value = cached

        result = await service.discover_instagram_for_venue("v1")

        assert result == cached
        service.apify_client.search_users.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self, service, mock_apify_client):
        """force_refresh=True skips cache check."""
        mock_apify_client.search_users.return_value = []

        result = await service.discover_instagram_for_venue("v1", force_refresh=True)

        assert result.status == "not_found"
        mock_apify_client.search_users.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_candidates_returns_not_found(self, service, mock_apify_client, mock_venue_dao):
        """No search results -> not_found, caches the result."""
        mock_apify_client.search_users.return_value = []

        result = await service.discover_instagram_for_venue("v1", force_refresh=True)

        assert result.status == "not_found"
        assert result.confidence_score == 0.0
        mock_venue_dao.set_venue_instagram.assert_called_once()

    @pytest.mark.asyncio
    async def test_high_confidence_match_found(
        self, service, mock_apify_client, mock_validator, mock_venue_dao
    ):
        """High confidence candidate -> status=found."""
        # search_users returns InstagramProfile directly (no separate get_profile)
        mock_apify_client.search_users.return_value = [
            InstagramProfile(
                username="barconchittas",
                full_name="Bar Conchittas",
                biography="Bar in Recife",
                followers_count=5000,
                is_business_account=True,
            )
        ]
        mock_validator.validate.return_value = InstagramValidationResult(
            username="barconchittas",
            confidence_score=0.85,
            signals={},
            is_match=True,
        )

        result = await service.discover_instagram_for_venue("v1", force_refresh=True)

        assert result.status == "found"
        assert result.instagram_handle == "barconchittas"
        assert result.confidence_score == 0.85
        mock_venue_dao.set_venue_instagram.assert_called_once()
        # No get_profile call needed
        mock_apify_client.get_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_match(
        self, service, mock_apify_client, mock_validator
    ):
        """Candidate between thresholds -> status=low_confidence."""
        mock_apify_client.search_users.return_value = [
            InstagramProfile(
                username="conchittas_official",
                followers_count=1000,
            )
        ]
        mock_validator.validate.return_value = InstagramValidationResult(
            username="conchittas_official",
            confidence_score=0.60,
            signals={},
            is_match=True,
        )

        result = await service.discover_instagram_for_venue("v1", force_refresh=True)

        assert result.status == "low_confidence"
        assert result.instagram_handle == "conchittas_official"

    @pytest.mark.asyncio
    async def test_all_candidates_below_threshold(
        self, service, mock_apify_client, mock_validator
    ):
        """All candidates score below minimum -> not_found."""
        mock_apify_client.search_users.return_value = [
            InstagramProfile(username="random_user", followers_count=100)
        ]
        mock_validator.validate.return_value = InstagramValidationResult(
            username="random_user",
            confidence_score=0.20,
            signals={},
            is_match=False,
        )

        result = await service.discover_instagram_for_venue("v1", force_refresh=True)

        assert result.status == "not_found"

    @pytest.mark.asyncio
    async def test_early_exit_on_high_confidence(
        self, service, mock_apify_client, mock_validator
    ):
        """Stops checking candidates after finding high-confidence match."""
        mock_apify_client.search_users.return_value = [
            InstagramProfile(username="barconchittas", followers_count=5000, is_business_account=True),
            InstagramProfile(username="conchittas_bar", followers_count=3000),
            InstagramProfile(username="bar_conchittas_recife", followers_count=1000),
        ]
        mock_validator.validate.return_value = InstagramValidationResult(
            username="barconchittas",
            confidence_score=0.90,
            signals={},
            is_match=True,
        )

        await service.discover_instagram_for_venue("v1", force_refresh=True)

        # Should have validated only once (early exit after first high score)
        assert mock_validator.validate.call_count == 1

    @pytest.mark.asyncio
    async def test_venue_not_found_in_dao(self, service, mock_venue_dao):
        """Venue doesn't exist -> returns None."""
        mock_venue_dao.get_venue.return_value = None

        result = await service.discover_instagram_for_venue("v999", force_refresh=True)

        assert result is None

    @pytest.mark.asyncio
    async def test_best_candidate_selected(
        self, service, mock_apify_client, mock_validator
    ):
        """With multiple candidates, highest score wins."""
        mock_apify_client.search_users.return_value = [
            InstagramProfile(username="user_a", followers_count=100),
            InstagramProfile(username="user_b", followers_count=5000, is_business_account=True),
        ]

        validations = [
            InstagramValidationResult(
                username="user_a", confidence_score=0.40, signals={}, is_match=False
            ),
            InstagramValidationResult(
                username="user_b", confidence_score=0.65, signals={}, is_match=True
            ),
        ]
        mock_validator.validate.side_effect = validations

        result = await service.discover_instagram_for_venue("v1", force_refresh=True)

        assert result.instagram_handle == "user_b"
        assert result.status == "low_confidence"


class TestEnrichAllVenues:
    """Test bulk enrichment."""

    @pytest.mark.asyncio
    async def test_skips_cached_venues(self, service, mock_venue_dao, mock_apify_client):
        """Already-cached venues are skipped."""
        mock_venue_dao.get_venue_instagram.return_value = VenueInstagram(
            venue_id="v1", status="found", confidence_score=0.85
        )

        await service.enrich_all_venues()

        mock_apify_client.search_users.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_after_credit_exhaustion(
        self, service, mock_venue_dao, mock_apify_client
    ):
        """ApifyCreditExhaustedError sets flag but continues loop."""
        mock_venue_dao.get_venue_instagram.return_value = None
        mock_apify_client.search_users.side_effect = ApifyCreditExhaustedError("402")

        result = await service.enrich_all_venues()

        assert result == 0
        # Only called once — second venue skipped after exhaustion flag
        assert mock_apify_client.search_users.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_venue_list(self, service, mock_venue_dao):
        """No venues -> returns 0."""
        mock_venue_dao.list_all_venue_ids.return_value = []

        result = await service.enrich_all_venues()

        assert result == 0


class TestExtractCity:
    """Test _extract_city helper."""

    def test_recife_address(self):
        city = InstagramEnrichmentService._extract_city(
            "R. do Hospício, 51 - Boa Vista, Recife - PE"
        )
        assert city == "Recife"

    def test_olinda_address(self):
        city = InstagramEnrichmentService._extract_city(
            "Av. Sigismundo Gonçalves, Carmo - Olinda"
        )
        assert city == "Olinda"

    def test_unknown_city_defaults(self):
        city = InstagramEnrichmentService._extract_city("123 Unknown Street")
        assert city == "Recife"


class TestParseInstagramHandle:
    """Test Instagram handle extraction from website URLs.

    Duplicates the regex logic to avoid importing GooglePlacesEnrichmentService
    (which triggers global Settings() at module load).
    """

    @staticmethod
    def _parse(url: str):
        """Mirror of GooglePlacesEnrichmentService._parse_instagram_handle."""
        import re
        match = re.match(
            r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)",
            url.strip(),
        )
        if match:
            handle = match.group(1)
            if handle.lower() in ("p", "explore", "reel", "stories", "accounts", "about"):
                return None
            return handle
        return None

    def test_standard_url(self):
        assert self._parse("https://www.instagram.com/barconchittas/") == "barconchittas"

    def test_without_www(self):
        assert self._parse("https://instagram.com/cervejaria.alphaiate") == "cervejaria.alphaiate"

    def test_with_query_params(self):
        assert self._parse("https://www.instagram.com/champagneclub_recife?hl=pt-br") == "champagneclub_recife"

    def test_http_url(self):
        assert self._parse("http://instagram.com/meu_bar") == "meu_bar"

    def test_non_instagram_url(self):
        assert self._parse("https://www.barconchittas.com.br") is None

    def test_instagram_post_url_ignored(self):
        assert self._parse("https://www.instagram.com/p/ABC123/") is None

    def test_instagram_explore_ignored(self):
        assert self._parse("https://www.instagram.com/explore/") is None

    def test_empty_string(self):
        assert self._parse("") is None
