"""The data-derived city vocabulary the address parser matches against
(plans/260906_address-components-backfill.md, Phase 1). Pure where it can
be: `load_city_vocabulary`/`mine_city_vocabulary_candidates` take their
inputs as plain data and make no I/O of their own; only the thin admin-
config read inside `load_city_vocabulary` touches anything external, and it
degrades to the seeded capitals alone on any failure (mirrors
`venue_eligibility.load_geo_fence`'s own fail-safe posture).

Two admin-config keys, two different trust levels:
- `address_city_vocabulary` — operator-APPROVED additions beyond the 27
  state capitals. Editable via the existing generic
  `PUT /admin/config/address_city_vocabulary`, shape-validated by
  `validate_address_city_vocabulary_config` (correctness of the name is a
  human judgment call, not validated).
- `address_city_vocabulary_candidates` — read-only mining OUTPUT. Never
  applied automatically; an operator inspects it via the existing generic
  `GET /admin/config/address_city_vocabulary_candidates` and, if a name
  looks right, adds it to `address_city_vocabulary` by hand.

`load_city_vocabulary` is the ONE function every call site (the live parser,
the backfill service) must go through — never the admin-config key read
directly — because it is deliberately NOT this repo's usual
`AdminConfigService`-consumer shape (store-and-return-verbatim,
`venue_eligibility.load_geo_fence`'s own pattern): `address_city_vocabulary`
holds only NEW cities beyond the 27 capitals, so replacing the default
outright the way `load_geo_fence` does would silently drop the capitals the
very first time an operator approves anything. Capitals win any collision on
geography (the server owns coordinates, matching `venue_eligibility`'s own
posture) — but an approved config entry sharing a capital's name can still
flag it `ambiguous` (a data fact from the mining pass, not a geography
override), the one way a capital can be marked ambiguous for the live parser
without ever hand-maintaining a collision list in code.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from app.services.venue_address_parser import extract_blob, parse_address
from app.services.venue_eligibility import STATE_CAPITALS, haversine_km
from app.utils.text_norm import fold_text

logger = logging.getLogger(__name__)

ADMIN_CONFIG_CITY_VOCABULARY_KEY = "address_city_vocabulary"
ADMIN_CONFIG_CITY_VOCABULARY_CANDIDATES_KEY = "address_city_vocabulary_candidates"

# Brazil's bounding box, generously padded. Shape-only: the validator never
# second-guesses whether a NAME is a real municipality, only that its
# coordinates are plausibly inside the country.
_BRAZIL_LAT_RANGE = (-34.0, 6.0)
_BRAZIL_LNG_RANGE = (-75.0, -28.0)

# The mechanical left-extension assist: a connector/prefix word immediately
# preceding a raw candidate's own left boundary extends it by one word before
# the candidate is finalized as a SUGGESTION (never a guarantee — see the
# module docstring and the plan's own "São José de Ribamar" counter-example).
CONNECTOR_PREFIX_WORDS_FOLDED = frozenset({
    "de", "da", "do", "das", "dos", "sao", "santo", "santa", "nossa", "nova", "novo",
})


def _capital_entries() -> list[dict]:
    return [
        {"name": c["name"], "lat": c["lat"], "lng": c["lng"], "ambiguous": False}
        for c in STATE_CAPITALS
    ]


def load_city_vocabulary(admin_config_service) -> list[dict]:
    """`STATE_CAPITALS` UNIONED WITH the operator-approved
    `address_city_vocabulary` additions, by folded name — never the config
    value alone (see the module docstring for why `load_geo_fence`'s
    replace-outright shape would be wrong here). A missing key, unreadable
    client, or malformed value all degrade to the capitals alone, mirroring
    `venue_eligibility.load_geo_fence`'s fail-safe posture."""
    merged: dict[str, dict] = {
        fold_text(entry["name"]): dict(entry) for entry in _capital_entries()
    }

    raw = None
    if admin_config_service is not None:
        try:
            raw = admin_config_service.get(ADMIN_CONFIG_CITY_VOCABULARY_KEY)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                f"[venue_city_vocabulary] failed reading "
                f"{ADMIN_CONFIG_CITY_VOCABULARY_KEY}, using capitals only: {e}"
            )
            raw = None

    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            key = fold_text(name)
            ambiguous = bool(entry.get("ambiguous"))
            if key in merged:
                # Capitals win the collision on geography/identity; an
                # approved ambiguous flag for the SAME name still augments
                # the seeded entry rather than being discarded.
                if ambiguous:
                    merged[key]["ambiguous"] = True
            else:
                lat, lng = entry.get("lat"), entry.get("lng")
                merged[key] = {"name": name, "lat": lat, "lng": lng, "ambiguous": ambiguous}

    return list(merged.values())


def validate_address_city_vocabulary_config(value) -> list[dict]:
    """Shape-only validator for `PUT /admin/config/address_city_vocabulary`:
    a non-empty list of `{name, lat, lng, ambiguous?}`, each name non-empty
    and unique (folded), lat/lng numeric and inside a plausible Brazil
    bounding box, `ambiguous` a bool when present (defaults False).
    Correctness of the NAME (is this really a Brazilian municipality?) is a
    human judgment call this validator does not attempt."""
    if not isinstance(value, list) or not value:
        raise ValueError(
            "address_city_vocabulary must be a non-empty list of "
            '{"name", "lat", "lng", "ambiguous"?}'
        )
    normalized: list[dict] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("each address_city_vocabulary entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each entry needs a non-empty 'name'")
        lat, lng = entry.get("lat"), entry.get("lng")
        if isinstance(lat, bool) or not isinstance(lat, (int, float)):
            raise ValueError(f"entry {name!r} needs a numeric 'lat'")
        if isinstance(lng, bool) or not isinstance(lng, (int, float)):
            raise ValueError(f"entry {name!r} needs a numeric 'lng'")
        if not (_BRAZIL_LAT_RANGE[0] <= float(lat) <= _BRAZIL_LAT_RANGE[1]):
            raise ValueError(f"entry {name!r} lat is outside the plausible Brazil range")
        if not (_BRAZIL_LNG_RANGE[0] <= float(lng) <= _BRAZIL_LNG_RANGE[1]):
            raise ValueError(f"entry {name!r} lng is outside the plausible Brazil range")
        ambiguous = entry.get("ambiguous", False)
        if not isinstance(ambiguous, bool):
            raise ValueError(f"entry {name!r} 'ambiguous' must be a boolean")
        key = fold_text(name)
        if key in seen:
            raise ValueError(f"duplicate city name: {name!r}")
        seen.add(key)
        normalized.append({"name": name, "lat": float(lat), "lng": float(lng), "ambiguous": ambiguous})
    return normalized


def mine_city_vocabulary_candidates(
    rows,
    current_vocabulary,
    *,
    min_distinct_remainders: int = 3,
    geo_tightness_km: float = 30.0,
    ambiguous_ngram_min_occurrences: int = 2,
) -> dict:
    """Two mechanical, read-only passes over `rows` (each
    `{"raw_text", "lat", "lng"}` — a `venues.address` row), producing the
    `address_city_vocabulary_candidates` output. Never applied automatically
    — see the module docstring.

    Pass 1 (candidate generation), over rows where `current_vocabulary`
    resolves NO city at all: trailing 1/2/3-word n-grams of the row's own
    `blob` (steps 1-5 of the parser), promoted to a candidate only when it
    recurs across `min_distinct_remainders`+ DISTINCT leading prefixes AND
    the contributing rows cluster within `geo_tightness_km` of their own
    centroid. All passing lengths are surfaced side by side (a real
    municipality name can legitimately be 1-3 words, and this statistical
    test cannot always tell a true multi-word name from a shorter name's own
    fragment sharing the identical points — see the reproduced Icaraí/
    Niterói over-merge in this module's own test suite). A connector/prefix
    word (de/da/do/das/dos/são/santo/santa/nossa/nova/novo) immediately to
    a candidate's left extends it by one word into `suggested_name` — a
    draft, not a guarantee.

    Pass 2 (ambiguity flag), over EVERY row (not just residuals), for every
    name in the vocabulary UNION this run's own candidate suggestions: counts
    how often the name appears inside a blob as a substring that is NOT the
    row's own actual resolved city. Above `ambiguous_ngram_min_occurrences`,
    flagged ambiguous — this is how a name like "Boa Vista" gets flagged
    automatically from the data, without a hand-maintained collision list.
    """
    residual: list[tuple[str, Optional[float], Optional[float]]] = []
    for row in rows:
        raw_text = row.get("raw_text") or ""
        lat, lng = row.get("lat"), row.get("lng")
        resolved = parse_address(raw_text, current_vocabulary, venue_lat=lat, venue_lng=lng)
        if resolved["city"] is not None:
            continue
        blob = extract_blob(raw_text)
        if blob:
            residual.append((blob, lat, lng))

    groups: dict[tuple[int, str], dict] = {}
    for blob, lat, lng in residual:
        tokens = blob.split()
        for n in (1, 2, 3):
            if len(tokens) < n:
                continue
            ngram = " ".join(tokens[-n:])
            leading = tokens[:-n]
            key = (n, fold_text(ngram))
            group = groups.setdefault(
                key, {"n": n, "ngram": ngram, "prefixes": set(), "points": [], "left_words": []}
            )
            group["prefixes"].add(" ".join(leading))
            if lat is not None and lng is not None:
                group["points"].append((lat, lng))
            if leading:
                group["left_words"].append(leading[-1])

    candidates: list[dict] = []
    for (n, _folded), group in groups.items():
        points = group["points"]
        if not points:
            continue
        if len(group["prefixes"]) < min_distinct_remainders:
            continue
        clat = sum(p[0] for p in points) / len(points)
        clng = sum(p[1] for p in points) / len(points)
        if not all(haversine_km(p[0], p[1], clat, clng) <= geo_tightness_km for p in points):
            continue

        suggested = group["ngram"]
        if group["left_words"]:
            common_word, _count = Counter(group["left_words"]).most_common(1)[0]
            if fold_text(common_word) in CONNECTOR_PREFIX_WORDS_FOLDED:
                suggested = f"{common_word} {group['ngram']}"

        candidates.append({
            "ngram": group["ngram"],
            "n": n,
            "suggested_name": suggested,
            "distinct_prefixes": len(group["prefixes"]),
            "sample_size": len(points),
            "centroid_lat": clat,
            "centroid_lng": clng,
        })
    candidates.sort(key=lambda c: (-c["n"], c["ngram"]))

    universe_names = {entry["name"] for entry in current_vocabulary if entry.get("name")}
    universe_names |= {c["suggested_name"] for c in candidates}
    folded_universe = {fold_text(name): name for name in universe_names if fold_text(name)}
    occurrences: dict[str, int] = {name: 0 for name in universe_names}

    for row in rows:
        raw_text = row.get("raw_text") or ""
        lat, lng = row.get("lat"), row.get("lng")
        blob = extract_blob(raw_text)
        if not blob:
            continue
        resolved_city = parse_address(raw_text, current_vocabulary, venue_lat=lat, venue_lng=lng)["city"]
        resolved_folded = fold_text(resolved_city) if resolved_city else None
        folded_blob = fold_text(blob)
        for folded_name, original_name in folded_universe.items():
            if folded_name == resolved_folded:
                continue
            if folded_name in folded_blob:
                occurrences[original_name] += 1

    ambiguous = [
        {"name": name, "occurrences": count}
        for name, count in occurrences.items()
        if count > ambiguous_ngram_min_occurrences
    ]
    ambiguous.sort(key=lambda a: (-a["occurrences"], a["name"]))

    return {"candidates": candidates, "ambiguous": ambiguous}


__all__ = [
    "ADMIN_CONFIG_CITY_VOCABULARY_CANDIDATES_KEY",
    "ADMIN_CONFIG_CITY_VOCABULARY_KEY",
    "CONNECTOR_PREFIX_WORDS_FOLDED",
    "load_city_vocabulary",
    "mine_city_vocabulary_candidates",
    "validate_address_city_vocabulary_config",
]
