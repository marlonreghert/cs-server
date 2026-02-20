"""Service for fetching venue menu photos and storing them on S3.

Uses Instagram Highlights (primary) to discover menu photos from venue
Instagram accounts, with Google Maps photos via compass/google-maps-extractor
as a fallback.

For each venue:
1. Check if venue has an Instagram handle (from instagram_enrichment)
2. PRIMARY: Fetch highlights via apify/instagram-scraper, filter by menu keywords
3. FALLBACK: Fetch categorized photos via compass/google-maps-extractor
4. Download photos and upload to S3
5. Store metadata in Redis

Follows the same pattern as instagram_enrichment_service.py.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx

from app.api.apify_instagram_highlights_client import ApifyInstagramHighlightsClient
from app.api.apify_gmaps_extractor_client import ApifyGMapsExtractorClient
from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.api.s3_client import S3Client
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.menu import MenuPhoto, VenueMenuPhotos
from app.metrics import (
    MENU_PHOTO_ENRICHMENT_RESULTS,
    MENU_VENUES_WITH_PHOTOS,
    MENU_PHOTOS_STORED_TOTAL,
)

logger = logging.getLogger(__name__)

# Rate limiting
INTER_VENUE_DELAY = 1.5  # seconds between venues

# Default menu category keywords
DEFAULT_MENU_CATEGORIES = ["menu", "cardápio", "cardapio", "preços", "valores"]


class MenuPhotoEnrichmentService:
    """Fetches and caches venue menu photos.

    Primary: Instagram Highlights via apify/instagram-scraper.
    Fallback: Google Maps photos via compass/google-maps-extractor.
    """

    def __init__(
        self,
        instagram_highlights_client: Optional[ApifyInstagramHighlightsClient],
        gmaps_extractor_client: Optional[ApifyGMapsExtractorClient],
        s3_client: S3Client,
        venue_dao: RedisVenueDAO,
        enrichment_limit: int = 10,
        photos_per_venue: int = 10,
        menu_categories: Optional[list[str]] = None,
    ):
        self.instagram_highlights_client = instagram_highlights_client
        self.gmaps_extractor_client = gmaps_extractor_client
        self.s3_client = s3_client
        self.venue_dao = venue_dao
        self.enrichment_limit = enrichment_limit
        self.photos_per_venue = photos_per_venue
        self.menu_categories = menu_categories or DEFAULT_MENU_CATEGORIES
        self._download_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def close(self):
        """Close internal HTTP client."""
        await self._download_client.aclose()

    async def enrich_venue(self, venue_id: str, force_refresh: bool = False) -> Optional[VenueMenuPhotos]:
        """Fetch and store menu photos for a single venue.

        Tries Instagram Highlights first, falls back to Google Maps extractor.

        Args:
            venue_id: Internal venue ID
            force_refresh: If True, re-enrich even if cached

        Returns:
            VenueMenuPhotos result, or None on error
        """
        # Check cache
        if not force_refresh:
            existing = self.venue_dao.get_venue_menu_photos(venue_id)
            if existing is not None:
                logger.debug(f"[MenuPhotoEnrichment] Cache hit for {venue_id}")
                MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="cached").inc()
                return existing

        # Get venue data
        venue = self.venue_dao.get_venue(venue_id)
        if venue is None:
            logger.warning(f"[MenuPhotoEnrichment] Venue not found: {venue_id}")
            return None

        # Try Instagram Highlights (primary)
        photo_data = None
        if self.instagram_highlights_client:
            ig_data = self.venue_dao.get_venue_instagram(venue_id)
            if ig_data and ig_data.instagram_handle:
                photo_data = await self._fetch_via_instagram(
                    ig_data.instagram_handle, venue.venue_name
                )

        # Fallback to Google Maps Extractor
        if photo_data is None and self.gmaps_extractor_client:
            logger.info(
                f"[MenuPhotoEnrichment] No IG highlights for {venue.venue_name}, "
                f"trying Google Maps fallback"
            )
            photo_data = await self._fetch_via_gmaps(venue)

        if not photo_data or not photo_data.get("photos"):
            logger.info(
                f"[MenuPhotoEnrichment] No menu photos found for {venue.venue_name}"
            )
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="no_photos_found").inc()
            result = VenueMenuPhotos(venue_id=venue_id)
            self.venue_dao.set_venue_menu_photos(result)
            return result

        photos_list = photo_data["photos"]
        categories = photo_data.get("categories", [])
        has_menu_category = photo_data.get("has_menu_category", False)
        source = photo_data.get("source", "")

        # Download and upload each photo to S3
        all_photos: list[MenuPhoto] = []
        for url_info in photos_list:
            image_url = url_info.get("image_url", "")
            if not image_url:
                continue

            try:
                photo = await self._download_and_upload(
                    venue_id=venue_id,
                    image_url=image_url,
                    author_name=url_info.get("author_name"),
                )
                if photo:
                    all_photos.append(photo)
            except Exception as e:
                logger.error(
                    f"[MenuPhotoEnrichment] Failed to process photo for "
                    f"{venue_id}: {e}"
                )

            if len(all_photos) >= self.photos_per_venue:
                break

        # Cache result
        result = VenueMenuPhotos(
            venue_id=venue_id,
            photos=all_photos,
            available_categories=categories,
            has_menu_category=has_menu_category,
            total_images_on_maps=len(photos_list),
            source=source,
        )
        self.venue_dao.set_venue_menu_photos(result)

        if all_photos:
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="enriched").inc()
            MENU_PHOTOS_STORED_TOTAL.inc(len(all_photos))
            logger.info(
                f"[MenuPhotoEnrichment] Stored {len(all_photos)} photos for "
                f"{venue.venue_name} (source: {source})"
            )
        else:
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="no_photos_found").inc()
            logger.info(
                f"[MenuPhotoEnrichment] No photos downloaded for {venue.venue_name}"
            )

        return result

    async def _fetch_via_instagram(
        self, instagram_handle: str, venue_name: str
    ) -> Optional[dict]:
        """Fetch menu photos from Instagram highlights.

        Returns:
            Dict with keys: photos, categories, has_menu_category, source
            or None on failure / no results.
        """
        try:
            highlight_photos = await self.instagram_highlights_client.fetch_menu_highlights(
                username=instagram_handle,
                menu_keywords=self.menu_categories,
            )

            if not highlight_photos:
                logger.info(
                    f"[MenuPhotoEnrichment] No menu highlights for @{instagram_handle} "
                    f"({venue_name})"
                )
                return None

            return {
                "photos": [
                    {"image_url": p["image_url"], "author_name": None}
                    for p in highlight_photos
                ],
                "categories": list(set(
                    p.get("highlight_title", "") for p in highlight_photos
                )),
                "has_menu_category": True,
                "source": "instagram_highlights",
            }

        except ApifyCreditExhaustedError:
            logger.error(
                f"[MenuPhotoEnrichment] Apify credits exhausted for "
                f"@{instagram_handle} ({venue_name})"
            )
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="credit_exhausted").inc()
            raise

        except Exception as e:
            logger.error(
                f"[MenuPhotoEnrichment] Instagram highlights error for "
                f"@{instagram_handle} ({venue_name}): {e}"
            )
            return None

    async def _fetch_via_gmaps(self, venue) -> Optional[dict]:
        """Fetch menu photos from Google Maps via compass extractor.

        Builds a search query from venue name + address.

        Returns:
            Dict with keys: photos, categories, has_menu_category, source
            or None on failure / no results.
        """
        search_query = f"{venue.venue_name} {venue.venue_address}"

        try:
            photos = await self.gmaps_extractor_client.fetch_venue_menu_photos(
                search_query=search_query,
                menu_keywords=self.menu_categories,
                max_photos=self.photos_per_venue,
            )

            if not photos:
                return None

            return {
                "photos": [
                    {"image_url": p["image_url"], "author_name": None}
                    for p in photos
                ],
                "categories": list(set(
                    p.get("category", "") for p in photos
                )),
                "has_menu_category": any(p.get("category") for p in photos),
                "source": "gmaps_extractor",
            }

        except ApifyCreditExhaustedError:
            logger.error(
                f"[MenuPhotoEnrichment] Apify credits exhausted for "
                f"{venue.venue_name}"
            )
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="credit_exhausted").inc()
            raise

        except Exception as e:
            logger.error(
                f"[MenuPhotoEnrichment] GMaps extractor error for "
                f"{venue.venue_name}: {e}"
            )
            return None

    async def enrich_all_venues(self, force_refresh: bool = False) -> int:
        """Fetch photos for all venues.

        Args:
            force_refresh: If True, re-enrich even if cached

        Returns:
            Number of venues successfully enriched with photos
        """
        all_venue_ids = self.venue_dao.list_all_venue_ids()
        logger.info(
            f"[MenuPhotoEnrichment] Starting enrichment for "
            f"{len(all_venue_ids)} venues (limit={self.enrichment_limit})"
        )

        if not all_venue_ids:
            logger.warning("[MenuPhotoEnrichment] No venues found")
            return 0

        enriched_count = 0
        processed = 0
        skipped = 0
        errors = 0

        for venue_id in all_venue_ids:
            # Check cache
            if not force_refresh:
                existing = self.venue_dao.get_venue_menu_photos(venue_id)
                if existing is not None:
                    if existing.has_photos():
                        enriched_count += 1
                    skipped += 1
                    continue

            # Enforce limit
            if self.enrichment_limit > 0 and processed >= self.enrichment_limit:
                logger.info(
                    f"[MenuPhotoEnrichment] Reached enrichment limit "
                    f"({self.enrichment_limit}). Stopping."
                )
                break

            try:
                result = await self.enrich_venue(venue_id, force_refresh=True)
                if result and result.has_photos():
                    enriched_count += 1
                processed += 1

            except ApifyCreditExhaustedError:
                logger.error(
                    "[MenuPhotoEnrichment] Apify credits exhausted. "
                    "Stopping enrichment."
                )
                break

            except Exception as e:
                logger.error(
                    f"[MenuPhotoEnrichment] Error processing {venue_id}: {e}"
                )
                MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="error").inc()
                errors += 1

            # Rate limit between venues
            await asyncio.sleep(INTER_VENUE_DELAY)

        # Update metrics
        total_with_photos = self.venue_dao.count_venues_with_menu_photos()
        MENU_VENUES_WITH_PHOTOS.set(total_with_photos)

        logger.info(
            f"[MenuPhotoEnrichment] Enrichment complete: "
            f"{enriched_count} enriched, {processed} API calls, "
            f"{skipped} skipped (cached), {errors} errors"
        )

        return enriched_count

    async def _download_and_upload(
        self,
        venue_id: str,
        image_url: str,
        author_name: Optional[str] = None,
    ) -> Optional[MenuPhoto]:
        """Download a photo from URL and upload to S3.

        Args:
            venue_id: Venue identifier
            image_url: Photo URL (Instagram or Google Maps)
            author_name: Photo author

        Returns:
            MenuPhoto with S3 metadata, or None on error
        """
        try:
            response = await self._download_client.get(image_url)
            response.raise_for_status()
            photo_bytes = response.content

            content_type = response.headers.get("content-type", "image/jpeg")
            photo_id, s3_key, s3_url = await self.s3_client.upload_photo_bytes(
                venue_id=venue_id,
                photo_bytes=photo_bytes,
                content_type=content_type,
            )

            return MenuPhoto(
                photo_id=photo_id,
                s3_url=s3_url,
                s3_key=s3_key,
                source_url=image_url,
                author_name=author_name,
                downloaded_at=datetime.utcnow(),
            )

        except httpx.HTTPError as e:
            logger.error(
                f"[MenuPhotoEnrichment] Failed to download photo from {image_url}: {e}"
            )
            return None
