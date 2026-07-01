"""Centralized venue eligibility evaluation.

This module owns the single decision used by serving, inventory sync, discovery,
and the eligibility sweep to decide whether a venue belongs in the active,
user-facing inventory.

Policy: **block-list**. A venue is eligible unless it positively matches a block
rule. Unknown/unlabeled venues (e.g. BestTime ``OTHER`` with no Google type)
stay eligible — the goal is to never irreversibly hide a real bar.

Two confidence levels drive the irreversibility trade-off (soft-delete is
one-way in V1):
  - ``high``  → safe to soft-delete at write time / during the sweep with no
    further evidence (empty name, blocked BestTime/Google type, hard name
    keyword).
  - ``low``   → exclude at serve time (reversible) but **never** soft-delete
    before Google labeling. Ambiguous name keywords (tokens that legitimately
    appear in real bar names, like "mercado" or "parque") are low confidence
    until a Google type confirms a non-good category.

The block-lists are admin-tunable, live, via the Redis key
``admin_config:venue_eligibility``. Missing/invalid config falls back to the
hardcoded defaults below so a bad admin write can never break filtering.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol

from app.models.venue_category import resolve_category

logger = logging.getLogger(__name__)

ADMIN_CONFIG_ELIGIBILITY_KEY = "admin_config:venue_eligibility"
# Redis mirror of the admin.geo_fence box (RDS is the durable truth the SQL view
# reads). This mirror serves the admin GET + parity reads only.
ADMIN_CONFIG_GEOFENCE_KEY = "admin_config:venue_geofence"

# Source label used on every soft-delete this module drives.
ELIGIBILITY_SOURCE = "eligibility_filter"

# Rejection reasons (also used as Prometheus metric label values).
REASON_EMPTY_NAME = "ineligible_empty_name"
REASON_NAME_KEYWORD = "ineligible_name_keyword"
REASON_BESTTIME_TYPE = "ineligible_besttime_type"
REASON_GOOGLE_TYPE = "ineligible_google_type"
# Geo-fence exclusion is a THIRD state: not-servable but never soft-deletable
# (reversible serve-time filter). It is applied as a separate predicate
# (`geo_excluded`), NOT folded into evaluate()'s eligible/soft_deletable axis.
REASON_GEO = "ineligible_geo"

ALL_REASONS = (
    REASON_EMPTY_NAME,
    REASON_NAME_KEYWORD,
    REASON_BESTTIME_TYPE,
    REASON_GOOGLE_TYPE,
    REASON_GEO,
)

# ── Recife/Olinda metro geo-fence (default seeded box) ───────────────────────
# A venue is served only if its coordinates fall inside this box (fail-open on
# missing coords). Confirmed default box; admin-editable via admin.geo_fence + the
# Redis mirror. The SQL serving view enforces the same predicate in parity.
DEFAULT_GEO_FENCE: dict = {
    "min_lat": -8.30,
    "max_lat": -7.85,
    "min_lng": -35.10,
    "max_lng": -34.80,
    "enabled": True,
}

# ── BestTime types that must never reach clients ─────────────────────────────
DEFAULT_BLOCKED_VENUE_TYPES: set[str] = {
    "PARK", "CITY_PARK", "SHOPPING", "SHOPPING_CENTER", "DEPARTMENT_STORE",
    "SUPERMARKET", "GROCERY", "MARKET", "COFFEE", "FAST_FOOD", "BAKERY",
    "DESSERT", "LIBRARY", "SCHOOL", "CHURCH", "TEMPLE", "GYM", "FITNESS",
    "HOSPITAL", "PHARMACY", "BANK", "GAS_STATION", "MUSEUM",
    "MODERN_ART_MUSEUM", "APPAREL", "GIFTS", "PERSONAL_CARE",
    "TELECOMMUNICATIONS_SERVICE_PROVIDER", "BUSINESS_MANAGEMENT_CONSULTANT",
    "SOCIAL_SERVICES_ORGANIZATION", "TOURIST_DESTINATION", "HISTORICAL",
    "PLAZA", "SPORTS_COMPLEX", "SPORTS_CLUB", "GOLF", "BOATING",
}

# ── Google Places types that must never reach clients ────────────────────────
# More accurate than BestTime types since Google classifies venues correctly.
DEFAULT_BLOCKED_GOOGLE_TYPES: set[str] = {
    "park", "city_park", "garden", "national_park",
    "shopping_mall", "department_store",
    "store", "home_goods_store", "furniture_store", "electronics_store",
    "auto_parts_store", "cell_phone_store", "shoe_store", "cosmetics_store",
    "candy_store", "food_store", "health_food_store",
    "museum", "art_museum", "history_museum",
    "library", "book_store",
    "warehouse_store", "building_materials_store",
    "drugstore", "pharmacy",
    "plaza",
    "supermarket", "grocery_store",
    "liquor_store",
    "florist",
    "sports_club",
    "tour_agency",
    "consultant",
    "telecommunications_service_provider",
    "lodging", "hotel",
    "church", "mosque", "synagogue", "hindu_temple",
    "hospital", "doctor", "dentist",
    "school", "university",
    "gas_station",
    "bank", "atm",
    "gym",
    "post_office", "postal_code",
    "business_center",
}

# ── Name keywords: HARD (unambiguous, high confidence) ───────────────────────
# Tokens that never appear in a legitimate bar/restaurant/nightlife name.
# Matching one of these is safe to soft-delete even before Google labeling.
DEFAULT_HARD_BLOCKED_NAME_KEYWORDS: list[str] = [
    "farmácia", "farmacia", "drogaria",
    "hospital", "clínica", "clinica",
    "igreja", "catedral", "capela", "temple",
    "escola", "colégio", "colegio", "universidade", "faculdade",
    "supermercado", "atacado", "atacadão",
    "gym", "fitness",
    "caixa econômica", "bradesco", "itaú", "santander",
    "gas station",
    "loja ", "lojas ", "casas bahia", "magazine luiza", "americanas",
    "home center", "ferreira costa", "leroy merlin",
    "livraria", "biblioteca",
    "museu", "museum",
    "correios", "cartório", "delegacia", "tribunal",
    "estacionamento", "parking",
    "condomínio", "condominio", "edifício", "edificio",
    "pet shop", "veterinár",
    "barbearia",
    "floricultura",
    "nagem", "multicoisas", "game station",
]

# ── Name keywords: AMBIGUOUS (low confidence) ────────────────────────────────
# Tokens that legitimately appear in real bar/restaurant/event names
# ("Bar do Mercado", "Parque Bar", "Academia da Cachaça"). These only exclude
# at serve time and are never soft-deleted before a Google type confirms a
# non-good category.
DEFAULT_AMBIGUOUS_NAME_KEYWORDS: list[str] = [
    "shopping", "mall", "parque", "park", "praça", "plaza",
    "mercado",
    "academia",
    "banco",
    "posto",
    "instituto",
    "catamaran", "passeio", "tour",
    "salão", "salao",
    "plantation square",
    "tim ", "claro ", "vivo ",
]

# Backward-compatible union (some callers/tests still import the flat list).
BLOCKED_NAME_KEYWORDS: list[str] = (
    DEFAULT_HARD_BLOCKED_NAME_KEYWORDS + DEFAULT_AMBIGUOUS_NAME_KEYWORDS
)


class _SupportsGet(Protocol):
    def get(self, key: str) -> Optional[str]: ...


@dataclass(frozen=True)
class EligibilityConfig:
    """The effective block-lists used by a single evaluation pass."""

    blocked_venue_types: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_BLOCKED_VENUE_TYPES)
    )
    blocked_google_types: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_BLOCKED_GOOGLE_TYPES)
    )
    hard_blocked_name_keywords: tuple[str, ...] = field(
        default_factory=lambda: tuple(DEFAULT_HARD_BLOCKED_NAME_KEYWORDS)
    )
    ambiguous_name_keywords: tuple[str, ...] = field(
        default_factory=lambda: tuple(DEFAULT_AMBIGUOUS_NAME_KEYWORDS)
    )
    # True when the config came from the Redis admin override, False for defaults.
    from_admin_override: bool = False

    @classmethod
    def defaults(cls) -> "EligibilityConfig":
        return cls()

    @classmethod
    def from_dict(
        cls, data: dict, *, from_admin_override: bool = True
    ) -> "EligibilityConfig":
        """Build a config from an admin payload, validating each field.

        Any field that is absent falls back to its default list. A present field
        must be a list of strings or a ValueError is raised (so the admin
        endpoint can reject the write and leave the active config unchanged).
        """
        if not isinstance(data, dict):
            raise ValueError("eligibility config must be a JSON object")

        def _string_list(key: str, default: list[str]) -> list[str]:
            if key not in data or data[key] is None:
                return list(default)
            value = data[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"{key} must be a list of strings")
            return [item for item in value]

        venue_types = _string_list(
            "blocked_venue_types", list(DEFAULT_BLOCKED_VENUE_TYPES)
        )
        google_types = _string_list(
            "blocked_google_types", list(DEFAULT_BLOCKED_GOOGLE_TYPES)
        )
        hard = _string_list(
            "hard_blocked_name_keywords", DEFAULT_HARD_BLOCKED_NAME_KEYWORDS
        )
        ambiguous = _string_list(
            "ambiguous_name_keywords", DEFAULT_AMBIGUOUS_NAME_KEYWORDS
        )
        # Convenience alias: operators may send a single "blocked_name_keywords"
        # list. Operator-curated keywords are treated as hard (high confidence).
        if "blocked_name_keywords" in data and data["blocked_name_keywords"]:
            extra = _string_list("blocked_name_keywords", [])
            hard = hard + [kw for kw in extra if kw not in hard]

        return cls(
            blocked_venue_types=frozenset(t.upper() for t in venue_types),
            blocked_google_types=frozenset(t.lower() for t in google_types),
            hard_blocked_name_keywords=tuple(kw.lower() for kw in hard),
            ambiguous_name_keywords=tuple(kw.lower() for kw in ambiguous),
            from_admin_override=from_admin_override,
        )

    def to_public_dict(self) -> dict:
        """Serialize for the admin GET endpoint."""
        return {
            "blocked_venue_types": sorted(self.blocked_venue_types),
            "blocked_google_types": sorted(self.blocked_google_types),
            "hard_blocked_name_keywords": list(self.hard_blocked_name_keywords),
            "ambiguous_name_keywords": list(self.ambiguous_name_keywords),
            "source": "admin_override" if self.from_admin_override else "defaults",
        }


@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of evaluating a venue against the block-lists."""

    eligible: bool
    reason: Optional[str] = None
    confidence: str = "high"  # "high" | "low"

    @property
    def soft_deletable(self) -> bool:
        """True when this venue may be soft-deleted with no further evidence."""
        return (not self.eligible) and self.confidence == "high"


_ELIGIBLE = EligibilityResult(eligible=True)


def _matches_keyword(name_lower: str, keywords) -> bool:
    return any(kw in name_lower for kw in keywords)


def _has_good_category(google_type: Optional[str], besttime_type: Optional[str]) -> bool:
    """True when the venue positively resolves to a nightlife/food category."""
    return resolve_category(
        google_type=google_type, besttime_type=besttime_type
    ) != "OTHER"


def evaluate(
    venue_name: Optional[str],
    besttime_type: Optional[str] = None,
    google_type: Optional[str] = None,
    config: Optional[EligibilityConfig] = None,
) -> EligibilityResult:
    """Evaluate a venue against the block-list policy.

    Order matters: the most accurate signals (Google type) win first, then
    BestTime type, then name keywords. Google's positive classification
    suppresses ambiguous-keyword false positives.
    """
    cfg = config or EligibilityConfig.defaults()

    name = (venue_name or "").strip()
    if not name:
        return EligibilityResult(False, REASON_EMPTY_NAME, "high")

    name_lower = name.lower()
    gtype = (google_type or "").strip().lower() or None
    btype = (besttime_type or "").strip().upper() or None

    # 1. Blocked Google type — Google is accurate; high confidence.
    if gtype and gtype in cfg.blocked_google_types:
        return EligibilityResult(False, REASON_GOOGLE_TYPE, "high")

    # 2. Blocked BestTime type — high confidence.
    if btype and btype in cfg.blocked_venue_types:
        return EligibilityResult(False, REASON_BESTTIME_TYPE, "high")

    # A positive category from Google OR BestTime suppresses keyword false
    # positives ("Bar do Mercado" typed BAR, "Academia da Cachaça" typed bar).
    good_category = _has_good_category(gtype, btype)

    # 3. Hard name keyword — unambiguous junk. High confidence even unlabeled.
    if _matches_keyword(name_lower, cfg.hard_blocked_name_keywords):
        if good_category:
            return _ELIGIBLE
        return EligibilityResult(False, REASON_NAME_KEYWORD, "high")

    # 4. Ambiguous name keyword — appears in real bar names.
    if _matches_keyword(name_lower, cfg.ambiguous_name_keywords):
        if good_category:
            return _ELIGIBLE
        if gtype:
            # Labeled and Google confirms a non-good category → high junk.
            return EligibilityResult(False, REASON_NAME_KEYWORD, "high")
        # Unlabeled: exclude at serve time but never soft-delete pre-label.
        return EligibilityResult(False, REASON_NAME_KEYWORD, "low")

    # 5. Block-list default: anything not positively blocked is eligible,
    #    including unknown/unlabeled venues.
    return _ELIGIBLE


def load_eligibility_config(redis_like: Optional[_SupportsGet]) -> EligibilityConfig:
    """Read the live eligibility config from Redis, falling back to defaults.

    A missing key, unreadable client, malformed JSON, or invalid shape all
    degrade to the hardcoded defaults so filtering never breaks on a bad write.
    """
    if redis_like is None:
        return EligibilityConfig.defaults()
    try:
        raw = redis_like.get(ADMIN_CONFIG_ELIGIBILITY_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            f"[venue_eligibility] Failed to read {ADMIN_CONFIG_ELIGIBILITY_KEY}, "
            f"using defaults: {e}"
        )
        return EligibilityConfig.defaults()

    if raw is None:
        return EligibilityConfig.defaults()

    try:
        data = json.loads(raw)
        return EligibilityConfig.from_dict(data, from_admin_override=True)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(
            f"[venue_eligibility] {ADMIN_CONFIG_ELIGIBILITY_KEY} is invalid "
            f"({e}); using defaults"
        )
        return EligibilityConfig.defaults()


# ── Ex2: normalized eligibility rules <-> the override blob ──────────────────
# One admin.eligibility_rule row = (rule_type, value). rule_type maps to the
# from_dict blob key and the normalization a value of that type receives.
RULE_TYPE_TO_BLOB_KEY: dict[str, str] = {
    "blocked_venue_type": "blocked_venue_types",
    "blocked_google_type": "blocked_google_types",
    "hard_blocked_name_keyword": "hard_blocked_name_keywords",
    "ambiguous_name_keyword": "ambiguous_name_keywords",
}
RULE_TYPES: frozenset[str] = frozenset(RULE_TYPE_TO_BLOB_KEY)


def normalize_rule_value(rule_type: str, value: str) -> str:
    """Match EligibilityConfig.from_dict normalization: BestTime types upper,
    Google types + name keywords lower."""
    if rule_type not in RULE_TYPES:
        raise ValueError(f"unknown eligibility rule_type: {rule_type!r}")
    return value.upper() if rule_type == "blocked_venue_type" else value.lower()


def assemble_eligibility_blob(rules) -> dict:
    """Rows -> admin override blob. Only categories that HAVE rows appear, so a
    category with no rows is omitted and EligibilityConfig.from_dict fills its
    defaults (preserves per-category "absent == track defaults" semantics)."""
    blob: dict[str, list] = {}
    for rule_type, value in rules:
        key = RULE_TYPE_TO_BLOB_KEY[rule_type]
        bucket = blob.setdefault(key, [])
        if value not in bucket:
            bucket.append(value)
    return blob


def decompose_eligibility_blob(blob: dict) -> list[tuple[str, str]]:
    """Admin override blob -> normalized rows whose assembled effective config
    equals ``from_dict(blob)``.

    A category emits rows only when the blob actually OVERRODE it (its key is
    present — or, for hard keywords, the ``blocked_name_keywords`` alias is). For
    those, we emit the *effective* list from ``from_dict`` (so the alias's
    "defaults + extra" and all normalization are captured); categories the blob
    left untouched emit nothing and fall back to defaults at assembly. Note: a
    category set to an explicit empty list cannot be represented (it degrades to
    defaults) — the post-migration parity check guards that edge.
    """
    cfg = EligibilityConfig.from_dict(blob, from_admin_override=True)
    rows: set[tuple[str, str]] = set()
    if "blocked_venue_types" in blob:
        rows |= {("blocked_venue_type", v) for v in cfg.blocked_venue_types}
    if "blocked_google_types" in blob:
        rows |= {("blocked_google_type", v) for v in cfg.blocked_google_types}
    if "ambiguous_name_keywords" in blob:
        rows |= {("ambiguous_name_keyword", v) for v in cfg.ambiguous_name_keywords}
    if ("hard_blocked_name_keywords" in blob) or ("blocked_name_keywords" in blob):
        rows |= {("hard_blocked_name_keyword", v) for v in cfg.hard_blocked_name_keywords}
    return sorted(rows)


def eligibility_config_from_rules(
    rules, *, from_admin_override: bool = True
) -> EligibilityConfig:
    """Effective config from rows: hardcoded defaults when there are no rows
    (fail-safe), else from_dict over the assembled blob."""
    rules = list(rules)
    if not rules:
        return EligibilityConfig.defaults()
    return EligibilityConfig.from_dict(
        assemble_eligibility_blob(rules), from_admin_override=from_admin_override
    )


# ── Recife-metro geo-fence: a separate, reversible serve-time predicate ───────
# Geo-exclusion is intentionally NOT part of evaluate()/soft_deletable. Serving
# membership = (not evaluate().soft_deletable) AND (not geo_excluded(...)). Keeping
# it separate preserves the "never irreversibly hide a real bar" policy: an
# out-of-box venue is dropped from serving but never soft-deleted.
_BOX_KEYS = ("min_lat", "max_lat", "min_lng", "max_lng")


def validate_geo_fence(data: dict) -> dict:
    """Validate an admin geo-fence payload and return a normalized box dict.

    Requires numeric min_lat/max_lat/min_lng/max_lng with lat in [-90, 90], lng in
    [-180, 180], and min < max on both axes. `enabled` defaults to True. Raises
    ValueError on any invalid field so the admin endpoint can reject the write and
    leave the active box unchanged.
    """
    if not isinstance(data, dict):
        raise ValueError("geo-fence must be a JSON object")

    box: dict = {}
    for key in _BOX_KEYS:
        if key not in data or data[key] is None:
            raise ValueError(f"{key} is required")
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        box[key] = float(value)

    for lat_key in ("min_lat", "max_lat"):
        if not (-90.0 <= box[lat_key] <= 90.0):
            raise ValueError(f"{lat_key} must be between -90 and 90")
    for lng_key in ("min_lng", "max_lng"):
        if not (-180.0 <= box[lng_key] <= 180.0):
            raise ValueError(f"{lng_key} must be between -180 and 180")
    if box["min_lat"] >= box["max_lat"]:
        raise ValueError("min_lat must be less than max_lat")
    if box["min_lng"] >= box["max_lng"]:
        raise ValueError("min_lng must be less than max_lng")

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    box["enabled"] = enabled
    return box


def geo_excluded(lat, lng, box: Optional[dict]) -> bool:
    """True when a venue's coordinates place it OUTSIDE the enabled geo-fence box.

    Fail-open: a missing box, a disabled box, or missing coordinates never exclude
    (mirrors the reversible policy and the SQL view's fail-open LEFT JOIN). The box
    keys are min_lat/max_lat/min_lng/max_lng/enabled.
    """
    if not box or not box.get("enabled", True):
        return False
    if lat is None or lng is None:
        return False
    try:
        return not (
            box["min_lat"] <= lat <= box["max_lat"]
            and box["min_lng"] <= lng <= box["max_lng"]
        )
    except (KeyError, TypeError):
        # A malformed box must never break filtering — treat as fail-open.
        return False


def load_geo_fence(redis_like: Optional[_SupportsGet]) -> dict:
    """Read the live geo-fence box from the Redis mirror, falling back to the
    seeded default. A missing key, unreadable client, malformed JSON, or invalid
    shape all degrade to DEFAULT_GEO_FENCE so filtering never breaks (mirrors
    load_eligibility_config). RDS (admin.geo_fence) is the durable truth."""
    if redis_like is None:
        return dict(DEFAULT_GEO_FENCE)
    try:
        raw = redis_like.get(ADMIN_CONFIG_GEOFENCE_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            f"[venue_eligibility] Failed to read {ADMIN_CONFIG_GEOFENCE_KEY}, "
            f"using default box: {e}"
        )
        return dict(DEFAULT_GEO_FENCE)

    if raw is None:
        return dict(DEFAULT_GEO_FENCE)

    try:
        return validate_geo_fence(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(
            f"[venue_eligibility] {ADMIN_CONFIG_GEOFENCE_KEY} is invalid "
            f"({e}); using default box"
        )
        return dict(DEFAULT_GEO_FENCE)
