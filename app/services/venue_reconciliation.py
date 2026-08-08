"""Detect venue rows that duplicate the same physical place.

The ingestion pipeline now prevents new duplicates (place-match verification +
discovery geo-dedup), but rows created before that fix remain — one physical
place under two venue_ids, each enriched separately (the "Adega do Futuro shown
twice" bug). This module finds those groups so a reconciliation pass can
deprecate the redundant rows (soft-delete, reversible).

Pure detection — no DB, no writes. "Same place" = within DEDUP_RADIUS_M AND a
folded-name match, identical to the ingestion/serving layers, so all four agree.
It is name-based, never google_place_id-based: two genuinely different venues can
wrongly share a place_id, and keying on it would merge them.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from app.utils.text_norm import names_match

DEDUP_RADIUS_M = 50


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _coords(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    lat, lng = row.get("venue_lat"), row.get("venue_lng")
    if lat is None or lng is None:
        return None
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def _canonical_sort_key(row: Dict[str, Any]) -> tuple:
    """Order within a duplicate group; the FIRST is kept, the rest deprecated.
    Keep the earliest-created row (the established record; later rows are the
    accidental re-adds), tie-broken by venue_id for determinism. created_at is
    compared as its ISO string, which sorts chronologically."""
    return (str(row.get("created_at") or ""), str(row.get("venue_id") or ""))


class DuplicateGroup:
    """One physical place found under several venue_ids."""

    def __init__(self, canonical: Dict[str, Any], duplicates: List[Dict[str, Any]]):
        self.canonical = canonical
        self.duplicates = duplicates  # rows to deprecate


def find_duplicate_groups(
    rows: List[Dict[str, Any]], radius_m: int = DEDUP_RADIUS_M
) -> List[DuplicateGroup]:
    """Cluster ACTIVE venue rows into same-place groups and, for each group of
    more than one, return the chosen canonical + the redundant rows to deprecate.

    Greedy single-link clustering by (distance <= radius_m AND names_match). A
    row with no coordinates or no name can't be matched and is never grouped.
    """
    active = [
        r for r in rows if (r.get("lifecycle_status") or "active") != "deprecated"
    ]
    radius_km = radius_m / 1000.0
    clusters: List[List[Dict[str, Any]]] = []
    cluster_coords: List[Tuple[float, float]] = []  # representative coord per cluster

    for row in active:
        c = _coords(row)
        name = row.get("venue_name") or ""
        if c is None or not name:
            continue
        placed = False
        for idx, rep_coord in enumerate(cluster_coords):
            rep = clusters[idx][0]
            if _haversine_km(
                c[0], c[1], rep_coord[0], rep_coord[1]
            ) <= radius_km and names_match(name, rep.get("venue_name") or ""):
                clusters[idx].append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
            cluster_coords.append(c)

    groups: List[DuplicateGroup] = []
    for members in clusters:
        if len(members) < 2:
            continue
        ordered = sorted(members, key=_canonical_sort_key)
        groups.append(DuplicateGroup(ordered[0], ordered[1:]))
    return groups
