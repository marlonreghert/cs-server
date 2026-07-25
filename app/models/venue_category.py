"""VibeSense venue category mapping.

Maps Google Places granular types to VibeSense display categories.
Two levels of type info are served to the app:
  - category: display-level grouping (BAR, NIGHTCLUB, RESTAURANT, etc.)
  - granular_type: specific type for detail pages ("japanese_restaurant", "irish_pub")

Priority: google_primary_type > besttime venue_type > "OTHER"

The Google/BestTime → category maps are admin-tunable: an override stored in the
Redis key ``admin_config:venue_category_map`` is merged over the hardcoded
defaults below at serve time (see ``load_category_map``). Category is resolved
per request in the venue handler, never persisted, so an override applies on the
next serve with no venue re-fetch.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Redis key holding the admin override (mirror of the RDS admin_config row).
ADMIN_CONFIG_CATEGORY_MAP_KEY = "admin_config:venue_category_map"

# ── VibeSense display categories ─────────────────────────────────────────────
CATEGORIES = {
    "BAR":          {"label": "Bar",               "emoji": "🍺", "color": "#D97706"},
    "PUB":          {"label": "Pub",               "emoji": "🍻", "color": "#92400E"},
    "COCKTAIL_BAR": {"label": "Coquetelaria",      "emoji": "🍸", "color": "#DB2777"},
    "NIGHTCLUB":    {"label": "Balada",            "emoji": "💃", "color": "#7C3AED"},
    "KARAOKE":      {"label": "Karaokê",           "emoji": "🎤", "color": "#A855F7"},
    "BREWERY":      {"label": "Cervejaria",        "emoji": "🍺", "color": "#B45309"},
    "WINERY":       {"label": "Vinícola",          "emoji": "🍷", "color": "#991B1B"},
    "COFFEE_SHOP":  {"label": "Cafeteria",         "emoji": "☕", "color": "#78350F"},
    "BAKERY":       {"label": "Padaria",           "emoji": "🥐", "color": "#B7791F"},
    "RESTAURANT":   {"label": "Restaurante",       "emoji": "🍽️", "color": "#DC2626"},
    "BUFFET":       {"label": "Buffet",            "emoji": "🍱", "color": "#DC2626"},
    "FOOD_DRINK":   {"label": "Gastronomia",       "emoji": "🍴", "color": "#10B981"},
    "EVENT_VENUE":  {"label": "Espaço de Eventos", "emoji": "🎪", "color": "#6366F1"},
    "LIVE_MUSIC":   {"label": "Música ao Vivo",    "emoji": "🎵", "color": "#EC4899"},
    "CASINO":       {"label": "Cassino",           "emoji": "🎰", "color": "#EAB308"},
    "ENTERTAINMENT":{"label": "Entretenimento",    "emoji": "🎮", "color": "#8B5CF6"},
    "PARK":         {"label": "Ao Ar Livre",       "emoji": "🌳", "color": "#16A34A"},
    "OTHER":        {"label": "Outro",             "emoji": "📍", "color": "#6B7280"},
}

# ── Granular type → PT-BR label (for detail pages) ──────────────────────────
# Shown as subtitle: "Restaurante · Japonês" or "Bar · Irish Pub"
GRANULAR_LABELS = {
    # Bars
    "bar":                      "Bar",
    "bar_and_grill":            "Bar & Grill",
    "snack_bar":                "Petiscaria",
    "irish_pub":                "Pub Irlandês",
    "pub":                      "Pub",
    "cocktail_bar":             "Bar de Coquetéis",
    "wine_bar":                 "Bar de Vinhos",
    # Nightlife
    "night_club":               "Casa Noturna",
    "karaoke":                  "Karaokê",
    # Brewery/wine
    "brewery":                  "Cervejaria",
    "winery":                   "Vinícola",
    # Coffee
    "coffee_shop":              "Cafeteria",
    "cafe":                     "Café",
    # Bakery
    "bakery":                   "Padaria",
    # Restaurants
    "restaurant":               "Restaurante",
    "brazilian_restaurant":     "Restaurante Brasileiro",
    "buffet_restaurant":        "Buffet",
    "portuguese_restaurant":    "🇵🇹 Português",
    "argentinian_restaurant":   "🇦🇷 Argentino",
    "family_restaurant":        "Restaurante Familiar",
    "seafood_restaurant":       "🦞 Frutos do Mar",
    "steak_restaurant":         "🥩 Churrascaria",
    "pizza_restaurant":         "🍕 Pizzaria",
    "italian_restaurant":       "🇮🇹 Italiano",
    "japanese_restaurant":      "🇯🇵 Japonês",
    "chinese_restaurant":       "🇨🇳 Chinês",
    "mexican_restaurant":       "🇲🇽 Mexicano",
    "asian_restaurant":         "🌏 Asiático",
    "mediterranean_restaurant": "🫒 Mediterrâneo",
    "french_restaurant":        "🇫🇷 Francês",
    "indian_restaurant":        "🇮🇳 Indiano",
    "thai_restaurant":          "🇹🇭 Tailandês",
    "turkish_restaurant":       "🇹🇷 Turco",
    "korean_restaurant":        "🇰🇷 Coreano",
    "vegetarian_restaurant":    "🥗 Vegetariano",
    "vegan_restaurant":         "🌱 Vegano",
    "ramen_restaurant":         "🇯🇵 Ramen",
    "sushi_restaurant":         "🇯🇵 Sushi",
    "hamburger_restaurant":     "🍔 Hamburgueria",
    "barbecue_restaurant":      "🔥 Churrasco",
    # Food & drink
    "bistro":                   "Bistrô",
    "deli":                     "Delicatessen",
    "cafeteria":                "Lanchonete",
    "food_court":               "Praça de Alimentação",
    "acai_shop":                "Açaiteria",
    "salad_shop":               "Saladas",
    "ice_cream_shop":           "Sorveteria",
    "pastry_shop":              "Confeitaria",
    "confectionery":            "Doces",
    "juice_shop":               "Casa de Sucos",
    # Events & entertainment
    "event_venue":              "Espaço de Eventos",
    "performing_arts_theater":  "Teatro",
    "cultural_center":          "Centro Cultural",
    "video_arcade":             "Fliperama",
    # Casino
    "casino":                   "Cassino",
    # Parks / open-air
    "plaza":                    "Praça",
    "city_park":                "Parque Urbano",
    "park":                     "Parque",
    "historical_landmark":      "Marco Histórico",
}

# ── Google Places type → VibeSense category ──────────────────────────────────
_GOOGLE_TO_CATEGORY = {
    # Bars
    "bar":                      "BAR",
    "bar_and_grill":            "BAR",
    "snack_bar":                "BAR",
    "irish_pub":                "PUB",
    "pub":                      "PUB",
    "cocktail_bar":             "COCKTAIL_BAR",
    "wine_bar":                 "COCKTAIL_BAR",
    # Nightlife
    "night_club":               "NIGHTCLUB",
    "karaoke":                  "KARAOKE",
    # Brewery/wine
    "brewery":                  "BREWERY",
    "winery":                   "WINERY",
    # Coffee
    "coffee_shop":              "COFFEE_SHOP",
    "cafe":                     "COFFEE_SHOP",
    # Bakery
    "bakery":                   "BAKERY",
    # Restaurants
    "restaurant":               "RESTAURANT",
    "brazilian_restaurant":     "RESTAURANT",
    "portuguese_restaurant":    "RESTAURANT",
    "argentinian_restaurant":   "RESTAURANT",
    "family_restaurant":        "RESTAURANT",
    "seafood_restaurant":       "RESTAURANT",
    "steak_restaurant":         "RESTAURANT",
    "pizza_restaurant":         "RESTAURANT",
    "italian_restaurant":       "RESTAURANT",
    "japanese_restaurant":      "RESTAURANT",
    "chinese_restaurant":       "RESTAURANT",
    "mexican_restaurant":       "RESTAURANT",
    "asian_restaurant":         "RESTAURANT",
    "mediterranean_restaurant": "RESTAURANT",
    "french_restaurant":        "RESTAURANT",
    "indian_restaurant":        "RESTAURANT",
    "thai_restaurant":          "RESTAURANT",
    "turkish_restaurant":       "RESTAURANT",
    "korean_restaurant":        "RESTAURANT",
    "vegetarian_restaurant":    "RESTAURANT",
    "vegan_restaurant":         "RESTAURANT",
    "ramen_restaurant":         "RESTAURANT",
    "sushi_restaurant":         "RESTAURANT",
    "hamburger_restaurant":     "RESTAURANT",
    "barbecue_restaurant":      "RESTAURANT",
    # Buffet (own category)
    "buffet_restaurant":        "BUFFET",
    # Food & drink
    "bistro":                   "FOOD_DRINK",
    "deli":                     "FOOD_DRINK",
    "cafeteria":                "FOOD_DRINK",
    "food_court":               "FOOD_DRINK",
    "acai_shop":                "FOOD_DRINK",
    "salad_shop":               "FOOD_DRINK",
    "ice_cream_shop":           "FOOD_DRINK",
    "pastry_shop":              "FOOD_DRINK",
    "confectionery":            "FOOD_DRINK",
    "juice_shop":               "FOOD_DRINK",
    # Events & entertainment
    "event_venue":              "EVENT_VENUE",
    "performing_arts_theater":  "LIVE_MUSIC",
    "cultural_center":          "LIVE_MUSIC",
    "video_arcade":             "ENTERTAINMENT",
    # Casino
    "casino":                   "CASINO",
    # Parks / open-air (garden, national_park intentionally NOT mapped — stay OTHER)
    "park":                     "PARK",
    "plaza":                    "PARK",
    "city_park":                "PARK",
    "historical_landmark":      "PARK",
}

# ── BestTime type → VibeSense category (fallback) ───────────────────────────
_BESTTIME_TO_CATEGORY = {
    "BAR":              "BAR",
    "BEER":             "BAR",
    "CLUBS":            "NIGHTCLUB",
    "BREWERY":          "BREWERY",
    "CONCERT_HALL":     "LIVE_MUSIC",
    "EVENT_VENUE":      "EVENT_VENUE",
    "PERFORMING_ARTS":  "LIVE_MUSIC",
    "ARTS":             "ENTERTAINMENT",
    "WINERY":           "WINERY",
    "CASINO":           "CASINO",
    "FOOD_AND_DRINK":   "FOOD_DRINK",
    "BISTRO":           "FOOD_DRINK",
    "RESTAURANT":       "RESTAURANT",
    "CAFE":             "COFFEE_SHOP",
    "PARK":             "PARK",
    "PLAZA":            "PARK",
    "CITY_PARK":        "PARK",
}


def resolve_category(
    google_type: str = None,
    besttime_type: str = None,
    venue_name: str = None,
    google_map: dict = None,
    besttime_map: dict = None,
) -> str:
    """Resolve the VibeSense display category for a venue.

    Priority: google_type > besttime_type > name heuristics > OTHER

    ``google_map`` / ``besttime_map`` default to the hardcoded module dicts; the
    serve path injects the admin-effective maps from ``load_category_map`` so an
    override applies without a code change.

    Special rules:
    - If Google says "restaurant" but BestTime says BAR/BEER → keep as BAR
      (many bars that serve food get classified as restaurant by Google)
    - If Google says "night_club" but name contains "warehouse/espaço/centro"
      → EVENT_VENUE (event spaces, not regular nightclubs)
    """
    gmap = google_map if google_map is not None else _GOOGLE_TO_CATEGORY
    bmap = besttime_map if besttime_map is not None else _BESTTIME_TO_CATEGORY
    google_cat = gmap.get(google_type.lower()) if google_type else None
    besttime_cat = bmap.get(besttime_type.upper()) if besttime_type else None

    # Rule: Google=restaurant but BestTime=BAR → trust BestTime (it's a bar that serves food)
    if google_cat == "RESTAURANT" and besttime_cat in ("BAR", "PUB"):
        return besttime_cat

    if google_cat:
        return google_cat
    if besttime_cat:
        return besttime_cat
    return "OTHER"


def get_category_info(category: str) -> dict:
    """Get display info (label, emoji, color) for a category."""
    return CATEGORIES.get(category, CATEGORIES["OTHER"])


def get_granular_label(granular_type: str) -> str:
    """Get PT-BR label for a granular type (for detail pages)."""
    if not granular_type:
        return ""
    return GRANULAR_LABELS.get(granular_type.lower(), "")


def resolve_venue_display(
    google_type: str = None,
    besttime_type: str = None,
    venue_name: str = None,
    google_map: dict = None,
    besttime_map: dict = None,
) -> dict:
    """Full resolution: returns category + granular_type + granular_label + label + emoji + color."""
    cat = resolve_category(
        google_type,
        besttime_type,
        venue_name,
        google_map=google_map,
        besttime_map=besttime_map,
    )
    info = get_category_info(cat)
    granular = google_type or (besttime_type.lower() if besttime_type else None)
    return {
        "category": cat,
        "granular_type": granular,
        "granular_label": get_granular_label(granular) if granular else "",
        **info,
    }


# ── admin-configurable mapping (Redis override merged over defaults) ──────────
def load_category_map(redis_like) -> tuple[dict, dict]:
    """Return the effective ``(google_map, besttime_map)`` for category resolution.

    Reads the admin override from Redis key ``admin_config:venue_category_map``
    and merges it over the hardcoded defaults. Fails open to the defaults on any
    error, a missing key, malformed JSON, or a bad shape — category resolution
    must never break on bad config. Individual malformed entries are skipped.
    """
    google_map = dict(_GOOGLE_TO_CATEGORY)
    besttime_map = dict(_BESTTIME_TO_CATEGORY)
    if redis_like is None:
        return google_map, besttime_map
    try:
        raw = redis_like.get(ADMIN_CONFIG_CATEGORY_MAP_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[category_map] read failed (%s); using defaults", e)
        return google_map, besttime_map
    if raw is None:
        return google_map, besttime_map
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.debug("[category_map] invalid JSON (%s); using defaults", e)
        return google_map, besttime_map
    if not isinstance(data, dict):
        logger.debug("[category_map] not an object; using defaults")
        return google_map, besttime_map
    google_override = data.get("google")
    if isinstance(google_override, dict):
        for k, v in google_override.items():
            if isinstance(k, str) and isinstance(v, str):
                google_map[k.lower()] = v
    besttime_override = data.get("besttime")
    if isinstance(besttime_override, dict):
        for k, v in besttime_override.items():
            if isinstance(k, str) and isinstance(v, str):
                besttime_map[k.upper()] = v
    return google_map, besttime_map


def effective_category_map(redis_like) -> dict:
    """The effective map as the admin API document ``{"google", "besttime"}``."""
    google_map, besttime_map = load_category_map(redis_like)
    return {"google": google_map, "besttime": besttime_map}


def validate_category_map_config(value) -> dict:
    """Validate + normalize an admin category-map override before persistence.

    Body shape: ``{"google": {type: CATEGORY}, "besttime": {type: CATEGORY}}``.
    Every category value must be a known ``CATEGORIES`` key (else ValueError).
    Google keys are normalized to lowercase, BestTime keys to uppercase. Returns
    the normalized dict actually persisted. Extra top-level keys are ignored.
    """
    if not isinstance(value, dict):
        raise TypeError("category map must be a JSON object")
    result: dict[str, dict] = {"google": {}, "besttime": {}}
    for side, normalize in (("google", str.lower), ("besttime", str.upper)):
        submap = value.get(side)
        if submap is None:
            continue
        if not isinstance(submap, dict):
            raise TypeError(f"'{side}' must be a JSON object")
        for raw_type, category in submap.items():
            if not isinstance(raw_type, str) or not isinstance(category, str):
                raise TypeError(f"'{side}' entries must map a string type to a string category")
            if category not in CATEGORIES:
                raise ValueError(
                    f"unknown category '{category}' for {side} type '{raw_type}'"
                )
            key = normalize(raw_type.strip())
            if key:
                result[side][key] = category
    return result
