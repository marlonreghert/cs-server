"""Where archived photos come from.

One descriptor per source. A source owns four things the rest of the pipeline
must not hard-code:

  * the extra config fields it needs (rendered by the admin panel from here),
  * how it turns a venue into photos,
  * how much a run costs, because the billing models genuinely differ —
    Google charges per photo request, Apify per place scraped,
  * which dependency has to be wired for it to work at all.

Everything else — the caps, eligibility, the target prefix, the skip rules — is
source-independent and lives on the shared run config, not in here.

Adding a source is an entry in `ARCHIVE_SOURCES`. The admin panel renders the
catalog this module publishes, so a new source needs no UI change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SOURCE_GOOGLE_PHOTOS = "google_photos"
SOURCE_APIFY_GMAPS = "apify_gmaps_extractor"
SOURCE_SEARCHAPI_PHOTOS = "searchapi_gmaps_photos"


class ArchiveCreditExhausted(Exception):
    """The source's own budget ran out mid-run.

    Distinct from a fetch failure: the run must stop rather than keep trying,
    and the operator needs to be told which budget, not just "it failed".
    """


@dataclass(frozen=True)
class ConfigField:
    """One source-specific control, rendered by the admin panel."""

    name: str
    label: str
    type: str  # "number" | "text" | "select" | "multiselect"
    default: Any = None
    help: str = ""
    options: Optional[list[str]] = None

    def to_public(self) -> dict:
        out = {
            "name": self.name, "label": self.label, "type": self.type,
            "default": self.default, "help": self.help,
        }
        if self.options:
            out["options"] = self.options
        return out


@dataclass(frozen=True)
class ArchiveSource:
    label: str
    description: str
    # Container attribute that must be wired, and what to say when it is not.
    requires_attr: str
    unavailable_reason: str
    # async (client, venue_ctx, cfg) -> list[photo dict]
    fetch: Callable[..., Any]
    # (venues, cfg) -> (billable_units, unit_label)
    estimate_units: Callable[[int, dict], tuple[int, str]]
    # (settings, cfg) -> cost in USD for one billable unit
    unit_cost_usd: Callable[..., float]
    config_schema: list[ConfigField] = field(default_factory=list)
    cost_note: str = ""

    def to_public(self, source_id: str, available: bool) -> dict:
        return {
            "id": source_id,
            "label": self.label,
            "description": self.description,
            "available": available,
            "unavailable_reason": None if available else self.unavailable_reason,
            "config_schema": [f.to_public() for f in self.config_schema],
            "cost_note": self.cost_note,
        }


# ── google_photos ────────────────────────────────────────────────────────────
async def _fetch_google(client, venue, cfg):
    """Place Details for the photo names, then one media call per photo.

    That is 1 + N billed calls per venue, which is what the cost model below
    counts — the Place Details call was previously omitted from the estimate.
    """
    place_id = venue.get("google_place_id")
    if not place_id:
        return None  # caller records `no_place_id`; costs nothing
    photos = await client.get_place_photos(
        place_id, max_photos=cfg["max_photos_per_venue"], include_ref=True
    )
    # The Places photo endpoint returns photos and nothing else, so there is no
    # extra info to keep — an empty info block, not a missing one.
    return {"photos": photos or [], "info": {"google_place_id": place_id}}


def _google_units(venues: int, cfg: dict) -> tuple[int, str]:
    # 1 Place Details + N photo-media requests, per venue.
    return venues * (1 + cfg["max_photos_per_venue"]), "Google API requests"


def _google_unit_cost(settings, cfg) -> float:
    return float(getattr(settings, "google_photo_cost_per_1k_usd", 7.0)) / 1000.0


# ── apify_gmaps_extractor ────────────────────────────────────────────────────
async def _fetch_apify(client, venue, cfg):
    """One actor run per venue; the actor bills per place, not per photo.

    The actor returns images in the order Google Maps displays them — the "All"
    tab, which is Google's own ranking — so asking for N gives the TOP N, not an
    arbitrary N. There is no category filter on either compass actor:
    `imageCategories` is output metadata listing which tabs exist, not a
    per-image label and not an input, so "only the Menu photos" is not
    expressible here.

    Requests exactly the operator's cap. An earlier version over-fetched into a
    "pool" borrowed from the menu-photo path, where a bigger pool feeds a
    downstream classifier; the archive has no such selection step, so the pool
    only meant storing more photos than the operator asked for.
    """
    query = venue.get("search_query")
    if not query:
        return None
    source_cfg = cfg.get("source_config") or {}
    result = await client.fetch_venue_photos(
        query,
        max_photos=cfg["max_photos_per_venue"],
        language=str(source_cfg.get("language") or "pt-BR"),
    )
    if result is None:
        return None
    # Tolerate a client that still returns a bare photo list.
    if isinstance(result, list):
        return {"photos": result, "info": {}}
    return result


def _apify_units(venues: int, cfg: dict) -> tuple[int, str]:
    # Billed per place scraped, independent of how many photos come back.
    return venues, "places scraped"


def _apify_unit_cost(settings, cfg) -> float:
    # place-scraped + the additional-details add-on that carries the images.
    return (
        float(getattr(settings, "apify_place_scraped_cost_usd", 0.004))
        + float(getattr(settings, "apify_place_details_cost_usd", 0.002))
    )


# ── searchapi_gmaps_photos ───────────────────────────────────────────────────
async def _fetch_searchapi(client, venue, cfg):
    """One request PER CATEGORY, which is exactly what it bills.

    The only source that can say which Google tab a photo came from. The engine
    returns no categories array, so the ids come from the table on the client;
    a tab a venue lacks simply yields nothing and is skipped.
    """
    place_id = venue.get("google_place_id")
    if not place_id:
        return None
    source_cfg = cfg.get("source_config") or {}
    categories = source_cfg.get("categories") or ["menu"]
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",") if c.strip()]
    # Per CATEGORY, so the per-category cap governs when set; otherwise the
    # venue cap is the only bound and the service trims the total afterwards.
    per_category = cfg.get("max_photos_per_category") or cfg["max_photos_per_venue"]
    return await client.fetch_venue_photos(
        place_id,
        categories=categories,
        max_photos=per_category,
        hl=str(source_cfg.get("language") or "pt-BR"),
    )


def _searchapi_units(venues: int, cfg: dict) -> tuple[int, str]:
    source_cfg = cfg.get("source_config") or {}
    categories = source_cfg.get("categories") or ["menu"]
    if isinstance(categories, str):
        categories = [c for c in categories.split(",") if c.strip()]
    # One billed search per category per venue — no discovery call, because the
    # category ids are constants.
    return venues * max(1, len(categories)), "searches"


def _searchapi_unit_cost(settings, cfg) -> float:
    return float(getattr(settings, "searchapi_cost_per_1k_usd", 4.0)) / 1000.0


ARCHIVE_SOURCES: dict[str, ArchiveSource] = {
    SOURCE_GOOGLE_PHOTOS: ArchiveSource(
        label="Google Places API",
        description=(
            "Official API. Licensed and attributed, and the most expensive: "
            "one Place Details call plus one request per photo, per venue."
        ),
        requires_attr="google_places_client",
        unavailable_reason="Google Places API key not configured",
        fetch=_fetch_google,
        estimate_units=_google_units,
        unit_cost_usd=_google_unit_cost,
        config_schema=[],
        cost_note="Billed per request: 1 Place Details + 1 per photo, per venue.",
    ),
    SOURCE_APIFY_GMAPS: ArchiveSource(
        label="Apify Google Maps extractor",
        description=(
            "Scrapes Google Maps through the compass extractor. Roughly an "
            "order of magnitude cheaper because it bills per place, not per "
            "photo — but it is a scrape, not a licensed API."
        ),
        requires_attr="apify_gmaps_extractor_client",
        unavailable_reason="Apify API token not configured (APIFY_API_TOKEN)",
        fetch=_fetch_apify,
        estimate_units=_apify_units,
        unit_cost_usd=_apify_unit_cost,
        config_schema=[
            ConfigField(
                name="language", label="Result language", type="text",
                default="pt-BR",
                help="Passed to the actor as the Google Maps locale.",
            ),
        ],
        cost_note=(
            "Billed per place scraped, not per photo. Photos come back in "
            "Google Maps' own display order, so the cap takes the TOP N. The "
            "per-image charge is not published by Apify, so the estimate is a "
            "floor."
        ),
    ),
    SOURCE_SEARCHAPI_PHOTOS: ArchiveSource(
        label="SearchApi Google Maps photos (by category)",
        description=(
            "The only source that knows which Google tab a photo came from — "
            "Menu, Food & drink, Vibe. Costs one search per category per venue."
        ),
        requires_attr="searchapi_photos_client",
        unavailable_reason="SearchApi key not configured (SEARCHAPI_API_KEY)",
        fetch=_fetch_searchapi,
        estimate_units=_searchapi_units,
        unit_cost_usd=_searchapi_unit_cost,
        config_schema=[
            ConfigField(
                name="categories", label="Photo categories", type="multiselect",
                default=["menu"],
                options=["menu", "food_drink", "vibe", "latest", "all"],
                help="Each category is a separate billed search per venue. A "
                     "venue that lacks a tab is skipped, not failed. `all` is "
                     "the unfiltered view — it catches photos the named tabs "
                     "miss, including place-specific ones like \"Risotto\" "
                     "whose ids cannot be enumerated.",
            ),
            ConfigField(
                name="language", label="Result language", type="text",
                default="pt-BR", help="Passed to the engine as `hl`.",
            ),
        ],
        cost_note=(
            "Billed per search: one per category, per venue. No discovery call "
            "— the category ids are constants."
        ),
    ),
}

SUPPORTED_SOURCES = tuple(ARCHIVE_SOURCES)


def get_source(source_id: str) -> ArchiveSource:
    try:
        return ARCHIVE_SOURCES[source_id]
    except KeyError:
        raise KeyError(source_id)


def public_catalog(container) -> list[dict]:
    """The catalog the admin panel renders.

    Availability is resolved from the SERVICE that actually dispatches, not
    from the container. They name the same client differently — the container
    calls it `google_places_api`, the service `google_places_client` — so
    asking the container reported Google as unavailable while runs against it
    worked fine. Whatever answers the fetch must answer the availability
    question too.
    """
    holder = getattr(container, "venue_photo_archive_service", None) or container
    out = []
    for source_id, source in ARCHIVE_SOURCES.items():
        available = getattr(holder, source.requires_attr, None) is not None
        out.append(source.to_public(source_id, available))
    return out
