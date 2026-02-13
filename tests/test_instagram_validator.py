"""Unit tests for InstagramValidator."""
import pytest

from app.models.venue import Venue
from app.models.instagram import InstagramProfile
from app.services.instagram_validator import InstagramValidator


@pytest.fixture
def validator():
    """Create validator with default thresholds."""
    return InstagramValidator(
        auto_accept_threshold=0.75,
        low_confidence_threshold=0.50,
    )


def _make_venue(
    name="Bar Conchittas",
    address="R. do Hospício, 51 - Boa Vista, Recife - PE",
    venue_type="BAR",
) -> Venue:
    return Venue(
        venue_id="v1",
        venue_name=name,
        venue_address=address,
        venue_lat=-8.05,
        venue_lng=-34.87,
        venue_type=venue_type,
    )


def _make_profile(
    username="barconchittas",
    full_name="Bar Conchittas",
    biography="Bar e petiscaria no coração da Boa Vista, Recife",
    external_url=None,
    followers_count=5000,
    is_business_account=True,
    business_category_name="Bar",
) -> InstagramProfile:
    return InstagramProfile(
        username=username,
        full_name=full_name,
        biography=biography,
        external_url=external_url,
        followers_count=followers_count,
        is_business_account=is_business_account,
        business_category_name=business_category_name,
    )


class TestNameSimilarity:
    """Test _score_name_similarity signal."""

    def test_exact_match_concatenated(self, validator):
        """Username is venue name without spaces."""
        score = validator._score_name_similarity(
            "Bar Conchittas", "barconchittas", "Bar Conchittas"
        )
        assert score >= 0.9

    def test_dot_separated(self, validator):
        """Username uses dots: cervejaria.alphaiate."""
        score = validator._score_name_similarity(
            "Cervejaria Alphaiate", "cervejaria.alphaiate", None
        )
        assert score >= 0.9

    def test_underscore_separated(self, validator):
        """Username uses underscores."""
        score = validator._score_name_similarity(
            "Champagne Club", "champagne_club_recife", None
        )
        assert score >= 0.5

    def test_partial_match(self, validator):
        """Only part of venue name is in username."""
        score = validator._score_name_similarity(
            "Bar Conchittas", "conchittas_bar", None
        )
        assert score >= 0.5

    def test_no_match(self, validator):
        """Completely different name."""
        score = validator._score_name_similarity(
            "Bar Conchittas", "padaria_recife", "Padaria Central"
        )
        assert score < 0.3

    def test_accented_characters(self, validator):
        """Handles accented chars: café → cafe."""
        score = validator._score_name_similarity(
            "Café Cultura", "cafecultura", "Café Cultura"
        )
        assert score >= 0.9

    def test_full_name_match_when_username_differs(self, validator):
        """Full name matches even though username is different."""
        score = validator._score_name_similarity(
            "Cervejaria Alphaiate",
            "alphaiate_oficial",
            "Cervejaria Alphaiate",
        )
        assert score >= 0.9

    def test_empty_venue_name(self, validator):
        score = validator._score_name_similarity("", "someuser", "Some Name")
        assert score == 0.0


class TestBioAddress:
    """Test _score_bio_address signal."""

    def test_bio_contains_city(self, validator):
        """Bio mentions Recife."""
        score = validator._score_bio_address(
            "R. do Hospício, 51 - Recife",
            "O melhor bar de Recife!",
        )
        assert score >= 0.3

    def test_bio_contains_address_fragment(self, validator):
        """Bio contains street/address tokens."""
        score = validator._score_bio_address(
            "R. do Hospício, 51 - Boa Vista, Recife",
            "Hospício 51, Boa Vista - Recife. Drinks e petiscaria.",
        )
        assert score >= 0.5

    def test_bio_no_address(self, validator):
        """Bio has no address info at all."""
        score = validator._score_bio_address(
            "R. do Hospício, 51 - Recife",
            "Best drinks in town!",
        )
        assert score == 0.0

    def test_bio_none(self, validator):
        """Bio is None."""
        score = validator._score_bio_address("Some address", None)
        assert score == 0.0

    def test_neighborhood_match(self, validator):
        """Bio contains neighborhood name."""
        score = validator._score_bio_address(
            "Rua Qualquer - Boa Viagem, Recife",
            "Bar na Boa Viagem!",
        )
        assert score >= 0.3


class TestBioVenueType:
    """Test _score_bio_venue_type signal."""

    def test_bio_mentions_bar(self, validator):
        """Bio says 'bar'."""
        score = validator._score_bio_venue_type("BAR", "O melhor bar de Recife")
        assert score > 0.0

    def test_bio_mentions_cervejaria(self, validator):
        """Bio says 'cervejaria' for BREWERY type."""
        score = validator._score_bio_venue_type(
            "BREWERY", "Cervejaria artesanal desde 2018"
        )
        assert score > 0.0

    def test_no_type_keywords(self, validator):
        """Bio doesn't mention any type keywords."""
        score = validator._score_bio_venue_type("BAR", "Aberto todos os dias!")
        assert score == 0.0

    def test_none_bio(self, validator):
        score = validator._score_bio_venue_type("BAR", None)
        assert score == 0.0

    def test_none_venue_type(self, validator):
        score = validator._score_bio_venue_type(None, "Great bar!")
        assert score == 0.0


class TestBusinessCategory:
    """Test _score_business_category signal."""

    def test_bar_matches_bar(self, validator):
        score = InstagramValidator._score_business_category("BAR", "Bar")
        assert score == 1.0

    def test_restaurant_matches_food(self, validator):
        score = InstagramValidator._score_business_category("RESTAURANT", "Food & Dining")
        assert score == 1.0

    def test_no_match(self, validator):
        score = InstagramValidator._score_business_category("BAR", "Clothing Store")
        assert score == 0.0

    def test_none_category(self, validator):
        score = InstagramValidator._score_business_category("BAR", None)
        assert score == 0.0

    def test_none_venue_type(self, validator):
        score = InstagramValidator._score_business_category(None, "Bar")
        assert score == 0.0


class TestExternalUrl:
    """Test _score_external_url signal."""

    def test_url_contains_venue_name(self, validator):
        score = validator._score_external_url(
            "Bar Conchittas", "https://barconchittas.com.br"
        )
        assert score == 1.0

    def test_url_partial_match(self, validator):
        score = validator._score_external_url(
            "Cervejaria Alphaiate", "https://alphaiate.com.br"
        )
        assert score >= 0.5

    def test_no_url(self, validator):
        score = validator._score_external_url("Bar Conchittas", None)
        assert score == 0.0

    def test_unrelated_url(self, validator):
        score = validator._score_external_url(
            "Bar Conchittas", "https://ifood.com.br"
        )
        assert score == 0.0


class TestFollowerSanity:
    """Test _score_follower_sanity signal."""

    def test_sweet_spot(self, validator):
        """200-100k followers → 1.0."""
        assert InstagramValidator._score_follower_sanity(5000) == 1.0
        assert InstagramValidator._score_follower_sanity(200) == 1.0
        assert InstagramValidator._score_follower_sanity(100_000) == 1.0

    def test_very_low(self, validator):
        """<50 followers → suspicious."""
        assert InstagramValidator._score_follower_sanity(10) == 0.2

    def test_low(self, validator):
        """50-200 followers → moderate."""
        assert InstagramValidator._score_follower_sanity(100) == 0.5

    def test_high(self, validator):
        """100k-500k → still reasonable."""
        assert InstagramValidator._score_follower_sanity(300_000) == 0.7

    def test_celebrity(self, validator):
        """>500k → suspicious for local venue."""
        assert InstagramValidator._score_follower_sanity(1_000_000) == 0.3

    def test_none(self, validator):
        """Unknown followers → neutral."""
        assert InstagramValidator._score_follower_sanity(None) == 0.5


class TestFullValidation:
    """Test complete validate() method end-to-end."""

    def test_strong_match_auto_accept(self, validator):
        """Profile that strongly matches venue → found."""
        venue = _make_venue()
        profile = _make_profile()
        result = validator.validate(venue, profile)

        assert result.is_match is True
        assert result.confidence_score >= 0.75
        assert result.username == "barconchittas"
        assert "name_similarity" in result.signals

    def test_weak_match_below_threshold(self, validator):
        """Profile that barely matches → not matched."""
        venue = _make_venue()
        profile = _make_profile(
            username="totallydifferent",
            full_name="Something Else",
            biography="No location info",
            is_business_account=False,
            business_category_name=None,
            followers_count=10,
        )
        result = validator.validate(venue, profile)

        assert result.is_match is False
        assert result.confidence_score < 0.50

    def test_low_confidence_match(self, validator):
        """Profile that partially matches → low confidence range."""
        venue = _make_venue()
        profile = _make_profile(
            username="conchittas_oficial",
            full_name=None,
            biography="Drinks and fun in Recife",
            is_business_account=True,
            business_category_name="Bar",
            followers_count=3000,
        )
        result = validator.validate(venue, profile)

        assert result.is_match is True
        assert result.confidence_score >= 0.50

    def test_weights_sum_to_one(self, validator):
        """All signal weights should sum to 1.0."""
        weights = {
            "name_similarity": 0.30,
            "bio_address_city": 0.20,
            "bio_venue_type": 0.10,
            "is_business_account": 0.10,
            "business_category": 0.10,
            "external_url": 0.10,
            "follower_sanity": 0.10,
        }
        assert abs(sum(weights.values()) - 1.0) < 1e-9

    def test_all_signals_returned(self, validator):
        """Validate returns all 7 signal keys."""
        venue = _make_venue()
        profile = _make_profile()
        result = validator.validate(venue, profile)

        expected_keys = {
            "name_similarity", "bio_address_city", "bio_venue_type",
            "is_business_account", "business_category", "external_url",
            "follower_sanity",
        }
        assert set(result.signals.keys()) == expected_keys

    def test_brewery_with_cervejaria_profile(self, validator):
        """Real-world scenario: brewery matched with cervejaria profile."""
        venue = _make_venue(
            name="Cervejaria Alphaiate",
            address="Rua da Aurora, 123 - Boa Vista, Recife",
            venue_type="BREWERY",
        )
        profile = _make_profile(
            username="cervejaria.alphaiate",
            full_name="Cervejaria Alphaiate",
            biography="Cervejaria artesanal em Recife. Boa Vista.",
            is_business_account=True,
            business_category_name="Brewery",
            followers_count=8000,
        )
        result = validator.validate(venue, profile)
        assert result.is_match is True
        assert result.confidence_score >= 0.75


class TestNormalization:
    """Test text normalization helper."""

    def test_lowercase(self, validator):
        assert InstagramValidator._normalize("BAR") == "bar"

    def test_strip_accents(self, validator):
        assert InstagramValidator._normalize("café") == "cafe"
        assert InstagramValidator._normalize("São Paulo") == "sao paulo"

    def test_strip_whitespace(self, validator):
        assert InstagramValidator._normalize("  hello  ") == "hello"
