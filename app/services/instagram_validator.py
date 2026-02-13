"""Instagram profile-to-venue matching validation.

Scores an Instagram profile against a venue using multiple weighted signals
to determine if the profile belongs to that venue.
"""
import logging
import re
import unicodedata
from typing import Optional

from app.models.venue import Venue
from app.models.instagram import InstagramProfile, InstagramValidationResult

logger = logging.getLogger(__name__)


# Venue type keywords in Portuguese (matching venue_type values from BestTime)
VENUE_TYPE_KEYWORDS: dict[str, list[str]] = {
    "BAR": ["bar", "barzinho", "boteco", "pub"],
    "BREWERY": ["cervejaria", "brewery", "cerveja artesanal", "chopp"],
    "RESTAURANT": ["restaurante", "restaurant", "gastronomia", "culinaria"],
    "CAFE": ["cafe", "cafeteria", "coffee"],
    "CLUB": ["club", "balada", "nightclub", "casa noturna", "boate"],
    "LOUNGE": ["lounge", "rooftop"],
    "CONCERT_HALL": ["show", "musica", "concert", "teatro"],
    "EVENT_VENUE": ["evento", "event", "festa", "party"],
    "WINERY": ["vinho", "wine", "vinhos"],
    "CASINO": ["casino", "cassino"],
    "FOOD_AND_DRINK": ["comida", "food", "drink", "bebida"],
}

# Business category keywords from Instagram that correlate with venue types
BUSINESS_CATEGORY_MAP: dict[str, list[str]] = {
    "BAR": ["bar", "pub", "lounge", "wine bar", "cocktail bar"],
    "BREWERY": ["brewery", "beer garden", "cervejaria"],
    "RESTAURANT": ["restaurant", "food", "dining", "steakhouse", "pizza", "sushi"],
    "CAFE": ["cafe", "coffee shop", "bakery"],
    "CLUB": ["night club", "dance club", "music venue", "event venue", "nightlife"],
    "LOUNGE": ["lounge", "hookah"],
    "CONCERT_HALL": ["concert", "music venue", "theater"],
    "EVENT_VENUE": ["event", "venue", "party"],
    "WINERY": ["winery", "wine bar"],
}

# Known city/neighborhood names in the Recife metro area
CITY_KEYWORDS = [
    "recife", "olinda", "jaboatao", "paulista", "camaragibe",
    "boa viagem", "boa vista", "pina", "derby", "casa forte",
    "espinheiro", "graças", "aflitos", "madalena", "torre",
    "santo amaro", "marco zero", "recife antigo", "cordeiro",
]


class InstagramValidator:
    """Validates whether an Instagram profile matches a venue.

    Uses 7 weighted signals to produce a confidence score between 0 and 1.
    """

    def __init__(
        self,
        auto_accept_threshold: float = 0.75,
        low_confidence_threshold: float = 0.50,
    ):
        self.auto_accept_threshold = auto_accept_threshold
        self.low_confidence_threshold = low_confidence_threshold

    def validate(
        self, venue: Venue, profile: InstagramProfile
    ) -> InstagramValidationResult:
        """Score an Instagram profile against a venue.

        Signal weights (sum to 1.0):
          - name_similarity:        0.30
          - bio_address_city:       0.20
          - bio_venue_type:         0.10
          - is_business_account:    0.10
          - business_category:      0.10
          - external_url:           0.10
          - follower_sanity:        0.10
        """
        signals = {}

        signals["name_similarity"] = self._score_name_similarity(
            venue.venue_name, profile.username, profile.full_name
        )
        signals["bio_address_city"] = self._score_bio_address(
            venue.venue_address, profile.biography
        )
        signals["bio_venue_type"] = self._score_bio_venue_type(
            venue.venue_type, profile.biography
        )
        signals["is_business_account"] = 1.0 if profile.is_business_account else 0.0
        signals["business_category"] = self._score_business_category(
            venue.venue_type, profile.business_category_name
        )
        signals["external_url"] = self._score_external_url(
            venue.venue_name, profile.external_url
        )
        signals["follower_sanity"] = self._score_follower_sanity(
            profile.followers_count
        )

        weights = {
            "name_similarity": 0.30,
            "bio_address_city": 0.20,
            "bio_venue_type": 0.10,
            "is_business_account": 0.10,
            "business_category": 0.10,
            "external_url": 0.10,
            "follower_sanity": 0.10,
        }

        confidence = sum(signals[key] * weights[key] for key in weights)
        is_match = confidence >= self.low_confidence_threshold

        return InstagramValidationResult(
            username=profile.username,
            confidence_score=round(confidence, 4),
            signals={k: round(v, 4) for k, v in signals.items()},
            is_match=is_match,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text: lowercase, remove accents, strip special chars."""
        text = text.lower().strip()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c))
        return text

    def _score_name_similarity(
        self, venue_name: str, username: str, full_name: Optional[str]
    ) -> float:
        """Score how well the venue name matches the IG username/full name.

        Uses token overlap and substring containment. Handles patterns like
        "Bar Conchittas" -> "barconchittas" and "Cervejaria Alphaiate" -> "cervejaria.alphaiate".
        """
        norm_venue = self._normalize(venue_name)
        venue_tokens = set(re.split(r"[\s\-_&/]+", norm_venue))
        venue_tokens.discard("")

        if not venue_tokens:
            return 0.0

        best_score = 0.0

        # Check against username (split on _ and .)
        norm_username = self._normalize(username.replace("_", " ").replace(".", " "))
        username_tokens = set(re.split(r"\s+", norm_username))
        username_tokens.discard("")

        # Token overlap: what fraction of venue name tokens appear in username?
        if username_tokens:
            overlap = venue_tokens & username_tokens
            token_score = len(overlap) / len(venue_tokens)
            best_score = max(best_score, token_score)

        # Substring containment: does concatenated username contain all venue tokens?
        username_concat = self._normalize(
            username.replace("_", "").replace(".", "")
        )
        contained = sum(1 for t in venue_tokens if t in username_concat)
        containment_score = contained / len(venue_tokens)
        best_score = max(best_score, containment_score)

        # Check full_name if available
        if full_name:
            norm_full = self._normalize(full_name)
            full_tokens = set(re.split(r"[\s\-_&/]+", norm_full))
            full_tokens.discard("")
            if full_tokens:
                overlap = venue_tokens & full_tokens
                full_score = len(overlap) / len(venue_tokens)
                best_score = max(best_score, full_score)

        return min(best_score, 1.0)

    def _score_bio_address(
        self, venue_address: str, bio: Optional[str]
    ) -> float:
        """Score whether the bio contains address/city fragments."""
        if not bio:
            return 0.0

        norm_bio = self._normalize(bio)
        norm_address = self._normalize(venue_address)

        # Extract meaningful address tokens (skip common Portuguese address words)
        stop_words = {
            "r", "rua", "av", "avenida", "n", "no", "de", "do", "da",
            "dos", "das", "s/n", "pe", "brazil", "brasil",
        }
        address_tokens = set(re.split(r"[\s,.\-/]+", norm_address))
        address_tokens -= stop_words
        address_tokens = {t for t in address_tokens if len(t) > 2}

        if not address_tokens:
            # No meaningful address tokens, check city only
            city_match = any(city in norm_bio for city in CITY_KEYWORDS)
            return 0.3 if city_match else 0.0

        # Count address tokens found in bio
        matches = sum(1 for t in address_tokens if t in norm_bio)
        score = matches / len(address_tokens)

        # Bonus: city/neighborhood name in bio
        city_match = any(city in norm_bio for city in CITY_KEYWORDS)
        if city_match:
            score = min(score + 0.3, 1.0)

        return min(score, 1.0)

    def _score_bio_venue_type(
        self, venue_type: Optional[str], bio: Optional[str]
    ) -> float:
        """Score whether the bio mentions venue type keywords."""
        if not bio or not venue_type:
            return 0.0

        norm_bio = self._normalize(bio)
        keywords = VENUE_TYPE_KEYWORDS.get(venue_type.upper(), [])

        if not keywords:
            return 0.0

        matches = sum(1 for kw in keywords if kw in norm_bio)
        return min(matches / len(keywords), 1.0) if matches > 0 else 0.0

    @staticmethod
    def _score_business_category(
        venue_type: Optional[str], business_category: Optional[str]
    ) -> float:
        """Score whether the IG business category matches the venue type."""
        if not business_category or not venue_type:
            return 0.0

        norm_category = business_category.lower().strip()
        expected_keywords = BUSINESS_CATEGORY_MAP.get(venue_type.upper(), [])

        for kw in expected_keywords:
            if kw in norm_category:
                return 1.0

        return 0.0

    def _score_external_url(
        self, venue_name: str, external_url: Optional[str]
    ) -> float:
        """Score whether the profile's external URL relates to the venue."""
        if not external_url:
            return 0.0

        norm_url = self._normalize(external_url)
        norm_name = self._normalize(venue_name).replace(" ", "")

        # Check if venue name (concatenated) appears in the URL
        if norm_name in norm_url:
            return 1.0

        # Partial match: check if major tokens appear
        tokens = set(re.split(r"[\s\-_]+", self._normalize(venue_name)))
        tokens = {t for t in tokens if len(t) > 2}
        if tokens:
            matches = sum(1 for t in tokens if t in norm_url)
            return min(matches / len(tokens), 1.0)

        return 0.0

    @staticmethod
    def _score_follower_sanity(followers_count: Optional[int]) -> float:
        """Score follower count sanity for a local business.

        Most real venue accounts in Recife have 200-100,000 followers.
        Very low (<50) or very high (>500k) are suspicious.
        """
        if followers_count is None:
            return 0.5  # Unknown, neutral

        if followers_count < 50:
            return 0.2
        elif followers_count < 200:
            return 0.5
        elif followers_count <= 100_000:
            return 1.0
        elif followers_count <= 500_000:
            return 0.7
        else:
            return 0.3
