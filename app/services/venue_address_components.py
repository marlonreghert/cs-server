"""Map Google Places API (New) `addressComponents` into the structured
`venues.address` columns (`street`/`neighborhood`/`city`/`postal_code`).

plans/260905_events-serving-projection.md, Phase 2. Pure: no I/O. Recife
bairros arrive as `sublocality_level_1` — Google's own name for a Brazilian
bairro — but not every municipality publishes one, so `neighborhood` falls
back through `sublocality` then `administrative_area_level_2`. The plan's
own Evidence section shows the bairro is present inconsistently in the
plain-text address too ("R. Abdon Batista..." has it, "R. Barão Rodrigues
Mendes..." does not), which is exactly why this structured signal is the
fix and parsing `venue_address` is not.

A field this module returns as None means "this response did not answer
it" — never "the answer is empty". That distinction is the never-clobber
write's whole contract: the DAO write path COALESCEs a None here over
whatever is already stored, and only a non-None value here is ever allowed
to change a stored one. Enforcing the never-clobber rule is the WRITE
path's job (see RdsVenueStore.update_venue_address_components); this module
only computes what one fresh response says.
"""
from __future__ import annotations

from typing import Optional

# Google Places API (New) component `types` values, in FALLBACK order per
# mapped field — see
# https://developers.google.com/maps/documentation/places/web-service/place-details#fields.
# A component's own position inside its `types` list never matters; only
# this rung order does.
_NEIGHBORHOOD_TYPES = ("sublocality_level_1", "sublocality", "administrative_area_level_2")
_CITY_TYPES = ("administrative_area_level_2", "locality")
_STREET_TYPES = ("route",)
_POSTAL_CODE_TYPES = ("postal_code",)

ADDRESS_COMPONENT_FIELDS = ("street", "neighborhood", "city", "postal_code")


def _first_component_text(components: list[dict], target_types: tuple[str, ...]) -> Optional[str]:
    """The `longText` of the first component whose `types` carries any of
    `target_types`, tried in `target_types`' own order."""
    for target in target_types:
        for component in components:
            if not isinstance(component, dict):
                continue
            if target in (component.get("types") or []):
                text = component.get("longText")
                if text:
                    return text
    return None


def map_address_components(components: Optional[list[dict]]) -> dict:
    """`components` is the raw `addressComponents` array from a Google
    Places (New) Details response, or None/empty when the field was absent
    from the response entirely. Returns
    `{street, neighborhood, city, postal_code}`, each a non-empty string or
    None (None == "not answered by this response")."""
    items = components or []
    return {
        "street": _first_component_text(items, _STREET_TYPES),
        "neighborhood": _first_component_text(items, _NEIGHBORHOOD_TYPES),
        "city": _first_component_text(items, _CITY_TYPES),
        "postal_code": _first_component_text(items, _POSTAL_CODE_TYPES),
    }


def write_mapped_components(venue_dao, venue_id: str, components, source: str) -> str:
    """Map `components` and write them via `venue_dao.
    update_venue_address_components` with the given provenance `source`,
    returning the outcome label ("written" if the response answered at
    least one field, else "unchanged") for the caller's own
    `VENUE_ADDRESS_COMPONENTS_TOTAL` bump.

    Shared glue between the opportunistic `enrich_venue` path (Place
    Details' own `addressComponents`) and the address-components backfill's
    Geocoding-by-`place_id` path
    (plans/260906_address-components-backfill.md Phase 3) — both are
    fundamentally the SAME event ("a Google address-components response was
    mapped and written"), so the map -> write -> outcome glue exists in
    exactly one place rather than two independently-maintained copies.
    """
    mapped = map_address_components(components)
    venue_dao.update_venue_address_components(venue_id, source=source, **mapped)
    return "written" if any(mapped.values()) else "unchanged"


__all__ = ["map_address_components", "write_mapped_components", "ADDRESS_COMPONENT_FIELDS"]
