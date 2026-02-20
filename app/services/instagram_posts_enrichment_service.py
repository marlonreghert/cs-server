"""Service for scraping recent Instagram posts for venues with known handles.

Feeds post captions into the vibe classifier pipeline as additional text context
alongside Google photos, Instagram bio, and Google reviews.

Follows the same pattern as InstagramEnrichmentService.
"""
import asyncio
import logging

from app.api.apify_instagram_client import ApifyInstagramClient, ApifyCreditExhaustedError
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.instagram import InstagramPost, VenueInstagramPosts

logger = logging.getLogger(__name__)

REQUEST_DELAY = 1.0


class InstagramPostsEnrichmentService:
    """Scrapes recent IG posts for venues with confirmed Instagram handles."""

    def __init__(
        self,
        apify_client: ApifyInstagramClient,
        venue_dao: RedisVenueDAO,
        enrichment_limit: int = 20,
        posts_per_venue: int = 10,
        cache_ttl_days: int = 30,
    ):
        self.apify_client = apify_client
        self.venue_dao = venue_dao
        self.enrichment_limit = enrichment_limit
        self.posts_per_venue = posts_per_venue
        self.cache_ttl_days = cache_ttl_days

    async def enrich_all_venues(self) -> int:
        """Scrape posts for all venues with IG handles but no cached posts.

        Returns:
            Number of venues successfully enriched.
        """
        all_venue_ids = self.venue_dao.list_all_venue_ids()
        venues_with_posts = set(self.venue_dao.list_cached_ig_posts_venue_ids())

        # Find venues that have an IG handle but no cached posts
        venues_to_process = []
        for vid in all_venue_ids:
            if vid in venues_with_posts:
                continue
            ig = self.venue_dao.get_venue_instagram(vid)
            if ig and ig.has_instagram():
                venues_to_process.append((vid, ig.instagram_handle))

        # Apply limit
        if self.enrichment_limit > 0:
            venues_to_process = venues_to_process[:self.enrichment_limit]

        logger.info(
            f"[IGPostsEnrichment] Starting enrichment for "
            f"{len(venues_to_process)} venues "
            f"(total={len(all_venue_ids)}, already_cached={len(venues_with_posts)})"
        )

        if not venues_to_process:
            logger.info("[IGPostsEnrichment] No venues need post scraping")
            return 0

        successful = 0
        for venue_id, handle in venues_to_process:
            try:
                raw_posts = await self.apify_client.fetch_recent_posts(
                    handle, results_limit=self.posts_per_venue
                )

                ig_posts = VenueInstagramPosts(
                    venue_id=venue_id,
                    instagram_handle=handle,
                    posts=[InstagramPost(**p) for p in raw_posts],
                )
                self.venue_dao.set_venue_ig_posts(
                    ig_posts, cache_ttl_days=self.cache_ttl_days
                )
                successful += 1

                logger.info(
                    f"[IGPostsEnrichment] Cached {len(ig_posts.posts)} posts "
                    f"for {venue_id} (@{handle})"
                )

            except ApifyCreditExhaustedError:
                logger.error(
                    "[IGPostsEnrichment] Apify credits exhausted, stopping"
                )
                break
            except Exception as e:
                logger.error(
                    f"[IGPostsEnrichment] Error scraping posts for "
                    f"{venue_id} (@{handle}): {e}"
                )

            await asyncio.sleep(REQUEST_DELAY)

        logger.info(
            f"[IGPostsEnrichment] Enrichment complete: "
            f"{successful}/{len(venues_to_process)} venues enriched"
        )
        return successful
