"""Service for fetching venue photos from Google Maps and storing them on S3.

Uses SerpApi (primary) to discover menu-specific photos via Google Maps
photo categories, with Apify as a fallback.

For each venue:
1. Look up Google Place ID (reuses google_places_client.search_place_id)
2. Resolve SerpApi data_id from place_id
3. Fetch photos with menu-category filtering (SerpApi)
4. On SerpApi failure: fallback to Apify actor
5. Download photos and upload to S3
6. Store metadata in Redis

Follows the same pattern as instagram_enrichment_service.py.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx

from app.api.serpapi_client import SerpApiClient
from app.api.apify_menu_photos_client import ApifyMenuPhotosClient
from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.api.google_places_client import GooglePlacesAPIClient
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
    """Fetches and caches venue photos from Google Maps.

    Primary: SerpApi with menu-category filtering.
    Fallback: Apify thescrappa/google-maps-photos-scraper.
    """

    def __init__(
        self,
        serpapi_client: SerpApiClient,
        s3_client: S3Client,
        venue_dao: RedisVenueDAO,
        google_places_client: GooglePlacesAPIClient,
        apify_client: Optional[ApifyMenuPhotosClient] = None,
        enrichment_limit: int = 10,
        photos_per_venue: int = 20,
        menu_categories: Optional[list[str]] = None,
    ):
        self.serpapi_client = serpapi_client
        self.s3_client = s3_client
        self.venue_dao = venue_dao
        self.google_places_client = google_places_client
        self.apify_client = apify_client
        self.enrichment_limit = enrichment_limit
        self.photos_per_venue = photos_per_venue
        self.menu_categories = menu_categories or DEFAULT_MENU_CATEGORIES
        self._download_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def close(self):
        """Close internal HTTP client."""
        await self._download_client.aclose()

    async def enrich_venue(self, venue_id: str, force_refresh: bool = False) -> Optional[VenueMenuPhotos]:
        """Fetch and store photos for a single venue.

        Tries SerpApi first (with menu-category filtering), falls back to Apify.

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

        # Get Google Place ID
        google_place_id = await self.google_places_client.search_place_id(
            venue_name=venue.venue_name,
            venue_address=venue.venue_address,
            lat=venue.venue_lat,
            lng=venue.venue_lng,
        )
        if not google_place_id:
            logger.warning(
                f"[MenuPhotoEnrichment] No Place ID for {venue.venue_name}"
            )
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="no_place_id").inc()
            return None

        # Try SerpApi (primary)
        photo_data = await self._fetch_via_serpapi(google_place_id, venue.venue_name)

        # Fallback to Apify if SerpApi failed
        if photo_data is None and self.apify_client:
            logger.info(
                f"[MenuPhotoEnrichment] SerpApi failed for {venue.venue_name}, "
                f"trying Apify fallback"
            )
            photo_data = await self._fetch_via_apify(google_place_id, venue.venue_name)

        if not photo_data or not photo_data.get("photos"):
            logger.info(
                f"[MenuPhotoEnrichment] No photos found for {venue.venue_name}"
            )
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="no_photos_found").inc()
            result = VenueMenuPhotos(venue_id=venue_id)
            self.venue_dao.set_venue_menu_photos(result)
            return result

        photos_list = photo_data["photos"]
        categories = photo_data.get("categories", [])
        has_menu_category = photo_data.get("has_menu_category", False)

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
        )
        self.venue_dao.set_venue_menu_photos(result)

        if all_photos:
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="enriched").inc()
            MENU_PHOTOS_STORED_TOTAL.inc(len(all_photos))
            logger.info(
                f"[MenuPhotoEnrichment] Stored {len(all_photos)} photos for "
                f"{venue.venue_name}"
            )
        else:
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="no_photos_found").inc()
            logger.info(
                f"[MenuPhotoEnrichment] No photos downloaded for {venue.venue_name}"
            )

        return result

    async def _fetch_via_serpapi(
        self, place_id: str, venue_name: str
    ) -> Optional[dict]:
        """Fetch photos via SearchApi.io with menu-category filtering.

        Uses place_id directly (no data_id resolution needed).

        Returns:
            Dict with keys: photos (list of {image_url, author_name}),
            categories (list of str), has_menu_category (bool),
            or None on failure.
        """
        try:
            # Step 1: Fetch photos + categories using place_id directly
            result = await self.serpapi_client.fetch_photos(place_id=place_id)
            if not result:
                return None

            categories = result.get("categories", [])
            category_titles = [c.get("title", "") for c in categories]

            # Step 2: Check for menu category
            menu_category_id = self.serpapi_client.find_menu_category(
                categories, self.menu_categories
            )

            has_menu_category = menu_category_id is not None
            photos = result.get("photos", [])

            # If menu category found, re-fetch with category filter
            if menu_category_id:
                filtered_result = await self.serpapi_client.fetch_photos(
                    place_id=place_id, category_id=menu_category_id
                )
                if filtered_result and filtered_result.get("photos"):
                    photos = filtered_result["photos"]
                    logger.info(
                        f"[MenuPhotoEnrichment] SearchApi: {len(photos)} menu-category "
                        f"photos for {venue_name}"
                    )
            else:
                logger.info(
                    f"[MenuPhotoEnrichment] SearchApi: no menu category for {venue_name}, "
                    f"using {len(photos)} unfiltered photos. "
                    f"Available categories: {category_titles}"
                )

            if not photos:
                return None

            # Normalize to common format
            normalized = [
                {
                    "image_url": p.get("image", p.get("thumbnail", "")),
                    "author_name": (p.get("user") or {}).get("name"),
                }
                for p in photos
            ]

            return {
                "photos": normalized,
                "categories": category_titles,
                "has_menu_category": has_menu_category,
            }

        except Exception as e:
            logger.error(
                f"[MenuPhotoEnrichment] SearchApi error for {venue_name}: {e}"
            )
            return None

    async def _fetch_via_apify(
        self, place_id: str, venue_name: str
    ) -> Optional[dict]:
        """Fetch photos via Apify fallback.

        Returns:
            Dict with keys: photos (list of {image_url, author_name}),
            categories (list), has_menu_category (bool),
            or None on failure.
        """
        try:
            photos = await self.apify_client.fetch_venue_photos(
                place_id=place_id,
                max_images=self.photos_per_venue,
            )

            if not photos:
                return None

            # Normalize to common format
            normalized = [
                {
                    "image_url": p.get("photo_url", ""),
                    "author_name": None,
                }
                for p in photos
            ]

            return {
                "photos": normalized,
                "categories": [],
                "has_menu_category": False,
            }

        except ApifyCreditExhaustedError:
            logger.error(
                f"[MenuPhotoEnrichment] Apify credits exhausted for {venue_name}"
            )
            MENU_PHOTO_ENRICHMENT_RESULTS.labels(result="credit_exhausted").inc()
            return None

        except Exception as e:
            logger.error(
                f"[MenuPhotoEnrichment] Apify error for {venue_name}: {e}"
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
            image_url: Photo URL (SerpApi or Apify)
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
