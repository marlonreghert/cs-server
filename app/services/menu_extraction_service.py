"""Service for extracting structured menu data from photos using OpenAI GPT-4o.

Independent from photo fetching — operates on photos already stored in S3.
For each venue with menu photos:
1. Read photo S3 keys from Redis
2. Generate presigned URLs for temporary access
3. Send to GPT-4o vision for extraction
4. Store structured menu data in Redis
"""
import asyncio
import logging

from app.api.openai_menu_client import OpenAIMenuClient
from app.api.s3_client import S3Client
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.menu import VenueMenuData
from app.metrics import (
    MENU_EXTRACTION_RESULTS,
    MENU_VENUES_WITH_DATA,
    MENU_ITEMS_EXTRACTED_TOTAL,
)

logger = logging.getLogger(__name__)

# Rate limiting for OpenAI calls
REQUEST_DELAY = 0.5  # 2 req/sec


def selectable_menu_photos(entries: list[dict]) -> list[dict]:
    """Archived menu photos worth paying an extractor to read.

    A photo the classifier marked illegible has no text in it to extract, so
    sending it to GPT-4o is pure waste — this is where the `legible` attribute
    pays for the classifier that produced it.

    Only a confident `no` drops a photo. An entry with no legibility verdict at
    all, or one the classifier answered `not_classified`, is KEPT: an archive
    from before the classifier existed must behave exactly as it did before,
    and "could not tell" is not "unreadable".
    """
    return [
        entry for entry in entries
        if (entry.get("attributes") or {}).get("legible") != "no"
    ]


class MenuExtractionService:
    """Extracts structured menu data from stored menu photos."""

    def __init__(
        self,
        openai_client: OpenAIMenuClient,
        s3_client: S3Client,
        venue_dao: RedisVenueDAO,
        extraction_model: str = "gpt-5.6-luna",
        photo_filter_enabled: bool = True,
        photo_filter_confidence: float = 0.6,
        media_store=None,
        photo_source: str = "archive",
        archive_source: str = "searchapi_gmaps_photos",
        archive_category: str = "menu",
        presign_seconds: int = 900,
    ):
        self.openai_client = openai_client
        self.s3_client = s3_client
        self.venue_dao = venue_dao
        self.extraction_model = extraction_model
        self.photo_filter_enabled = photo_filter_enabled
        self.photo_filter_confidence = photo_filter_confidence
        # Reading the archive instead of a second private copy of the same
        # photos. `redis` keeps the original path selectable without a deploy.
        self.media_store = media_store
        self.photo_source = photo_source
        self.archive_source = archive_source
        self.archive_category = archive_category
        self.presign_seconds = presign_seconds

    async def _photo_urls(self, venue_id: str):
        """Presigned urls for this venue's menu photos, their ids, and whether
        they still need the pre-filter.

        Returns (None, None, False) when the venue has no photos — a normal
        outcome, not a failure. A photo that cannot be signed is skipped so it
        never costs the venue its other photos.
        """
        if self.photo_source == "archive":
            return await self._archive_photo_urls(venue_id)
        return await self._redis_photo_urls(venue_id)

    async def _archive_photo_urls(self, venue_id: str):
        """The NEWEST archive run's photos for the configured category.

        Deliberately the newest run only: a run is a snapshot, so falling back
        to an older one would silently pair a fresh menu with a stale photo.

        Never pre-filtered: these photos are already in the `menu` category by
        construction, and the manifest has told us which of them are legible —
        a second GPT pass to re-decide "is this a menu" would pay twice for an
        answer we hold.
        """
        if self.media_store is None:
            logger.error("[MenuExtraction] archive source selected but no media store")
            return None, None, False
        prefix = await self.media_store.latest_run_prefix(self.archive_source)
        if not prefix:
            logger.info(
                f"[MenuExtraction] no {self.archive_source} run to read for {venue_id}"
            )
            return None, None, False
        keys = await self.media_store.list_venue_photos(
            prefix, venue_id, self.archive_category
        )
        if not keys:
            logger.debug(
                f"[MenuExtraction] {venue_id} absent from the newest "
                f"{self.archive_source} run"
            )
            return None, None, False
        keys = await self._drop_illegible(prefix, venue_id, keys)
        if not keys:
            logger.info(f"[MenuExtraction] every menu photo for {venue_id} is illegible")
            return None, None, False

        urls, ids = [], []
        for key in keys:
            url = await self.media_store.presign(key, self.presign_seconds)
            if url:
                urls.append(url)
                ids.append(key.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        if not urls:
            # Every signature failed — almost always the IAM grant, so say which.
            logger.error(
                f"[MenuExtraction] could not sign any archived photo for "
                f"{venue_id}; check s3:GetObject on retrieved/* for this role"
            )
            return None, None, False
        logger.info(
            f"[MenuExtraction] {venue_id}: {len(urls)} {self.archive_category} "
            f"photo(s) from the newest {self.archive_source} run"
        )
        return urls, ids, False

    async def _drop_illegible(self, prefix: str, venue_id: str, keys: list[str]):
        """Remove the photos the classifier said have no readable text.

        Only what the manifest POSITIVELY marks illegible is dropped. A key the
        manifest does not mention — an unclassified run, a photo added later —
        is kept, so this can only ever narrow a known-bad set, never silently
        lose a menu.
        """
        manifest = await self.media_store.read_manifest(prefix, venue_id)
        entries = (manifest or {}).get("photos") or []
        if not entries:
            return keys
        known = {e.get("key") for e in entries}
        keep = {e.get("key") for e in selectable_menu_photos(entries)}
        selected = [k for k in keys if k not in known or k in keep]
        if len(selected) < len(keys):
            logger.info(
                f"[MenuExtraction] {venue_id}: skipped {len(keys) - len(selected)} "
                f"illegible menu photo(s) before paying for extraction"
            )
        return selected

    async def _redis_photo_urls(self, venue_id: str):
        """The original path: photos the menu_photos job put in its own bucket."""
        menu_photos = self.venue_dao.get_venue_menu_photos(venue_id)
        if menu_photos is None or not menu_photos.has_photos():
            logger.debug(f"[MenuExtraction] No menu photos for {venue_id}")
            return None, None, False
        urls, ids = [], []
        for photo in menu_photos.photos:
            try:
                urls.append(await self.s3_client.generate_presigned_url(photo.s3_key))
                ids.append(photo.photo_id)
            except Exception as e:
                logger.error(
                    f"[MenuExtraction] Failed to generate presigned URL for "
                    f"{photo.s3_key}: {e}"
                )
        # These are unfiltered: they may be any photo the venue had, so the
        # pre-filter still has to decide which of them are menus — except for
        # the two sources that were already filtered upstream.
        needs_filter = menu_photos.source not in ("instagram_highlights", "gmaps_extractor")
        return (urls, ids, needs_filter) if urls else (None, None, False)

    async def extract_menu_for_venue(
        self, venue_id: str, force_refresh: bool = False
    ) -> VenueMenuData | None:
        """Extract menu data for a single venue from its cached photos.

        Args:
            venue_id: Internal venue ID
            force_refresh: If True, re-extract even if cached

        Returns:
            VenueMenuData result, or None on error
        """
        # Check cache
        if not force_refresh:
            existing = self.venue_dao.get_venue_menu_data(venue_id)
            if existing is not None:
                logger.debug(f"[MenuExtraction] Cache hit for {venue_id}")
                MENU_EXTRACTION_RESULTS.labels(result="cached").inc()
                return existing

        # Where the photos come from is a seam, so the archive path can be
        # switched back to the Redis one by config rather than a deploy.
        presigned_urls, photo_ids, needs_filter = await self._photo_urls(venue_id)
        if presigned_urls is None:
            MENU_EXTRACTION_RESULTS.labels(result="no_photos").inc()
            return None

        if not presigned_urls:
            logger.error(f"[MenuExtraction] No presigned URLs generated for {venue_id}")
            MENU_EXTRACTION_RESULTS.labels(result="error").inc()
            return None

        # Pre-filter: classify which photos are menus using GPT-4o-mini.
        # `needs_filter` comes from whoever supplied the photos — the archive
        # path has already filtered by category and legibility, and the Redis
        # path knows which of ITS sources were pre-filtered upstream. Reading
        # that flag from the supplier is also the fix for a NameError: this
        # condition used to reference `menu_photos`, a local of the Redis
        # branch, which does not exist on the archive path.
        if self.photo_filter_enabled and len(presigned_urls) > 1 and needs_filter:
            try:
                menu_indices = await self.openai_client.classify_menu_photos(
                    presigned_urls,
                    confidence_threshold=self.photo_filter_confidence,
                )
                if len(menu_indices) < len(presigned_urls):
                    logger.info(
                        f"[MenuExtraction] Pre-filter: {len(menu_indices)}/{len(presigned_urls)} "
                        f"photos classified as menus for {venue_id}"
                    )
                    presigned_urls = [presigned_urls[i] for i in menu_indices]
                    photo_ids = [photo_ids[i] for i in menu_indices]

                if not presigned_urls:
                    logger.info(
                        f"[MenuExtraction] No menu photos after filtering for {venue_id}"
                    )
                    MENU_EXTRACTION_RESULTS.labels(result="no_menu_photos_after_filter").inc()
                    result = VenueMenuData(
                        venue_id=venue_id, extraction_model=self.extraction_model
                    )
                    self.venue_dao.set_venue_menu_data(result)
                    return result
            except Exception as e:
                logger.warning(
                    f"[MenuExtraction] Pre-filter failed for {venue_id}, "
                    f"proceeding with all photos: {e}"
                )

        # Call OpenAI GPT-4o vision
        try:
            sections, currency, raw_response = await self.openai_client.extract_menu_from_photos(
                presigned_urls
            )
        except Exception as e:
            logger.error(f"[MenuExtraction] OpenAI extraction failed for {venue_id}: {e}")
            MENU_EXTRACTION_RESULTS.labels(result="error").inc()
            return None

        # Build and cache result
        total_items = sum(len(s.items) for s in sections)
        result = VenueMenuData(
            venue_id=venue_id,
            sections=sections,
            currency_detected=currency,
            source_photo_ids=photo_ids,
            extraction_model=self.extraction_model,
            raw_response=raw_response,
        )

        self.venue_dao.set_venue_menu_data(result)
        MENU_EXTRACTION_RESULTS.labels(result="extracted").inc()
        MENU_ITEMS_EXTRACTED_TOTAL.inc(total_items)

        logger.info(
            f"[MenuExtraction] Extracted {len(sections)} sections, "
            f"{total_items} items for {venue_id}"
        )

        return result

    async def extract_all_venues(self, force_refresh: bool = False) -> int:
        """Extract menu data for all venues that have menu photos.

        Only processes venues that have menu photos but no extracted data yet.

        Args:
            force_refresh: If True, re-extract even if cached

        Returns:
            Number of venues successfully extracted
        """
        # Gate on the serving view (active AND eligible): ineligible venues are
        # excluded so junk never burns OpenAI budget; unlabeled venues stay in scope.
        active_venue_ids = set(self.venue_dao.list_servable_venue_ids())
        photo_venue_ids = [
            venue_id
            for venue_id in self.venue_dao.list_cached_menu_photos_venue_ids()
            if venue_id in active_venue_ids
        ]
        logger.info(
            f"[MenuExtraction] Starting extraction for "
            f"{len(photo_venue_ids)} venues with menu photos"
        )

        if not photo_venue_ids:
            logger.info("[MenuExtraction] No venues with menu photos found")
            return 0

        extracted_count = 0
        skipped = 0
        errors = 0

        for venue_id in photo_venue_ids:
            # Check if already extracted
            if not force_refresh:
                existing = self.venue_dao.get_venue_menu_data(venue_id)
                if existing is not None:
                    extracted_count += 1
                    skipped += 1
                    continue

            try:
                result = await self.extract_menu_for_venue(venue_id, force_refresh=True)
                if result and len(result.sections) > 0:
                    extracted_count += 1
            except Exception as e:
                logger.error(f"[MenuExtraction] Error processing {venue_id}: {e}")
                MENU_EXTRACTION_RESULTS.labels(result="error").inc()
                errors += 1

            # Rate limiting
            await asyncio.sleep(REQUEST_DELAY)

        # Update metrics
        MENU_VENUES_WITH_DATA.set(extracted_count)

        logger.info(
            f"[MenuExtraction] Extraction complete: "
            f"{extracted_count} extracted, {skipped} skipped (cached), "
            f"{errors} errors"
        )

        return extracted_count
