"""Fixed taxonomy vocabulary for VibeSense venue vibe classification.

All valid labels per category. GPT output is validated against these
to ensure only known labels appear in the vibe profile.
"""

TAXONOMY: dict[str, list[str]] = {
    "publico": [
        "Turistas", "Alternativo", "Gótico", "LGBTQ+", "Casais",
        "Galera 50+", "Galera 30+", "Galera jovem", "Família",
        "Artistas / criativos", "Público misto",
    ],
    "musica": [
        "Pagode", "Samba", "Sertanejo", "Funk", "Eletrônica", "Techno",
        "House", "Pop", "Rock", "Indie", "Rap / Trap", "MPB", "Reggaeton",
        "Forró", "Jazz", "Música ambiente", "Brega", "Frevo",
    ],
    "music_format": [
        "DJ", "Som ao vivo", "Banda ao vivo", "Roda de samba", "Karaokê",
        "Playlist ambiente", "Open mic", "Instrumental",
    ],
    "estilo_do_lugar": [
        "Boteco raiz", "Gastrobar", "Bar tradicional", "Lounge", "Balada",
        "Club", "Pub", "Rooftop", "Pé na areia", "Beach club", "Wine bar",
        "Coquetelaria", "Bar com jogos", "Speakeasy", "Cultural / alternativo",
        "Inferninho",
    ],
    "estetica": [
        "Instagramável", "Minimalista", "Retrô", "Underground", "Neon",
        "Intimista", "Sofisticado", "Moderno", "Rústico", "Ao ar livre",
        "Vista bonita", "Beira-mar", "Nature vibe",
    ],
    "intencao": [
        "Pra dançar", "Clima de date", "Sentar com a galera", "Aniversário",
        "Comemoração", "Jantar tranquilo", "Virar a noite",
        "Conhecer gente nova", "Beber de leve", "Happy hour", "After",
    ],
    "dress_code": [
        "Casual", "Arrumadinho", "Esporte fino", "Praia", "Alternativo",
        "Sem dress code",
    ],
    "clima_social": [
        "Intimista", "Social", "Animado", "Agitado", "Fervendo", "Tranquilo",
    ],
}

# All category keys
TAXONOMY_CATEGORIES = list(TAXONOMY.keys())

# Union of all valid labels across all categories
ALL_VALID_LABELS: set[str] = set()
for _labels in TAXONOMY.values():
    ALL_VALID_LABELS.update(_labels)


def validate_category_labels(category_key: str, labels: list[str]) -> list[str]:
    """Filter out any labels not in the fixed taxonomy for a given category."""
    valid = set(TAXONOMY.get(category_key, []))
    return [label for label in labels if label in valid]


def validate_top_vibes(top_vibes: list[str]) -> list[str]:
    """Filter out any top_vibes labels not in any taxonomy category."""
    return [label for label in top_vibes if label in ALL_VALID_LABELS]
