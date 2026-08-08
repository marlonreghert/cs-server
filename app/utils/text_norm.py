"""Shared text normalization + name matching for venue place resolution.

The add-venue path has long carried its own `_fold_text` / `_find_name_match`
(add_venue_handler.py). This module hosts the same folded-name semantics as a
dependency-free util so the Google place-resolver can reuse them without a
handler → api layering inversion.
"""

import unicodedata

# A shorter folded name must be at least this many characters before it can
# containment-link to a longer one, so a short generic token ("bar", "pub")
# never matches an unrelated longer name. Mirrors
# add_venue_handler.MIN_CONTAINMENT_MATCH_LEN.
MIN_CONTAINMENT_MATCH_LEN = 5


def fold_text(text: str) -> str:
    """Accent-fold, casefold, strip punctuation, and collapse whitespace so
    differently-styled names of the same place compare equal (e.g.
    "LAÇA, Pina" ~ "Laca Pina")."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in without_accents)
    return " ".join(cleaned.casefold().split())


def names_match(
    a: str, b: str, min_containment_len: int = MIN_CONTAINMENT_MATCH_LEN
) -> bool:
    """True when two names plausibly refer to the same place by folded compare:
    exact folded equality, or one folded name contains the other AND the shorter
    folded name is at least ``min_containment_len`` characters. Empty/whitespace
    names never match."""
    fa = fold_text(a)
    fb = fold_text(b)
    if not fa or not fb:
        return False
    if fa == fb:
        return True
    if fa in fb or fb in fa:
        return min(len(fa), len(fb)) >= min_containment_len
    return False
