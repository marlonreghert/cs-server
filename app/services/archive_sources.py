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
    type: str  # "number" | "text" | "select"
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
    return await client.get_place_photos(
        place_id, max_photos=cfg["max_photos_per_venue"], include_ref=True
    )


def _google_units(venues: int, cfg: dict) -> tuple[int, str]:
    # 1 Place Details + N photo-media requests, per venue.
    return venues * (1 + cfg["max_photos_per_venue"]), "Google API requests"


def _google_unit_cost(settings, cfg) -> float:
    return float(getattr(settings, "google_photo_cost_per_1k_usd", 7.0)) / 1000.0


# ── apify_gmaps_extractor ────────────────────────────────────────────────────
async def _fetch_apify(client, venue, cfg):
    """One actor run per venue; the actor bills per place, not per photo.

    Asking for a bigger photo pool is therefore nearly free, which is why
    `photo_pool` exists as a source config rather than being tied to the cap.
    """
    query = venue.get("search_query")
    if not query:
        return None
    source_cfg = cfg.get("source_config") or {}
    pool = int(source_cfg.get("photo_pool") or 20)
    return await client.fetch_venue_photos(
        query,
        max_photos=max(cfg["max_photos_per_venue"], pool),
        language=str(source_cfg.get("language") or "pt-BR"),
    )


def _apify_units(venues: int, cfg: dict) -> tuple[int, str]:
    # Billed per place scraped, independent of how many photos come back.
    return venues, "places scraped"


def _apify_unit_cost(settings, cfg) -> float:
    # place-scraped + the additional-details add-on that carries the images.
    return (
        float(getattr(settings, "apify_place_scraped_cost_usd", 0.004))
        + float(getattr(settings, "apify_place_details_cost_usd", 0.002))
    )


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
                name="photo_pool", label="Photos to request per place",
                type="number", default=20,
                help="The actor bills per place, not per photo, so a larger "
                     "pool costs almost nothing and improves selection.",
            ),
            ConfigField(
                name="language", label="Result language", type="text",
                default="pt-BR",
                help="Passed to the actor as the Google Maps locale.",
            ),
        ],
        cost_note=(
            "Billed per place scraped, not per photo. The per-image charge is "
            "not published by Apify, so the estimate is a floor."
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

    Availability is resolved from the container, so an unconfigured source is
    visibly unavailable BEFORE an operator configures a run against it.
    """
    out = []
    for source_id, source in ARCHIVE_SOURCES.items():
        available = getattr(container, source.requires_attr, None) is not None
        out.append(source.to_public(source_id, available))
    return out
