"""Adapters between the cascade and the systems its sources actually live in.

The cascade knows "give me a website for this venue"; these know where that
website is. Keeping them separate is what let the BDD suite drive the real
cascade against four small fakes.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.services.archive_sources import SOURCE_APIFY_GMAPS

logger = logging.getLogger(__name__)


class GoogleListingWebsiteSource:
    """The venue's website as Google Places reported it.

    Free: the value is already stored on the venue's vibe attributes by the
    Google Places enrichment; no API call is made here.
    """

    def __init__(self, venue_dao):
        self.venue_dao = venue_dao

    async def website_for(self, venue_id: str, venue=None) -> Optional[str]:
        try:
            vibe = self.venue_dao.get_vibe_attributes(venue_id)
        except Exception as e:
            logger.warning(f"[GoogleListingSource] vibe read failed for {venue_id}: {e}")
            return None
        return getattr(vibe, "website_uri", None) if vibe else None


class ArchivedGmapsWebsiteSource:
    """The venue's website from the Apify Google Maps payload already in S3.

    Free in the sense that matters: the actor run was paid for by the photo
    archive, and the non-media half of its response was stored verbatim. This
    only reads it back.
    """

    def __init__(self, media_store, source: str = SOURCE_APIFY_GMAPS):
        self.media_store = media_store
        self.source = source

    async def website_for(self, venue_id: str, venue=None) -> Optional[str]:
        if self.media_store is None:
            return None
        # PermissionError propagates on purpose: the cascade reports the source
        # as unavailable (IAM not applied yet) rather than as "no handle here".
        info = await self.media_store.get_info(source=self.source, venue_id=venue_id)
        if not info:
            return None
        place = info.get("place") if isinstance(info, dict) else None
        place = place if isinstance(place, dict) else info
        return place.get("website") or place.get("webSite") or None


class ApifySearchSource:
    """The paid tier: search Instagram by venue name + city."""

    def __init__(self, apify_client, city_resolver=None, candidates: int = 3):
        self.apify_client = apify_client
        self.candidates = candidates
        self._city_resolver = city_resolver

    async def search(self, venue, limit: Optional[int] = None) -> list[dict]:
        if self.apify_client is None or venue is None:
            return []
        city = ""
        if self._city_resolver is not None:
            try:
                city = self._city_resolver(getattr(venue, "venue_address", "") or "")
            except Exception:
                city = ""
        query = f"{getattr(venue, 'venue_name', '')} {city}".strip()
        profiles = await self.apify_client.search_users(
            query=query, results_limit=limit or self.candidates
        )
        return [
            {
                "username": p.username,
                "display_name": p.full_name,
                "bio": p.biography,
                "followers": p.followers_count,
                "is_business_account": p.is_business_account,
                "business_category": p.business_category_name,
            }
            for p in profiles or []
        ]


class ArchivedVenuePhotoSource:
    """Venue photos for the judge, read from the media archive.

    Returns an empty list rather than raising when nothing is archived — the
    judge is required to work with no images at all.
    """

    def __init__(self, media_store, source: str = SOURCE_APIFY_GMAPS):
        self.media_store = media_store
        self.source = source

    async def venue_photos(self, venue_id: str, limit: int = 3) -> list:
        if self.media_store is None:
            return []
        try:
            info = await self.media_store.get_info(source=self.source, venue_id=venue_id)
        except Exception:
            return []
        if not info:
            return []
        photos = (info.get("photos") if isinstance(info, dict) else None) or []
        return [p.get("url") for p in photos[:limit] if isinstance(p, dict) and p.get("url")]
