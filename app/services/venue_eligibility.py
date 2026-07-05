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

import copy
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Protocol

from app.models.venue_category import resolve_category

logger = logging.getLogger(__name__)

ADMIN_CONFIG_ELIGIBILITY_KEY = "admin_config:venue_eligibility"
# Redis mirror of the geo-fence (admin.geo_fence enabled flag + the
# admin.geo_fence_city circles; RDS is the durable truth the SQL view reads).
# This mirror serves the admin GET + parity reads only.
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

# ── State-capital catalog (the only cities the geo-fence can hold) ───────────
# The 26 Brazilian state capitals + Brasília, city-center coordinates accurate
# to ≤0.05°. The server owns ALL coordinates: admins configure slug + radius
# only, and every read resolves coords from this catalog. Adding another
# municipality later is a one-line edit here, not a schema change.
STATE_CAPITALS: tuple[dict, ...] = (
    {"slug": "aracaju", "name": "Aracaju", "lat": -10.9472, "lng": -37.0731},
    {"slug": "belem", "name": "Belém", "lat": -1.4558, "lng": -48.4902},
    {"slug": "belo-horizonte", "name": "Belo Horizonte", "lat": -19.9167, "lng": -43.9345},
    {"slug": "boa-vista", "name": "Boa Vista", "lat": 2.8235, "lng": -60.6758},
    {"slug": "brasilia", "name": "Brasília", "lat": -15.7939, "lng": -47.8828},
    {"slug": "campo-grande", "name": "Campo Grande", "lat": -20.4697, "lng": -54.6201},
    {"slug": "cuiaba", "name": "Cuiabá", "lat": -15.6014, "lng": -56.0979},
    {"slug": "curitiba", "name": "Curitiba", "lat": -25.4284, "lng": -49.2733},
    {"slug": "florianopolis", "name": "Florianópolis", "lat": -27.5954, "lng": -48.5480},
    {"slug": "fortaleza", "name": "Fortaleza", "lat": -3.7319, "lng": -38.5267},
    {"slug": "goiania", "name": "Goiânia", "lat": -16.6869, "lng": -49.2648},
    {"slug": "joao-pessoa", "name": "João Pessoa", "lat": -7.1195, "lng": -34.8450},
    {"slug": "macapa", "name": "Macapá", "lat": 0.0349, "lng": -51.0694},
    {"slug": "maceio", "name": "Maceió", "lat": -9.6498, "lng": -35.7089},
    {"slug": "manaus", "name": "Manaus", "lat": -3.1190, "lng": -60.0217},
    {"slug": "natal", "name": "Natal", "lat": -5.7945, "lng": -35.2110},
    {"slug": "palmas", "name": "Palmas", "lat": -10.1844, "lng": -48.3336},
    {"slug": "porto-alegre", "name": "Porto Alegre", "lat": -30.0346, "lng": -51.2177},
    {"slug": "porto-velho", "name": "Porto Velho", "lat": -8.7612, "lng": -63.9004},
    {"slug": "recife", "name": "Recife", "lat": -8.0476, "lng": -34.8770},
    {"slug": "rio-branco", "name": "Rio Branco", "lat": -9.9754, "lng": -67.8249},
    {"slug": "rio-de-janeiro", "name": "Rio de Janeiro", "lat": -22.9068, "lng": -43.1729},
    {"slug": "salvador", "name": "Salvador", "lat": -12.9714, "lng": -38.5014},
    {"slug": "sao-luis", "name": "São Luís", "lat": -2.5307, "lng": -44.3068},
    {"slug": "sao-paulo", "name": "São Paulo", "lat": -23.5505, "lng": -46.6333},
    {"slug": "teresina", "name": "Teresina", "lat": -5.0892, "lng": -42.8019},
    {"slug": "vitoria", "name": "Vitória", "lat": -20.3155, "lng": -40.3128},
)
CAPITALS_BY_SLUG: dict[str, dict] = {c["slug"]: c for c in STATE_CAPITALS}

# Admin-tunable circle radius bounds (km). The 1 km floor prevents the
# serve-nothing cliff of a degenerate circle; 200 km comfortably covers any
# metro region while keeping a fat-fingered "20000" out.
MIN_RADIUS_KM = 1.0
MAX_RADIUS_KM = 200.0

# ── Default geo-fence: Recife @ 40 km ─────────────────────────────────────────
# A venue is served only if it falls inside ANY enabled circle (fail-open on
# missing coords). The 40 km Recife circle strictly contains the pre-0015
# bounding box (farthest box corner ≈37.3 km from the center), so migrating
# never shrinks the serving set. Admin-editable via admin.geo_fence(_city) +
# the Redis mirror; the SQL serving view enforces the same predicate in parity.
DEFAULT_GEO_FENCE: dict = {
    "enabled": True,
    "cities": [{**CAPITALS_BY_SLUG["recife"], "radius_km": 40.0}],
}


def default_geo_fence() -> dict:
    """A fresh deep copy of the default fence — the nested cities list must
    never be shared with (or mutated by) callers."""
    return copy.deepcopy(DEFAULT_GEO_FENCE)

# ── BestTime types that must never reach clients ─────────────────────────────
DEFAULT_BLOCKED_VENUE_TYPES: set[str] = {
    "SHOPPING", "SHOPPING_CENTER", "DEPARTMENT_STORE",
    "SUPERMARKET", "GROCERY", "MARKET", "COFFEE", "FAST_FOOD", "BAKERY",
    "DESSERT", "LIBRARY", "SCHOOL", "CHURCH", "TEMPLE", "GYM", "FITNESS",
    "HOSPITAL", "PHARMACY", "BANK", "GAS_STATION", "MUSEUM",
    "MODERN_ART_MUSEUM", "APPAREL", "GIFTS", "PERSONAL_CARE",
    "TELECOMMUNICATIONS_SERVICE_PROVIDER", "BUSINESS_MANAGEMENT_CONSULTANT",
    "SOCIAL_SERVICES_ORGANIZATION", "TOURIST_DESTINATION", "HISTORICAL",
    "SPORTS_COMPLEX", "SPORTS_CLUB", "GOLF", "BOATING",
}

# ── Google Places types that must never reach clients ────────────────────────
# More accurate than BestTime types since Google classifies venues correctly.
DEFAULT_BLOCKED_GOOGLE_TYPES: set[str] = {
    "garden", "national_park",
    "shopping_mall", "department_store",
    "store", "home_goods_store", "furniture_store", "electronics_store",
    "auto_parts_store", "cell_phone_store", "shoe_store", "cosmetics_store",
    "candy_store", "food_store", "health_food_store",
    "museum", "art_museum", "history_museum",
    "library", "book_store",
    "warehouse_store", "building_materials_store",
    "drugstore", "pharmacy",
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


# ── Capital-circle geo-fence: a separate, reversible serve-time predicate ─────
# Geo-exclusion is intentionally NOT part of evaluate()/soft_deletable. Serving
# membership = (not evaluate().soft_deletable) AND (not geo_excluded(...)). Keeping
# it separate preserves the "never irreversibly hide a real bar" policy: an
# out-of-fence venue is dropped from serving but never soft-deleted.
_LEGACY_BOX_KEYS = ("min_lat", "max_lat", "min_lng", "max_lng")

# Mean-Earth radius (km) used by the SQL view's haversine — keep in lockstep
# with migration 0015 so the Python predicate and the view agree at boundaries.
_EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km (mean Earth radius, matches the SQL view)."""
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(
        math.sin(math.radians(lat2 - lat1) / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    ))


def validate_geo_fence(data: dict) -> dict:
    """Validate an admin geo-fence payload and return the normalized fence:
    ``{"enabled": bool, "cities": [{slug, name, lat, lng, radius_km}]}``.

    The payload carries slug + radius_km per city; coordinates are ALWAYS
    resolved from STATE_CAPITALS (the server owns them — caller-sent coords are
    ignored). Raises ValueError on: a legacy bounding-box payload, an unknown or
    duplicate slug, a non-numeric or out-of-[MIN_RADIUS_KM, MAX_RADIUS_KM]
    radius, a non-boolean `enabled`, or `enabled` true with zero cities (the
    serve-everything cliff). `enabled` false with zero cities is a valid "fence
    off" state. The admin endpoint rejects the write on any error, leaving the
    active fence unchanged.
    """
    if not isinstance(data, dict):
        raise ValueError("geo-fence must be a JSON object")
    if any(key in data for key in _LEGACY_BOX_KEYS):
        raise ValueError(
            "the bounding-box geo-fence was replaced by capital-city circles; "
            'send {"enabled": bool, "cities": [{"slug": ..., "radius_km": ...}]}'
        )

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    cities = data.get("cities")
    if cities is None:
        raise ValueError('cities is required (a list of {"slug", "radius_km"})')
    if not isinstance(cities, list):
        raise ValueError('cities must be a list of {"slug", "radius_km"}')
    if enabled and not cities:
        raise ValueError("an enabled geo-fence requires at least one city")

    seen: set[str] = set()
    normalized: list[dict] = []
    for entry in cities:
        if not isinstance(entry, dict):
            raise ValueError('each city must be an object with "slug" and "radius_km"')
        slug = entry.get("slug")
        capital = CAPITALS_BY_SLUG.get(slug) if isinstance(slug, str) else None
        if capital is None:
            raise ValueError(f"unknown capital slug: {slug!r}")
        if slug in seen:
            raise ValueError(f"duplicate capital slug: {slug}")
        seen.add(slug)
        radius = entry.get("radius_km")
        if isinstance(radius, bool) or not isinstance(radius, (int, float)):
            raise ValueError(f"radius_km for {slug} must be a number")
        if not (MIN_RADIUS_KM <= float(radius) <= MAX_RADIUS_KM):
            raise ValueError(
                f"radius_km for {slug} must be between "
                f"{MIN_RADIUS_KM:g} and {MAX_RADIUS_KM:g} km"
            )
        normalized.append({**capital, "radius_km": float(radius)})
    # Canonical order: by name, matching the store's ORDER BY — so the PUT
    # response, the Redis mirror, and every GET agree byte-for-byte.
    normalized.sort(key=lambda c: c["name"])
    return {"enabled": enabled, "cities": normalized}


def geo_excluded(lat, lng, fence: Optional[dict]) -> bool:
    """True when a venue's coordinates fall OUTSIDE every enabled fence circle.

    Fail-open: a missing/disabled fence, missing coordinates, or an empty or
    malformed city list never exclude (mirrors the reversible policy and the SQL
    view's fail-open predicate). Each city holds catalog lat/lng + radius_km;
    membership is haversine distance ≤ radius to ANY circle.
    """
    if not fence or not fence.get("enabled", True):
        return False
    if lat is None or lng is None:
        return False
    cities = fence.get("cities")
    if not isinstance(cities, list) or not cities:
        return False
    try:
        return all(
            haversine_km(lat, lng, c["lat"], c["lng"]) > c["radius_km"]
            for c in cities
        )
    except (KeyError, TypeError):
        # A malformed fence must never break filtering — treat as fail-open.
        return False


def load_geo_fence(redis_like: Optional[_SupportsGet]) -> dict:
    """Read the live geo-fence from the Redis mirror, falling back to the seeded
    default. A missing key, unreadable client, malformed JSON, or invalid shape —
    including a pre-0015 legacy bounding-box blob — all degrade to the default
    fence so filtering never breaks (mirrors load_eligibility_config). RDS
    (admin.geo_fence + admin.geo_fence_city) is the durable truth."""
    if redis_like is None:
        return default_geo_fence()
    try:
        raw = redis_like.get(ADMIN_CONFIG_GEOFENCE_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            f"[venue_eligibility] Failed to read {ADMIN_CONFIG_GEOFENCE_KEY}, "
            f"using default fence: {e}"
        )
        return default_geo_fence()

    if raw is None:
        return default_geo_fence()

    try:
        return validate_geo_fence(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(
            f"[venue_eligibility] {ADMIN_CONFIG_GEOFENCE_KEY} is invalid "
            f"({e}); using default fence"
        )
        return default_geo_fence()
