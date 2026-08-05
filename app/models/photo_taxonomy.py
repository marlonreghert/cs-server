"""Fixed vocabulary for per-photo classification.

Sibling of `taxonomy.py`, which describes a VENUE in the product's own pt-BR
vocabulary. This describes one PHOTOGRAPH, and deliberately does **not** speak
that vocabulary: it is a coarse, generic, English classification of what is in
the frame. A venue-level opinion is somebody else's job.

Three rules shape every entry below:

1. **Coarse beats precise.** A model reading a thumbnail can tell beer from a
   cocktail; it cannot reliably tell a caipirinha from a batida, and a label it
   gets wrong is worse than one it never emitted, because everything downstream
   will trust it. Every list is short on purpose.
2. **Every attribute can say `not_classified` or `not_applicable`.** Both are
   real answers and both are stored rather than omitted, so an unknown is
   visible instead of looking like a field nobody asked about. They are kept
   apart because they mean opposite things about the photo: a dessert photo has
   no drink in it (`not_applicable` — the question does not arise), which is not
   the same as a dim bar shot where a glass might be a beer or a caipirinha
   (`not_classified` — the question arises and could not be answered). Counting
   the second as coverage would understate the model; counting the first as a
   failure would slander it.
3. **An attribute is only written when the model is confident about THAT
   attribute.** Confidence is per-attribute, not per-photo: a model can be
   certain a room is a bar and unsure whether it has screens, and those two
   facts must not share a fate. Below the floor, the value becomes
   `not_classified`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# "Asked, could not tell." Appended to every attribute's vocabulary, and what a
# low-confidence, unparseable, or unlisted answer collapses to.
NOT_CLASSIFIED = "not_classified"

# "The question does not arise for this photo" — there is no drink in a photo of
# a dessert. Distinct from `not_classified` because it is a fact about the
# photo, not a limit of the model, and lumping the two together makes an
# accurate classifier look like a failing one.
NOT_APPLICABLE = "not_applicable"

# Appended to every attribute, so the model always has an honest way out.
UNIVERSAL_VALUES: tuple[str, ...] = (NOT_APPLICABLE, NOT_CLASSIFIED)

CATEGORY_MENU = "menu"
CATEGORY_FOOD_DRINKS = "food_drinks"
CATEGORY_INTERIOR = "interior"
CATEGORY_EXTERIOR = "exterior"
CATEGORY_CROWD = "crowd"
CATEGORY_OTHER = "other"
# A promotional poster or flyer — mostly graphic design and text rather than a
# photograph. Instagram archives are the first source to feed this classifier
# images that are not photographs at all; without this category every event
# flyer landed in `other`, indistinguishable from a logo or a document. It is
# the free pre-filter the event extractor spends its vision calls behind.
CATEGORY_FLYER = "flyer"

PHOTO_CATEGORIES: tuple[str, ...] = (
    CATEGORY_MENU, CATEGORY_FOOD_DRINKS, CATEGORY_INTERIOR,
    CATEGORY_EXTERIOR, CATEGORY_CROWD, CATEGORY_OTHER, CATEGORY_FLYER,
)

# What separates the two spatial categories. Stated as a rule about the SKY
# rather than about the subject, because "is there open air overhead" is
# something a vision model answers reliably and "is this outdoorsy" is not.
CATEGORY_RULES: dict[str, str] = {
    CATEGORY_MENU: "the subject is a menu, price board or drinks list",
    CATEGORY_FOOD_DRINKS: "the subject is a dish, a drink, or a laid table",
    CATEGORY_INTERIOR: "an enclosed space of the venue — there is a ROOF overhead",
    CATEGORY_EXTERIOR: (
        "open air — the SKY is visible. Facade, terrace, rooftop, garden, "
        "sidewalk, beachfront. A covered veranda is interior, not exterior"
    ),
    CATEGORY_CROWD: (
        "people are the SUBJECT — you can read who is there. People at the "
        "edges of a room shot do not make it a crowd photo"
    ),
    CATEGORY_OTHER: "none of the above",
    CATEGORY_FLYER: (
        "the subject is a promotional poster, flyer or event announcement — "
        "the image is mostly graphic design and text rather than a photograph"
    ),
}

# Is this good enough to show in the app. Two buckets, because the difference
# between "dark" and "blurry" changes nothing anyone does with the photo.
QUALITY_VALUES: tuple[str, ...] = ("good", "poor", NOT_CLASSIFIED)

# The provider's authorship is a fact; this is a guess, and is only ever written
# where the provider had no answer. Kept under a different name so the two can
# never be confused — and confidence-gated like every other guess, because on a
# real run it came back `by_visitor` for 20 photos out of 20. A field with one
# possible answer is not a signal, it is a constant with a story attached, and
# storing it on every photo would be storing noise.
AUTHORSHIP_GUESS_VALUES: tuple[str, ...] = ("by_owner", "by_visitor")


@dataclass(frozen=True)
class Attr:
    """One attribute a category can carry.

    Single-valued, always. An array invites the model to hedge by listing
    everything plausible, which is precisely the imprecision this schema exists
    to avoid — and a yes/no question is an enum here rather than a bool so that
    "could not tell" is sayable.
    """

    name: str
    values: tuple[str, ...] = ()
    why: str = ""

    def allowed(self) -> tuple[str, ...]:
        return self.values + UNIVERSAL_VALUES


# Asked only where a photo can answer it. It was briefly asked of every photo,
# which put it on menu close-ups and plated dishes that show nothing of the
# outside world — measured at 5/20 answered on a real run, by far the worst
# field in the schema, because most of the time the question was unanswerable
# rather than hard.
TIME_OF_DAY = Attr(
    "time_of_day", values=("day", "night"),
    why="a daytime venue and a nighttime venue are different venues",
)

# ── the people block ─────────────────────────────────────────────────────────
# Extracted from ANY photo with visible people, not only from photos filed as
# `crowd`. Otherwise every person in every interior shot is discarded — and
# `has_kids`, the one signal the `familia` mode has no other source for, is the
# field most likely to turn up in a photo filed as something else.
#
# Deliberately absent: race, ethnicity, individual gender, dress, age, and
# anything else that profiles the people in the frame rather than describing
# the place. The model is unreliable at it, no product question needs it, and a
# venue recommender that reads crowds that way is a line this system does not
# cross.
PEOPLE_ATTRIBUTES: tuple[Attr, ...] = (
    Attr("crowd_level", values=("empty", "some", "busy", "packed"),
         why="corroborates busyness from a second, independent source"),
    Attr("has_kids", values=("yes", "no"),
         why="the familia mode's only photo evidence"),
    Attr("group_type", values=("couples", "friends", "families", "mixed"),
         why="couples -> date; friends -> resenha"),
    Attr("activity", values=("dancing", "eating_drinking", "watching", "other"),
         why="dancing is the best role_agitado signal available"),
    TIME_OF_DAY,
)

PHOTO_ATTRIBUTES: dict[str, tuple[Attr, ...]] = {
    CATEGORY_MENU: (
        Attr("legible", values=("yes", "partial", "no"),
             why="gates menu extraction — stop paying OCR for unreadable menus"),
        Attr("has_prices", values=("yes", "no"),
             why="gates the price-tier pipeline"),
        Attr("covers", values=("food", "drinks", "both"),
             why="a drinks-only board is a bar signal"),
    ),
    CATEGORY_FOOD_DRINKS: (
        Attr("subject", values=("food", "drinks", "both")),
        Attr("drink_type",
             values=("beer", "cocktails", "wine", "non_alcoholic", "other"),
             why="beer is the Brazilian bar signal; the rest is one bucket each"),
        Attr("food_type", values=("snacks", "main_dish", "dessert", "other")),
        Attr("portion", values=("individual", "shareable"),
             why="sharing plates -> a table-of-friends venue"),
    ),
    CATEGORY_INTERIOR: (
        Attr("space_type",
             values=("dining", "bar", "dance_floor", "stage", "other"),
             why="dance_floor is the strongest role_agitado signal available"),
        Attr("lighting", values=("bright", "dim", "dark"),
             why="dim is what date and an intimate room are made of"),
        Attr("has_screens", values=("yes", "no"),
             why="'is the game on?' is one of the most-asked questions about a "
                 "Brazilian bar"),
        Attr("music_setup", values=("live_music", "dj", "none_visible"),
             why="a stage or a DJ booth — music has no photo evidence today"),
        TIME_OF_DAY,
    ),
    CATEGORY_EXTERIOR: (
        Attr("exterior_kind", values=("facade", "open_air_area", "other"),
             why="separates 'how do I find the door' from 'is there an "
                 "open-air area'"),
        Attr("covered", values=("covered", "partial", "uncovered"),
             why="the rain question, which nothing else answers"),
        Attr("view", values=("sea", "city", "nature", "none")),
        TIME_OF_DAY,
    ),
    CATEGORY_CROWD: PEOPLE_ATTRIBUTES,
    CATEGORY_OTHER: (
        Attr("other_kind",
             values=("logo_art", "event_flyer", "document", "irrelevant"),
             why="knowing WHY it is `other` lets us reclassify later without "
                 "re-billing. event_flyer is the likeliest future promotion: a "
                 "flyer carries the event, the date and the cover"),
    ),
    # Deliberately shallow: only what a poster can actually answer. Reading the
    # date itself is a purpose-built prompt for a later extraction pass — asking
    # a coarse classifier to parse it would repeat the `time_of_day` mistake
    # above, asking a question most photos in the category cannot answer.
    CATEGORY_FLYER: (
        Attr("announces_event", values=("yes", "no"),
             why="the free pre-filter that decides which images the event "
                 "extractor is allowed to spend a vision call on"),
        Attr("names_time", values=("yes", "no"),
             why="whether a time appears on the flyer at all, not what it is"),
    ),
}


def attributes_for(category: str) -> tuple[Attr, ...]:
    """The schema for a category. Empty for a category we do not know."""
    return PHOTO_ATTRIBUTES.get(category, ())


# ── validation ───────────────────────────────────────────────────────────────
def _confident_value(spec: Attr, raw: Any, threshold: float) -> str:
    """One attribute, collapsed to `not_classified` unless it clears the bar.

    The model answers `{"value": ..., "confidence": 0-1}`. A bare value with no
    confidence is NOT accepted: without a confidence there is nothing to check
    it against, and the whole point of this field is that it was only written
    when the model was sure.
    """
    if not isinstance(raw, dict):
        return NOT_CLASSIFIED
    value = raw.get("value")
    if isinstance(value, (list, tuple, dict)) or value is None:
        # A list means the model answered a different question than the one
        # asked; picking one of its answers would invent a fact.
        return NOT_CLASSIFIED
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return NOT_CLASSIFIED
    if confidence < threshold:
        return NOT_CLASSIFIED
    text = str(value)
    return text if text in spec.allowed() else NOT_CLASSIFIED


def _validate_against(specs: tuple[Attr, ...], raw: Any, threshold: float) -> dict:
    """Every field of the schema, always — a known unknown beats a silent gap."""
    values = raw if isinstance(raw, dict) else {}
    return {
        spec.name: _confident_value(spec, values.get(spec.name), threshold)
        for spec in specs
    }


def validate_attributes(category: str, raw: Any, threshold: float) -> dict:
    """This category's attributes, each either confidently read or unknown."""
    specs = attributes_for(category)
    return _validate_against(specs, raw, threshold) if specs else {}


def validate_people(raw: Any, threshold: float) -> dict:
    """The people block — for a photo of any category, when people are visible.

    Returns {} when the model reported no people at all, which is different
    from reporting people it could not describe.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    return _validate_against(PEOPLE_ATTRIBUTES, raw, threshold)


def validate_quality(raw: Any, threshold: float) -> str:
    spec = Attr("quality", values=("good", "poor"))
    return _confident_value(spec, raw, threshold)


def validate_authorship_guess(raw: Any, threshold: float) -> Optional[str]:
    """The model's read of who took the photo, or None.

    Returns None rather than `not_classified`: this is an optional guess, not a
    schema field, and an absent key says "no opinion" more honestly than a
    stored non-answer would.
    """
    spec = Attr("likely_authorship", values=AUTHORSHIP_GUESS_VALUES)
    value = _confident_value(spec, raw, threshold)
    return value if value in AUTHORSHIP_GUESS_VALUES else None


# ── prompt text, generated from the schema ───────────────────────────────────
# Generated rather than written out again, so a vocabulary change cannot leave
# the prompt asking for labels that no longer validate.
def _describe(spec: Attr) -> str:
    return f"- {spec.name}: one of {', '.join(spec.allowed())}"


def describe_categories() -> str:
    return "\n".join(f"- {name}: {rule}" for name, rule in CATEGORY_RULES.items())


def describe_attributes(category: str) -> str:
    return "\n".join(_describe(spec) for spec in attributes_for(category))


def describe_people() -> str:
    return "\n".join(_describe(spec) for spec in PEOPLE_ATTRIBUTES)


def describe_schema() -> str:
    """Every category's attributes, for the single classification prompt."""
    blocks = []
    for category in PHOTO_CATEGORIES:
        blocks.append(f"If category is `{category}`:\n{describe_attributes(category)}")
    return "\n\n".join(blocks)
