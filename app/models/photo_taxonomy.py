"""Fixed vocabulary for per-photo classification.

Sibling of `taxonomy.py`, which describes a VENUE. This describes one
PHOTOGRAPH: which of six buckets it belongs in, and the attributes that bucket
can carry.

The organising rule: **wherever a photo can answer a question `taxonomy.py`
already asks, this emits that taxonomy's own labels** rather than a parallel
vocabulary that would have to be translated later. Those attributes carry a
`taxonomy_key` and are validated by `validate_category_labels`, so the
vocabulary lives in exactly one place and a taxonomy edit cannot desync the two.
Everything else is a photo-native fact the venue taxonomy has no home for —
whether a menu is legible, whether there are children, whether a terrace has a
roof.

Cardinality follows one rule: **one value when a photo can only be one thing, an
array when it can genuinely show several.** An array extends without a
migration; a boolean does not, which is why `dress_code` and `seating_type` are
lists and not a spray of flags.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.models.taxonomy import TAXONOMY, validate_category_labels

CATEGORY_MENU = "menu"
CATEGORY_FOOD_DRINKS = "food_drinks"
CATEGORY_INTERIOR = "interior"
CATEGORY_EXTERIOR = "exterior"
CATEGORY_CROWD = "crowd"
CATEGORY_OTHER = "other"

PHOTO_CATEGORIES: tuple[str, ...] = (
    CATEGORY_MENU, CATEGORY_FOOD_DRINKS, CATEGORY_INTERIOR,
    CATEGORY_EXTERIOR, CATEGORY_CROWD, CATEGORY_OTHER,
)

# What separates the two spatial categories. Stated as a rule about the SKY
# rather than about the subject, because "is there open air overhead" is
# something a vision model answers reliably and "is this outdoorsy" is not.
CATEGORY_RULES: dict[str, str] = {
    CATEGORY_MENU: "the subject is a menu, cardápio, price board or drinks list",
    CATEGORY_FOOD_DRINKS: "the subject is a dish, a drink, or a laid table",
    CATEGORY_INTERIOR: "an enclosed space of the venue — there is a ROOF overhead",
    CATEGORY_EXTERIOR: (
        "open air — the SKY is visible. Facade, terrace, rooftop, quintal, "
        "calçada, beira-mar. A covered varanda is interior, not exterior"
    ),
    CATEGORY_CROWD: (
        "people are the SUBJECT — you can read who is there. People at the "
        "edges of a room shot do not make it a crowd photo"
    ),
    CATEGORY_OTHER: "none of the above",
}

# Photo quality. Shared across every category because the question it answers —
# is this good enough to show in the app — is asked of every photo.
QUALITY_VALUES: tuple[str, ...] = ("boa", "escura", "borrada", "baixa_resolucao")

TIME_OF_DAY_VALUES: tuple[str, ...] = ("dia", "entardecer", "noite")

SEATING_VALUES: tuple[str, ...] = (
    "mesas", "banquetas_balcao", "sofas", "mesas_altas", "bancos_comunitarios",
    "em_pe", "puffs",
)

# The provider's authorship is a fact; this is a guess, and is only ever written
# where the provider had no answer. Kept under a different name so the two can
# never be confused.
AUTHORSHIP_GUESS_VALUES: tuple[str, ...] = ("by_owner", "by_visitor")


@dataclass(frozen=True)
class Attr:
    """One attribute a category can carry.

    `taxonomy_key` defers the allowed values to `app/models/taxonomy.py`, which
    is what keeps the photo vocabulary and the venue vocabulary from drifting.
    """

    name: str
    many: bool = False
    values: tuple[str, ...] = ()
    taxonomy_key: Optional[str] = None
    boolean: bool = False
    why: str = ""

    def allowed(self) -> tuple[str, ...]:
        if self.taxonomy_key:
            return tuple(TAXONOMY.get(self.taxonomy_key, ()))
        return self.values


# ── the people block ─────────────────────────────────────────────────────────
# Extracted from ANY photo with visible people, not only from photos filed as
# `crowd`. Otherwise every person in every interior shot is discarded — and
# `has_kids`, the one signal the `familia` mode has no other source for, is the
# field most likely to turn up in a photo filed as something else.
PEOPLE_ATTRIBUTES: tuple[Attr, ...] = (
    Attr("crowd_level", values=("vazio", "poucas_pessoas", "movimentado", "cheio", "lotado"),
         why="corroborates busyness from a second, independent source"),
    Attr("publico", many=True, taxonomy_key="publico",
         why="the age-range and scene read, in the vocabulary the product speaks"),
    Attr("has_kids", boolean=True,
         why="the familia mode's only photo evidence — a fact, not an impression"),
    Attr("dress_code", many=True, taxonomy_key="dress_code",
         why="the generic dress read"),
    Attr("dress_scene", many=True,
         values=("rock_metal", "rap_hip_hop", "funk_baile", "sertanejo",
                 "alternativo_indie", "queer", "praia", "esportivo", "fantasia_tematico"),
         why="the subculture read, which dress_code is too coarse for"),
    Attr("group_type", many=True,
         values=("casais", "amigos", "familias", "sozinhos", "grupo_grande", "turistas"),
         why="casais -> date; grupo_grande -> resenha"),
    Attr("activity", many=True,
         values=("dancando", "bebendo", "comendo", "conversando", "assistindo_show",
                 "assistindo_jogo", "karaoke", "fila"),
         why="dancando is the best role_agitado signal; fila means the place is hot"),
    Attr("clima_social", taxonomy_key="clima_social",
         why="straight into the venue vibe profile"),
    Attr("time_of_day", values=TIME_OF_DAY_VALUES,
         why="a Tuesday-afternoon crowd is not a Saturday-night crowd"),
)

# Deliberately absent: race, ethnicity, and individual gender. The model is
# unreliable at it, no product question needs it, and profiling crowds by those
# attributes is a line this system does not cross. `publico` and `dress_scene`
# read the SCENE — what a place is, from how people chose to present — never a
# claim about an individual in the frame. Also absent: cleanliness and
# attractiveness, which are subjective and defamatory about a real business.

PHOTO_ATTRIBUTES: dict[str, tuple[Attr, ...]] = {
    CATEGORY_MENU: (
        Attr("legible", values=("sim", "parcial", "nao"),
             why="gates menu extraction — stop paying OCR for unreadable menus"),
        Attr("medium", values=("impresso", "lousa", "placa_parede", "tela_digital",
                               "qr_code", "livreto"),
             why="a QR-code photo has no text to extract"),
        Attr("page_side", values=("frente", "verso", "ambos", "pagina_interna"),
             why="so extraction knows whether it has the whole menu"),
        Attr("content_scope", values=("so_comida", "so_bebida", "ambos"),
             why="a drinks-only board is a bar signal"),
        Attr("sections", many=True,
             values=("entradas", "petiscos", "principais", "sobremesas", "cervejas",
                     "drinks", "vinhos", "cafe", "combos", "infantil"),
             why="an `infantil` section is direct familia evidence"),
        Attr("has_prices", boolean=True, why="gates the price-tier pipeline"),
        Attr("is_promo", boolean=True, why="happy hour, rodízio, chopp em dobro"),
        Attr("language", values=("pt", "en", "es", "multi"),
             why="a bilingual menu means tourist-facing"),
    ),
    CATEGORY_FOOD_DRINKS: (
        Attr("subject", values=("comida", "bebida", "ambos")),
        Attr("dish_type", many=True,
             values=("petisco", "porcao", "prato_individual", "sanduiche", "pizza",
                     "frutos_do_mar", "carne_churrasco", "massa", "japonesa",
                     "sobremesa", "regional", "veg")),
        Attr("drink_type", many=True,
             values=("cerveja_chopp", "coquetel", "caipirinha", "vinho", "destilado",
                     "sem_alcool", "cafe", "balde_combo")),
        Attr("portion_size", values=("individual", "para_dividir", "combo_balde"),
             why="sharing plates -> intencao: Sentar com a galera"),
        Attr("plating", values=("simples", "caprichado", "autoral", "embalado"),
             why="autoral -> the jantar mode"),
        Attr("setting", values=("mesa_posta", "balcao", "close_up", "com_pessoas"),
             why="a laid table means table service, not a counter"),
        Attr("dietary_labels", many=True,
             values=("vegetariano", "vegano", "sem_gluten", "infantil"),
             why="only when visibly labelled"),
    ),
    CATEGORY_INTERIOR: (
        Attr("space_type",
             values=("salao", "balcao_bar", "pista_danca", "palco", "lounge",
                     "sala_jantar", "entrada", "mezanino_vip", "cozinha_aberta",
                     "banheiro"),
             why="pista_danca is the strongest role_agitado signal available"),
        Attr("estetica", many=True, taxonomy_key="estetica"),
        Attr("lighting",
             values=("natural", "quente_baixa", "neon_colorida", "escura_balada",
                     "fluorescente"),
             why="quente_baixa is what date and clima_social: Intimista are made of"),
        Attr("seating_type", many=True, values=SEATING_VALUES,
             why="em_pe ~ balada; bancos_comunitarios ~ resenha"),
        Attr("music_format", many=True, taxonomy_key="music_format",
             why="a stage, a DJ booth, a karaoke screen — music_format has no "
                 "photo evidence today"),
        Attr("screens", values=("telao", "tvs", "nenhuma"),
             why="'tem telão pro jogo?' is one of the most-asked questions "
                 "about a Brazilian bar"),
        Attr("capacity", values=("intimo", "medio", "amplo", "multiplos_ambientes")),
    ),
    CATEGORY_EXTERIOR: (
        Attr("exterior_kind",
             values=("fachada", "area_externa", "rooftop", "quintal_jardim",
                     "calcada", "pe_na_areia", "piscina", "estacionamento"),
             why="separates 'how do I find the door' from 'is there an open-air area'"),
        Attr("covered", values=("descoberto", "parcial", "coberto"),
             why="the rain question, which nothing else answers"),
        Attr("view", many=True,
             values=("mar", "cidade", "natureza", "rio", "rua", "sem_vista")),
        Attr("estetica", many=True, taxonomy_key="estetica"),
        Attr("seating_type", many=True, values=SEATING_VALUES),
        Attr("venue_name_legible", boolean=True,
             why="confirms the right venue, and is the photo to show for "
                 "'how do I find it'"),
        Attr("time_of_day", values=TIME_OF_DAY_VALUES),
    ),
    CATEGORY_CROWD: PEOPLE_ATTRIBUTES,
    CATEGORY_OTHER: (
        Attr("other_kind",
             values=("logo_arte", "flyer_evento", "documento_aviso", "pessoa_isolada",
                     "irrelevante", "ilegivel"),
             why="knowing WHY it is `other` lets us reclassify later without "
                 "re-billing. flyer_evento is the likeliest future promotion: a "
                 "flyer carries the event, the DJ, the date and the cover"),
    ),
}


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _validate_one(spec: Attr, value: Any) -> Any:
    """One attribute, or None when the model returned something unusable."""
    if value is None:
        return None
    if spec.boolean:
        return _coerce_bool(value)

    if spec.many:
        # A scalar for an array field is tolerated and wrapped: the model
        # returning "mesas" instead of ["mesas"] is a formatting slip, not a
        # different answer.
        items = value if isinstance(value, (list, tuple)) else [value]
        raw = [str(v) for v in items]
        if spec.taxonomy_key:
            kept = validate_category_labels(spec.taxonomy_key, raw)
        else:
            kept = [v for v in raw if v in spec.allowed()]
        return kept or None

    # A single-valued field given a list is NOT tolerated: it means the model
    # answered a different question than the one asked, and picking one of the
    # answers would invent a fact.
    if isinstance(value, (list, tuple, dict)):
        return None
    text = str(value)
    return text if text in spec.allowed() else None


def _validate_against(specs: tuple[Attr, ...], raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for spec in specs:
        validated = _validate_one(spec, raw.get(spec.name))
        if validated is not None:
            out[spec.name] = validated
    return out


def validate_attributes(category: str, raw: Any) -> dict:
    """Keep only the attributes that belong to this category and are in vocabulary.

    An unknown field, an invented label, or a value of the wrong shape is
    DROPPED rather than stored — the rest of the verdict survives. A wrong label
    is worse than a missing one, because everything downstream will trust it.
    """
    return _validate_against(PHOTO_ATTRIBUTES.get(category, ()), raw)


def validate_people(raw: Any) -> dict:
    """The people block, valid for a photo of any category."""
    return _validate_against(PEOPLE_ATTRIBUTES, raw)


def validate_quality(value: Any) -> Optional[str]:
    text = str(value or "")
    return text if text in QUALITY_VALUES else None


def validate_authorship_guess(value: Any) -> Optional[str]:
    text = str(value or "")
    return text if text in AUTHORSHIP_GUESS_VALUES else None


def _describe(spec: Attr) -> str:
    if spec.boolean:
        shape = "true or false"
    elif spec.many:
        shape = "array of any of: " + ", ".join(spec.allowed())
    else:
        shape = "exactly one of: " + ", ".join(spec.allowed())
    return f"- {spec.name}: {shape}"


def describe_attributes(category: str) -> str:
    """The attribute schema as prompt text.

    Generated from the specs rather than written out again, so a vocabulary
    change cannot leave the prompt describing labels that no longer validate.
    """
    lines = [_describe(spec) for spec in PHOTO_ATTRIBUTES.get(category, ())]
    if category != CATEGORY_CROWD:
        lines.append("")
        lines.append("Also, ONLY if people are visible in the photo, a `people` object:")
        lines.extend(_describe(spec) for spec in PEOPLE_ATTRIBUTES)
    return "\n".join(lines)


def describe_other_kind() -> str:
    """The `other_kind` line, for the CATEGORY prompt rather than the attribute one.

    It belongs to pass 1 because pass 2 skips `other` entirely: this is the only
    chance to record WHY a photo could not be categorized, and the model has
    already looked at the image, so asking costs nothing.
    """
    spec = PHOTO_ATTRIBUTES[CATEGORY_OTHER][0]
    return (
        f"- {spec.name} (ONLY when category is `{CATEGORY_OTHER}`): "
        f"exactly one of: {', '.join(spec.allowed())}"
    )


def describe_categories() -> str:
    return "\n".join(f"- {name}: {rule}" for name, rule in CATEGORY_RULES.items())
