"""Internal routes — on-demand venue photo resolution.

`/internal` is an internal-only surface, gated at the NETWORK layer exactly like
`/admin`: Caddy does not expose it publicly and cs-server publishes no host port,
so it is reachable only over the integrated compose network / VPC (vibes_bot ->
cs-server). There is no app-level auth token by design (see the plan's Open
Questions / the coordination decision), matching the existing internal surface.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


class ResolvePhotosResponse(BaseModel):
    venue_photos: list[VenuePhoto]


@router.post(
    "/venues/{venue_id}/photos/resolve",
    response_model=ResolvePhotosResponse,
    summary="Resolve a venue's photos on demand",
    description=(
        "Resolve a single venue's Google Places photos on demand into FRESH, "
        "KEYLESS CDN URLs, cache them under venue_photos_fresh_v1:{venue_id} "
        "(short TTL), and return the list. Degrades to an empty list on missing "
        "google_place_id, zero photos, or a Google failure — never a dead URL."
    ),
)
async def resolve_venue_photos(venue_id: str) -> ResolvePhotosResponse:
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

    photos = await service.resolve_and_cache_fresh_photos(venue_id)
    return ResolvePhotosResponse(
        venue_photos=[
            VenuePhoto(url=p["url"], author_name=p.get("author_name")) for p in photos
        ]
    )
