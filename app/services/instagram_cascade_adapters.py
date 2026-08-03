"""Adapters between the cascade and the systems its sources actually live in.

The cascade knows "give me a website for this venue"; these know where that
website is. Keeping them separate is what let the BDD suite drive the real
cascade against four small fakes.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from app.services.archive_sources import SOURCE_APIFY_GMAPS

# Paths that look like a handle in a url but are not one.
_IG_LINK = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)

_NON_PROFILE_PATHS = frozenset(
    {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about",
     "legal", "privacy", "developer", "directory"}
)

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


class VenueWebsiteScrapeSource:
    """The Instagram profile a venue links from its OWN website.

    The largest free source of handles left, and nothing had ever looked at it:
    measured on the top-250 Recife venues, 55 of the 129 that have a website
    publish an Instagram link on it.

    A footer link is NOT automatically the venue's — real cases include the
    agency that built the site, the franchise, and the shopping mall. So this
    tier carries a deliberately low provenance weight and lets name similarity
    decide; see plans/260730_venue-website-instagram-tier.md for the fit.

    Bounded by construction. This fetches arbitrary third-party pages from
    production during a 1,400-venue run, so it takes ONE request per venue, with
    a timeout, a byte cap, and a redirect limit. Every failure is "no candidate".
    """

    DEFAULT_TIMEOUT = 10.0
    DEFAULT_MAX_BYTES = 1_500_000
    # A browser UA: some sites serve a stub or a 403 to unknown agents.
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )

    _LINK = re.compile(r'instagram\.com/([A-Za-z0-9_.]{2,30})', re.I)

    def __init__(
        self,
        venue_dao,
        *,
        client=None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.venue_dao = venue_dao
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self._client = client or httpx.AsyncClient(
            follow_redirects=True, max_redirects=5
        )

    async def website_for(self, venue_id: str, venue=None) -> Optional[str]:
        site = self._listed_website(venue_id)
        if not site:
            return None
        # Already an Instagram url: the Google-listing tier owns it, and fetching
        # instagram.com here would spend a request to learn nothing.
        if "instagram.com" in site.lower():
            return None
        if not site.lower().startswith(("http://", "https://")):
            site = "https://" + site

        body = await self._fetch(venue_id, site)
        if not body:
            return None
        return self._first_profile_link(body)

    def _listed_website(self, venue_id: str) -> Optional[str]:
        try:
            vibe = self.venue_dao.get_vibe_attributes(venue_id)
        except Exception as e:
            logger.warning(f"[VenueWebsiteSource] vibe read failed for {venue_id}: {e}")
            return None
        return getattr(vibe, "website_uri", None) if vibe else None

    async def _fetch(self, venue_id: str, url: str) -> Optional[str]:
        try:
            response = await self._client.get(
                url,
                headers={"User-Agent": self.USER_AGENT},
                timeout=self.timeout_seconds,
            )
        except Exception as e:
            # A dead domain, a TLS error, a redirect loop, a timeout. All of it is
            # ordinary for third-party sites and none of it may fail the venue.
            logger.debug(f"[VenueWebsiteSource] {venue_id} fetch failed ({url}): {e}")
            return None

        content_type = (response.headers.get("content-type") or "").lower()
        if content_type and "html" not in content_type and "text" not in content_type:
            return None
        body = response.text or ""
        if len(body) > self.max_bytes:
            logger.debug(
                f"[VenueWebsiteSource] {venue_id} body over cap "
                f"({len(body)} > {self.max_bytes}); skipped"
            )
            return None
        return body

    def _first_profile_link(self, body: str) -> Optional[str]:
        """The first Instagram link on the page that is actually a profile.

        Returns the URL, not the handle: the cascade hands it to `extract_handle`,
        which already rejects shims and non-profile paths for every free tier.
        """
        for match in self._LINK.finditer(body):
            window = body[max(0, match.start() - 40):match.end()]
            if "l.instagram.com" in window:
                continue
            handle = match.group(1).strip(".")
            if not handle or handle.lower() in _NON_PROFILE_PATHS:
                continue
            return f"https://www.instagram.com/{handle}/"
        return None

    async def close(self) -> None:
        await self._client.aclose()


class GoogleSearchInstagramSource:
    """The Instagram profile Google surfaces for a venue.

    The last resort, and the only source that reaches a venue with no web
    presence at all: 191 Recife venues have no website, so every earlier tier has
    nothing to read, and Instagram's own user search does not surface them.

    A search result is a GUESS. Provenance is set so this tier can never clear
    the accept bar on its own — see PROVENANCE_WEIGHT in
    instagram_cascade_service.py — so a handle from here is only ever accepted
    after the judge confirms it. Two of the first five real results were
    plausible but wrong-looking (a public square, a monastery in a different
    city), which is exactly why.
    """

    def __init__(self, venue_dao, *, search_client, results: int = 10):
        self.venue_dao = venue_dao
        self.search_client = search_client
        self.results = results

    async def website_for(self, venue_id: str, venue=None) -> Optional[str]:
        name = getattr(venue, "venue_name", None)
        if not name:
            return None
        query = self._query(venue, name)
        try:
            items = await self.search_client.search(query, results=self.results)
        except Exception as e:
            # A failed actor run, a timeout, a quota error. Ordinary, and none of
            # it may fail the venue.
            logger.warning(f"[GoogleSearchSource] search failed for {venue_id}: {e}")
            return None
        return self._first_profile_link(items)

    def _query(self, venue, name: str) -> str:
        """Name, place, and the word that makes Google surface the profile.

        The neighbourhood is what separates venues sharing a name — Recife has
        several — so it is included whenever the address carries one.
        """
        where = getattr(venue, "neighborhood", None) or getattr(venue, "city", None)
        parts = [name, where or "Recife", "instagram"]
        return " ".join(str(p) for p in parts if p)

    def _first_profile_link(self, items) -> Optional[str]:
        for item in items or []:
            for value in self._strings(item):
                match = _IG_LINK.search(value)
                if not match:
                    continue
                window = value[max(0, match.start() - 40):match.end()]
                if "l.instagram.com" in window:
                    continue
                handle = match.group(1).strip(".")
                if not handle or handle.lower() in _NON_PROFILE_PATHS:
                    continue
                return f"https://www.instagram.com/{handle}/"
        return None

    @staticmethod
    def _strings(item):
        """Every string in a result row: the handle can be in the url, the title
        or the snippet, and which one varies by how Google rendered it."""
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            for value in item.values():
                yield from GoogleSearchInstagramSource._strings(value)
        elif isinstance(item, (list, tuple)):
            for value in item:
                yield from GoogleSearchInstagramSource._strings(value)
