"""VibeSense venue category mapping.

Maps Google Places granular types to VibeSense display categories.
Two levels of type info are served to the app:
  - category: display-level grouping (BAR, NIGHTCLUB, RESTAURANT, etc.)
  - granular_type: specific type for detail pages ("japanese_restaurant", "irish_pub")

Priority: google_primary_type > besttime venue_type > "OTHER"
"""

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
    "RESTAURANT":   {"label": "Restaurante",       "emoji": "🍽️", "color": "#DC2626"},
    "BUFFET":       {"label": "Buffet",            "emoji": "🍱", "color": "#DC2626"},
    "FOOD_DRINK":   {"label": "Gastronomia",       "emoji": "🍴", "color": "#10B981"},
    "EVENT_VENUE":  {"label": "Espaço de Eventos", "emoji": "🎪", "color": "#6366F1"},
    "LIVE_MUSIC":   {"label": "Música ao Vivo",    "emoji": "🎵", "color": "#EC4899"},
    "CASINO":       {"label": "Cassino",           "emoji": "🎰", "color": "#EAB308"},
    "ENTERTAINMENT":{"label": "Entretenimento",    "emoji": "🎮", "color": "#8B5CF6"},
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
}


def resolve_category(
    google_type: str = None,
    besttime_type: str = None,
    venue_name: str = None,
) -> str:
    """Resolve the VibeSense display category for a venue.

    Priority: google_type > besttime_type > name heuristics > OTHER

    Special rules:
    - If Google says "restaurant" but BestTime says BAR/BEER → keep as BAR
      (many bars that serve food get classified as restaurant by Google)
    - If Google says "night_club" but name contains "warehouse/espaço/centro"
      → EVENT_VENUE (event spaces, not regular nightclubs)
    """
    google_cat = _GOOGLE_TO_CATEGORY.get(google_type.lower()) if google_type else None
    besttime_cat = _BESTTIME_TO_CATEGORY.get(besttime_type.upper()) if besttime_type else None

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


def resolve_venue_display(google_type: str = None, besttime_type: str = None, venue_name: str = None) -> dict:
    """Full resolution: returns category + granular_type + granular_label + label + emoji + color."""
    cat = resolve_category(google_type, besttime_type, venue_name)
    info = get_category_info(cat)
    granular = google_type or (besttime_type.lower() if besttime_type else None)
    return {
        "category": cat,
        "granular_type": granular,
        "granular_label": get_granular_label(granular) if granular else "",
        **info,
    }
