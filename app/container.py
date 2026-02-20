"""Dependency injection container for application components."""
import logging
from typing import Optional

import redis

from app.config import Settings
from app.db import GeoRedisClient
from app.dao import RedisVenueDAO
from app.api import BestTimeAPIClient
from app.api.google_places_client import GooglePlacesAPIClient
from app.services import VenueService, VenuesRefresherService
from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService
from app.services.photo_enrichment_service import PhotoEnrichmentService
from app.api.apify_instagram_client import ApifyInstagramClient
from app.services.instagram_enrichment_service import InstagramEnrichmentService
from app.services.instagram_posts_enrichment_service import InstagramPostsEnrichmentService
from app.services.instagram_validator import InstagramValidator
from app.api.s3_client import S3Client
from app.api.apify_instagram_highlights_client import ApifyInstagramHighlightsClient
from app.api.apify_gmaps_extractor_client import ApifyGMapsExtractorClient
from app.api.openai_menu_client import OpenAIMenuClient
from app.services.menu_photo_enrichment_service import MenuPhotoEnrichmentService
from app.services.menu_extraction_service import MenuExtractionService
from app.api.openai_vibe_client import OpenAIVibeClient
from app.services.vibe_classifier_service import VibeClassifierService
from app.handlers import VenueHandler

logger = logging.getLogger(__name__)


class Container:
    """Dependency injection container.

    Matches Go implementation: di/container.go
    Initializes and wires up all application dependencies.
    """

    def __init__(self, settings: Settings):
        """Initialize container with all dependencies.

        Args:
            settings: Application settings
        """
        logger.info(f"[Container] Initializing container")
        self.settings = settings

        # Global processing cap — applied to all enrichment services
        global_cap = settings.process_venue_total_limit  # -1 = disabled

        def _capped(service_limit: int) -> int:
            """Apply global process_venue_total_limit cap to a per-service limit."""
            if global_cap < 0:
                return service_limit  # global cap disabled
            if service_limit <= 0:
                return global_cap  # service has no limit, use global
            return min(service_limit, global_cap)

        if global_cap >= 0:
            logger.info(f"[Container] Global process_venue_total_limit={global_cap}")

        # Initialize Redis client
        logger.info(
            f"[Container] Connecting to Redis at {settings.redis_host}:{settings.redis_port}"
        )
        redis_internal_client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            db=settings.redis_db,
            decode_responses=True,
        )

        # Test Redis connection
        try:
            redis_internal_client.ping()
            logger.info("[Container] Redis connection successful")
        except Exception as e:
            logger.error(f"[Container] Failed to connect to Redis: {e}")
            raise

        # Initialize Redis client wrapper
        self.redis_client = GeoRedisClient(redis_internal_client)

        # Initialize Redis Venue DAO
        self.redis_venue_dao = RedisVenueDAO(self.redis_client)

        # Initialize BestTime API client
        self.besttime_api = BestTimeAPIClient(
            api_key_public=settings.besttime_public_key,
            api_key_private=settings.besttime_private_key,
            base_url=settings.besttime_endpoint_base_v1,
        )

        # Initialize Google Places API client (for enrichment and photos)
        self.google_places_api = None
        self.google_places_enrichment_service = None
        self.photo_enrichment_service = None

        if settings.google_places_api_key:
            self.google_places_api = GooglePlacesAPIClient(
                api_key=settings.google_places_api_key,
            )
            logger.info("[Container] Google Places API client initialized")

            # Initialize Google Places Enrichment service
            self.google_places_enrichment_service = GooglePlacesEnrichmentService(
                self.google_places_api,
                self.redis_venue_dao,
            )
            logger.info("[Container] Google Places Enrichment service initialized")

            # Initialize Photo Enrichment service
            self.photo_enrichment_service = PhotoEnrichmentService(
                self.google_places_api,
                self.redis_venue_dao,
                enrichment_limit=_capped(settings.photo_enrichment_limit),
            )
            logger.info("[Container] Photo Enrichment service initialized")
        else:
            logger.warning(
                "[Container] Google Places API key not configured. "
                "Google Places enrichment and photo features will be disabled."
            )

        # Initialize Apify Instagram client and enrichment service
        self.apify_instagram_client = None
        self.instagram_enrichment_service = None

        if settings.apify_api_token:
            self.apify_instagram_client = ApifyInstagramClient(
                api_token=settings.apify_api_token,
            )
            logger.info("[Container] Apify Instagram client initialized")

            validator = InstagramValidator(
                auto_accept_threshold=settings.instagram_auto_accept_threshold,
                low_confidence_threshold=settings.instagram_min_confidence,
            )

            self.instagram_enrichment_service = InstagramEnrichmentService(
                apify_client=self.apify_instagram_client,
                venue_dao=self.redis_venue_dao,
                validator=validator,
                search_candidates=settings.instagram_search_candidates,
                enrichment_limit=_capped(settings.instagram_enrichment_limit),
                cache_ttl_days=settings.instagram_cache_ttl_days,
                not_found_ttl_days=settings.instagram_not_found_cache_ttl_days,
            )
            logger.info("[Container] Instagram Enrichment service initialized")
        else:
            logger.warning(
                "[Container] Apify API token not configured. "
                "Instagram discovery will be disabled."
            )

        # Initialize Instagram Posts Enrichment (scrape post captions for vibe classifier)
        self.instagram_posts_enrichment_service = None
        if settings.apify_api_token and self.apify_instagram_client:
            self.instagram_posts_enrichment_service = InstagramPostsEnrichmentService(
                apify_client=self.apify_instagram_client,
                venue_dao=self.redis_venue_dao,
                enrichment_limit=_capped(settings.ig_posts_enrichment_limit),
                posts_per_venue=settings.ig_posts_per_venue,
                cache_ttl_days=settings.ig_posts_cache_ttl_days,
            )
            logger.info("[Container] Instagram Posts Enrichment service initialized")

        # Initialize Instagram Highlights client (for menu photo discovery from IG)
        self.apify_instagram_highlights_client = None
        if settings.apify_api_token:
            self.apify_instagram_highlights_client = ApifyInstagramHighlightsClient(
                api_token=settings.apify_api_token,
            )
            logger.info("[Container] Apify Instagram Highlights client initialized")

        # Initialize Google Maps Extractor client (fallback for menu photos)
        self.apify_gmaps_extractor_client = None
        if settings.apify_api_token:
            self.apify_gmaps_extractor_client = ApifyGMapsExtractorClient(
                api_token=settings.apify_api_token,
            )
            logger.info("[Container] Apify Google Maps Extractor client initialized")

        # Initialize Menu Photo Enrichment (needs: S3 + Apify token)
        self.s3_client = None
        self.menu_photo_enrichment_service = None
        self.openai_menu_client = None
        self.menu_extraction_service = None

        if settings.s3_bucket and settings.s3_access_key_id:
            self.s3_client = S3Client(
                bucket=settings.s3_bucket,
                region=settings.s3_region,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=settings.s3_secret_access_key,
            )
            logger.info("[Container] S3 client initialized")

            if settings.apify_api_token:
                self.menu_photo_enrichment_service = MenuPhotoEnrichmentService(
                    instagram_highlights_client=self.apify_instagram_highlights_client,
                    gmaps_extractor_client=(
                        self.apify_gmaps_extractor_client
                        if settings.menu_gmaps_fallback_enabled
                        else None
                    ),
                    s3_client=self.s3_client,
                    venue_dao=self.redis_venue_dao,
                    enrichment_limit=_capped(settings.menu_enrichment_limit),
                    photos_per_venue=settings.menu_photos_per_venue,
                    menu_categories=settings.menu_photo_categories,
                )
                logger.info(
                    "[Container] Menu Photo Enrichment service initialized "
                    "(Instagram highlights primary, Google Maps fallback)"
                )
            else:
                logger.warning(
                    "[Container] Menu Photo Enrichment disabled "
                    "(missing APIFY_API_TOKEN)"
                )
        else:
            logger.info(
                "[Container] Menu Photo Enrichment disabled "
                "(missing S3 bucket or S3 credentials)"
            )

        # Initialize Menu Extraction (needs: openai_api_key + s3_client for presigned URLs)
        if settings.openai_api_key and self.s3_client:
            self.openai_menu_client = OpenAIMenuClient(
                api_key=settings.openai_api_key,
                model=settings.menu_extraction_model,
            )
            self.menu_extraction_service = MenuExtractionService(
                openai_client=self.openai_menu_client,
                s3_client=self.s3_client,
                venue_dao=self.redis_venue_dao,
                extraction_model=settings.menu_extraction_model,
                photo_filter_enabled=settings.menu_photo_filter_enabled,
                photo_filter_confidence=settings.menu_photo_filter_confidence,
            )
            logger.info("[Container] OpenAI Menu client and Menu Extraction service initialized")
        else:
            logger.info(
                "[Container] Menu Extraction disabled "
                "(missing OpenAI API key or S3 client)"
            )

        # Initialize Vibe Classifier (needs: openai_api_key + photos in Redis)
        self.openai_vibe_client = None
        self.vibe_classifier_service = None

        if settings.openai_api_key:
            self.openai_vibe_client = OpenAIVibeClient(api_key=settings.openai_api_key)
            self.vibe_classifier_service = VibeClassifierService(
                openai_vibe_client=self.openai_vibe_client,
                venue_dao=self.redis_venue_dao,
                target_photos=settings.vibe_classifier_target_photos,
                escalation_threshold=settings.vibe_classifier_escalation_threshold,
                stage_b_photo_count=settings.vibe_classifier_stage_b_photos,
                enrichment_limit=_capped(settings.vibe_classifier_limit),
                early_stop_enabled=settings.vibe_classifier_early_stop_enabled,
                early_stop_min_photos=settings.vibe_classifier_early_stop_min_photos,
                early_stop_confidence=settings.vibe_classifier_early_stop_confidence,
                stage_a_model=settings.vibe_classifier_stage_a_model,
                stage_b_model=settings.vibe_classifier_stage_b_model,
            )
            logger.info("[Container] Vibe Classifier service initialized")
        else:
            logger.info(
                "[Container] Vibe Classifier disabled "
                "(missing OpenAI API key)"
            )

        # Initialize services
        self.venue_service = VenueService(self.redis_venue_dao, self.besttime_api)
        self.venues_refresher_service = VenuesRefresherService(
            self.redis_venue_dao,
            self.besttime_api,
            fetch_venue_limit_override=settings.fetch_venue_limit_override,
            fetch_venue_total_limit=settings.fetch_venue_total_limit,
            dev_mode=settings.dev_mode,
            dev_lat=settings.dev_lat,
            dev_lng=settings.dev_lng,
            dev_radius=settings.dev_radius,
        )

        # Initialize handlers
        self.venue_handler = VenueHandler(self.redis_venue_dao)

        logger.info("[Container] Container initialized successfully")

    async def shutdown(self):
        """Clean up resources on shutdown."""
        logger.info("[Container] Shutting down container")
        try:
            await self.besttime_api.close()
            logger.info("[Container] BestTime API client closed")
        except Exception as e:
            logger.error(f"[Container] Error closing BestTime API client: {e}")

        if self.google_places_api:
            try:
                await self.google_places_api.close()
                logger.info("[Container] Google Places API client closed")
            except Exception as e:
                logger.error(f"[Container] Error closing Google Places API client: {e}")

        if self.apify_instagram_client:
            try:
                await self.apify_instagram_client.close()
                logger.info("[Container] Apify Instagram client closed")
            except Exception as e:
                logger.error(f"[Container] Error closing Apify Instagram client: {e}")

        if self.apify_instagram_highlights_client:
            try:
                await self.apify_instagram_highlights_client.close()
                logger.info("[Container] Apify Instagram Highlights client closed")
            except Exception as e:
                logger.error(f"[Container] Error closing Apify Instagram Highlights client: {e}")

        if self.apify_gmaps_extractor_client:
            try:
                await self.apify_gmaps_extractor_client.close()
                logger.info("[Container] Apify Google Maps Extractor client closed")
            except Exception as e:
                logger.error(f"[Container] Error closing Apify Google Maps Extractor client: {e}")

        if self.menu_photo_enrichment_service:
            try:
                await self.menu_photo_enrichment_service.close()
                logger.info("[Container] Menu Photo Enrichment service closed")
            except Exception as e:
                logger.error(f"[Container] Error closing Menu Photo Enrichment service: {e}")

        if self.openai_menu_client:
            try:
                await self.openai_menu_client.close()
                logger.info("[Container] OpenAI Menu client closed")
            except Exception as e:
                logger.error(f"[Container] Error closing OpenAI Menu client: {e}")

        if self.openai_vibe_client:
            try:
                await self.openai_vibe_client.close()
                logger.info("[Container] OpenAI Vibe client closed")
            except Exception as e:
                logger.error(f"[Container] Error closing OpenAI Vibe client: {e}")
