"""Pure, deterministic text parser for `venues.address.raw_text` — the
fallback source for `street`/`neighborhood`/`city`/`postal_code` whenever
Google Places cannot or does not answer (plans/260906_address-components-
backfill.md, Phase 1). Same style as its sibling `venue_address_components.py`:
no I/O, never guesses when it isn't reasonably sure.

Algorithm (see the plan's own worked traces for why each step exists):

1. `postal_code` — last regex match of a Brazilian CEP shape, independent
   of everything else below.
2. `uf` — the RIGHTMOST standalone 2-letter Brazilian state code. None
   found -> the address is not shaped like a Brazilian one; every other
   field stays None.
3. `tail` — everything before the UF token.
4. Strip `tail`'s OWN trailing UF-separator (a dash, then a comma) before
   splitting it — the single most important line here. Skipping this step
   finds the wrong dash in the DOMINANT space-joined format ("...Niterói -
   RJ...": the dash directly before "RJ", not the street/bairro dash before
   it) and yields an empty blob for the large majority of the catalog.
5. The LAST standalone dash in the fixed-up tail splits `street` from
   `blob`. No dash found -> `street` stays None and `blob` is the whole
   fixed-up tail (some real rows have no street/bairro separator at all).
6. Format B (comma-delimited): split `blob` on its LAST comma -> bairro,
   city — guarded against a street-fragment comma masquerading as the real
   one (a digit or more than 4 words in the city half rejects the split).
7. Format A (space-joined), or Format B rejected: try the trailing 3, then
   2, then 1 words of `blob` against the vocabulary (longest match wins).
   No match at any length -> left for Google or a future vocabulary update,
   never guessed.
8. Plausibility guard — only when step 5 found no dash: reject a bairro
   candidate that contains a digit, has more than 3 words, or contains a
   stoplist word (mall/housing-complex fragments this shape commonly
   captures). The city half is untouched either way.
9. Ambiguity guard: a city match flagged `ambiguous` in the vocabulary is
   accepted only when the venue's own coordinates are within
   `ambiguous_radius_km` of the vocabulary entry's coordinates; otherwise
   the whole match (city AND neighborhood) is treated as a non-match.
10. `neighborhood` := the surviving bairro candidate or None. `city` := the
    surviving city candidate.
"""
from __future__ import annotations

import re
from typing import Optional

from app.services.venue_eligibility import haversine_km
from app.utils.text_norm import fold_text

# 26 states + the federal district (Brasília) — the complete set of
# Brazilian UF codes. Word-boundaried, case-sensitive: real BestTime-echoed
# addresses always show the state code uppercase.
BRAZILIAN_UF_CODES = frozenset({
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
})
_UF_RE = re.compile(r"\b(" + "|".join(sorted(BRAZILIAN_UF_CODES)) + r")\b")

_POSTAL_RE = re.compile(r"\b(\d{5})-?(\d{3})\b")

# Dash-like characters step 4/5 treat as a separator — plain hyphen plus the
# unicode dash variants, for robustness. A hyphen with no adjacent whitespace
# (a compound word like "Ceará-Mirim") never qualifies as "standalone" (see
# _last_standalone_dash_index) so it is never mistaken for the separator.
_DASH_CHARS = "-‐‑‒–—"

# Step 8's plausibility guard: mall/housing-complex fragments this shape
# commonly captures, not real bairros. Fold-compared (accent/case-insensitive).
STOPLIST_BAIRRO_WORDS = frozenset({
    "loja", "quiosque", "piso", "sala", "shopping", "cohab", "conjunto",
})

ADDRESS_PARSER_FIELDS = ("street", "neighborhood", "city", "postal_code")


def extract_postal_code(raw_text: str) -> Optional[str]:
    """Step 1: the LAST `NNNNN-NNN`/`NNNNNNNN`-shaped CEP in `raw_text`,
    normalized to `NNNNN-NNN`. Independent of every other step."""
    matches = list(_POSTAL_RE.finditer(raw_text or ""))
    if not matches:
        return None
    m = matches[-1]
    return f"{m.group(1)}-{m.group(2)}"


def extract_uf_and_tail(raw_text: str) -> tuple[Optional[str], Optional[str]]:
    """Steps 2-3: `(uf, tail)` for the RIGHTMOST standalone Brazilian UF
    token, `tail` being everything before it. `(None, None)` when no UF
    token is found anywhere (the address is not shaped like a Brazilian
    one — e.g. a UK/US address)."""
    text = raw_text or ""
    matches = list(_UF_RE.finditer(text))
    if not matches:
        return None, None
    m = matches[-1]
    return m.group(1), text[: m.start()]


def clean_tail(tail: str) -> str:
    """Step 4: strip `tail`'s OWN trailing UF-separator before it is split.
    rstrip, then remove exactly one trailing dash-like char (if present) and
    rstrip again, then remove exactly one trailing comma (if present, the
    comma-instead-of-dash UF variant) and rstrip again."""
    cleaned = (tail or "").rstrip()
    if cleaned and cleaned[-1] in _DASH_CHARS:
        cleaned = cleaned[:-1].rstrip()
    if cleaned and cleaned[-1] == ",":
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def _last_standalone_dash_index(text: str) -> Optional[int]:
    """The index of the LAST dash-like char in `text` that is a standalone
    separator — bounded by whitespace or a string edge on BOTH sides, so a
    compound-word hyphen ("Ceará-Mirim") is never mistaken for it."""
    last = None
    for i, ch in enumerate(text):
        if ch not in _DASH_CHARS:
            continue
        before_ok = i == 0 or text[i - 1].isspace()
        after_ok = i == len(text) - 1 or text[i + 1].isspace()
        if before_ok and after_ok:
            last = i
    return last


def split_street_and_blob(tail: str) -> tuple[Optional[str], str]:
    """Step 5: `(street, blob)` from the fixed-up `tail`. No standalone dash
    found -> `street` is None and `blob` is the whole tail (trimmed) — some
    real rows have no street/bairro separator at all."""
    idx = _last_standalone_dash_index(tail)
    if idx is None:
        return None, tail.strip()
    street = tail[:idx].strip()
    blob = tail[idx + 1 :].strip()
    return (street or None), blob


def extract_blob(raw_text: str) -> Optional[str]:
    """Steps 1-5 collapsed to just the resulting `blob`, for the vocabulary
    miner's candidate generation over residual rows. None when no UF token
    is found (non-Brazilian / no shape) or the blob is empty."""
    uf, tail_raw = extract_uf_and_tail(raw_text)
    if uf is None:
        return None
    tail = clean_tail(tail_raw)
    _, blob = split_street_and_blob(tail)
    blob = blob.strip(" ,")
    return blob or None


def _vocabulary_index(vocabulary) -> dict[str, dict]:
    """Fold(name) -> vocabulary entry. First entry wins a fold-key
    collision within the given list (defensive; `load_city_vocabulary`
    already dedupes by fold-key before this ever sees it)."""
    index: dict[str, dict] = {}
    for entry in vocabulary or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            continue
        index.setdefault(fold_text(name), entry)
    return index


def _plausible_bairro(candidate: str) -> bool:
    """Step 8's plausibility guard, applied to one bairro candidate. True
    when it contains no digit, has at most 3 words, and no word (folded)
    is in the stoplist."""
    words = candidate.split()
    if not words:
        return True
    if any(ch.isdigit() for word in words for ch in word):
        return False
    if len(words) > 3:
        return False
    if any(fold_text(word) in STOPLIST_BAIRRO_WORDS for word in words):
        return False
    return True


def parse_address(
    raw_text: str,
    vocabulary,
    *,
    venue_lat: Optional[float] = None,
    venue_lng: Optional[float] = None,
    ambiguous_radius_km: float = 50.0,
) -> dict:
    """The full algorithm (steps 1-10). `vocabulary` is a sequence of
    `{name, lat, lng, ambiguous}` entries (see
    `app.services.venue_city_vocabulary.load_city_vocabulary`). Returns
    `{street, neighborhood, city, postal_code}`, each a non-empty string or
    None. Never raises: a malformed/unrecognizable `raw_text` yields every
    field None except whatever `postal_code` alone can find.
    """
    postal_code = extract_postal_code(raw_text)
    uf, tail_raw = extract_uf_and_tail(raw_text)
    if uf is None:
        return {"street": None, "neighborhood": None, "city": None, "postal_code": postal_code}

    tail = clean_tail(tail_raw)
    street, blob = split_street_and_blob(tail)
    blob = blob.strip(" ,")
    if not blob:
        return {"street": street, "neighborhood": None, "city": None, "postal_code": postal_code}

    vocab_index = _vocabulary_index(vocabulary)
    city_candidate: Optional[str] = None
    bairro_candidate: Optional[str] = None

    # ── Format B: comma-delimited ──────────────────────────────────────
    if "," in blob:
        idx = blob.rfind(",")
        b_cand = blob[:idx].strip(" ,")
        c_cand = blob[idx + 1 :].strip(" ,")
        street_fragment = bool(c_cand) and (
            any(ch.isdigit() for ch in c_cand) or len(c_cand.split()) > 4
        )
        if c_cand and not street_fragment:
            city_candidate = c_cand
            bairro_candidate = b_cand or None

    # ── Format A: space-joined (or Format B rejected) ──────────────────
    if city_candidate is None:
        tokens = blob.split()
        for n in (3, 2, 1):
            if len(tokens) < n:
                continue
            candidate = " ".join(tokens[-n:])
            if fold_text(candidate) in vocab_index:
                city_candidate = candidate
                leading = tokens[:-n]
                bairro_candidate = " ".join(leading) if leading else None
                break

    # ── Step 8: plausibility guard (only when no street/number anchored it) ──
    if street is None and bairro_candidate and not _plausible_bairro(bairro_candidate):
        bairro_candidate = None

    # ── Step 9: ambiguity guard ─────────────────────────────────────────
    if city_candidate is not None:
        entry = vocab_index.get(fold_text(city_candidate))
        if entry and entry.get("ambiguous"):
            corroborated = (
                venue_lat is not None
                and venue_lng is not None
                and entry.get("lat") is not None
                and entry.get("lng") is not None
                and haversine_km(venue_lat, venue_lng, entry["lat"], entry["lng"]) <= ambiguous_radius_km
            )
            if not corroborated:
                city_candidate = None
                bairro_candidate = None

    return {
        "street": street,
        "neighborhood": bairro_candidate or None,
        "city": city_candidate,
        "postal_code": postal_code,
    }


__all__ = [
    "ADDRESS_PARSER_FIELDS",
    "BRAZILIAN_UF_CODES",
    "STOPLIST_BAIRRO_WORDS",
    "clean_tail",
    "extract_blob",
    "extract_postal_code",
    "extract_uf_and_tail",
    "parse_address",
    "split_street_and_blob",
]
