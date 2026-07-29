"""Internal routes — on-demand venue photo resolution.

`/internal` is an internal-only surface, gated at the NETWORK layer exactly like
`/admin`: Caddy does not expose it publicly and cs-server publishes no host port,
so it is reachable only over the integrated compose network / VPC (vibes_bot ->
cs-server). There is no app-level auth token by design (see the plan's Open
Questions / the coordination decision), matching the existing internal surface.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

# Global container reference — set during startup (mirrors admin_trigger_router).
_container = None


def set_container(container):
    """Set the DI container (called during startup)."""
    global _container
    _container = container
    logger.info("[InternalRouter] Container injected")


class VenuePhoto(BaseModel):
    """A single resolved venue photo: a fresh, keyless CDN URL + optional author."""
    url: str
    author_name: Optional[str] = None
    # Vibe-classifier category tag (Ambiente/Comida/Bebida/Evento/Outro), when
    # the vibe profile has a matching evidence photo for this URL. Additive:
    # absent for photos with no known category, so existing readers are
    # unaffected until they opt in.
    category: Optional[str] = None


class ResolvePhotosResponse(BaseModel):
    venue_photos: list[VenuePhoto]


@router.post(
    "/venues/{venue_id}/photos/resolve",
    response_model=ResolvePhotosResponse,
    # Omit unset fields so `category` appears ONLY when the vibe profile
    # supplied one — the response then stays byte-identical to the cached
    # fresh-photo list (which carries `category` only when present), and the
    # additive field never surfaces as `category: null` noise for readers that
    # do not consume it. url/author_name are always explicitly set below, so
    # they are unaffected.
    response_model_exclude_unset=True,
    summary="Resolve a venue's photos on demand",
    description=(
        "Resolve a single venue's Google Places photos on demand into FRESH, "
        "KEYLESS CDN URLs, cache them under venue_photos_fresh_v1:{venue_id} "
        "(TTL from photo_fresh_cache_ttl_hours), and return the list. Degrades "
        "to an empty list on missing google_place_id, zero photos, or a Google "
        "failure — never a dead URL. Omitting both max_photos and force "
        "reproduces the pre-cost-controls behaviour exactly: resolves "
        "photos_per_venue and always overwrites the cache."
    ),
)
async def resolve_venue_photos(
    venue_id: str,
    max_photos: Optional[int] = Query(
        None,
        ge=1,
        le=settings.photos_per_venue,
        description=(
            "Cap the number of photos to resolve/bill (e.g. 1 for a list "
            "hero). A sufficient or empty cached entry short-circuits the "
            "Google call; a cached entry holding fewer than this is "
            "re-resolved and overwritten. Omitted = settings.photos_per_venue "
            "with no cache check (today's behaviour)."
        ),
    ),
    force: bool = Query(
        False,
        description=(
            "Ignore the cache entirely: always call Google and always "
            "overwrite the cached entry. The dead-URL repair path."
        ),
    ),
) -> ResolvePhotosResponse:
    """Resolve + cache a venue's fresh keyless photo URLs.

    503 only when the photo service is unconfigured (no Google Places API key).
    A Google outage degrades to `{"venue_photos": []}` (never a 5xx).
    """
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")

    service = getattr(_container, "photo_enrichment_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Photo resolution not configured (missing Google Places API key)",
        )

    photos = await service.resolve_and_cache_fresh_photos(
        venue_id, max_photos=max_photos, force=force
    )
    return ResolvePhotosResponse(
        venue_photos=[
            # Pass `category` only when present so response_model_exclude_unset
            # drops it otherwise (keeping the response == the cache).
            VenuePhoto(
                url=p["url"], author_name=p.get("author_name"),
                **({"category": p["category"]} if p.get("category") else {}),
            )
            for p in photos
        ]
    )
