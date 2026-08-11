"""Dependency injection container for application components."""
import logging
from typing import Optional

import redis

from app.config import Settings
from app.db import GeoRedisClient
from app.dao import RedisVenueDAO, VenueBudgetDao
from app.dao.venue_repository import VenueRepository
from app.dao.datalake_writer import DatalakeWriter
from app.dao.media_archive_store import MediaArchiveStore
from app.api import BestTimeAPIClient
from app.api.google_places_client import GooglePlacesAPIClient
from app.services import VenuesRefresherService, VenueBudgetService
from app.handlers import AddVenueHandler
from app.services.batch_add_service import BatchAddService
from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService
from app.services.photo_classification_service import PhotoClassificationService
from app.services.photo_enrichment_service import PhotoEnrichmentService
from app.services.venue_photo_archive_service import VenuePhotoArchiveService
from app.api.apify_instagram_client import ApifyInstagramClient
from app.services.instagram_enrichment_service import InstagramEnrichmentService
from app.services.instagram_posts_enrichment_service import InstagramPostsEnrichmentService
from app.services.instagram_validator import InstagramValidator
from app.api.s3_client import S3Client
from app.api.apify_instagram_highlights_client import ApifyInstagramHighlightsClient
from app.api.apify_gmaps_extractor_client import ApifyGMapsExtractorClient
from app.api.openai_menu_client import OpenAIMenuClient
from app.api.openai_photo_classifier_client import OpenAIPhotoClassifierClient
from app.services.menu_photo_enrichment_service import MenuPhotoEnrichmentService
from app.services.menu_extraction_service import MenuExtractionService
from app.api.openai_vibe_client import OpenAIVibeClient
from app.services.vibe_classifier_service import VibeClassifierService
from app.handlers import VenueHandler
from app.services.engagement_service import EngagementService
from app.services.redis_projection_service import RedisProjectionService

logger = logging.getLogger(__name__)


class Container:
    """Dependency injection container.

    Matches Go implementation: di/container.go
    Initializes and wires up all application dependencies.
    """


    def _build_google_search_source(self):
        """The Google-search tier, or None. Opt-in and dependency-aware."""
        if not self.settings.instagram_google_search_enabled:
            return None
        if not self.settings.apify_api_token:
            logger.warning(
                "[Container] Google search tier enabled but no Apify token; "
                "venues with no web presence stay unreachable"
            )
            return None
        from app.api.apify_google_search_client import ApifyGoogleSearchClient
        from app.services.instagram_cascade_adapters import GoogleSearchInstagramSource

        logger.info("[Container] Instagram Google-search tier initialized")
        return GoogleSearchInstagramSource(
            self.pipeline_repository,
            search_client=ApifyGoogleSearchClient(
                self.settings.apify_api_token,
                actor=self.settings.instagram_google_search_actor,
                country_code=self.settings.instagram_google_search_country,
            ),
            results=self.settings.instagram_google_search_results,
        )

    def _build_instagram_judge(self):
        """The judge, or None. Opt-in and dependency-aware, like every other
        optional enrichment path: no key or not enabled means the cascade runs
        exactly as before rather than failing to start."""
        from app.services.instagram_judge import InstagramJudge

        if not self.settings.instagram_judge_enabled:
            logger.info("[Container] Instagram judge disabled")
            return None
        if not self.settings.openai_api_key:
            logger.warning(
                "[Container] Instagram judge enabled but no OpenAI key is "
                "configured; ambiguous candidates will go unadjudicated"
            )
            return None
        from app.api.openai_instagram_judge_client import OpenAIInstagramJudgeClient

        logger.info(
            f"[Container] Instagram judge initialized (model="
            f"{self.settings.instagram_judge_model})"
        )
        return InstagramJudge(
            OpenAIInstagramJudgeClient(self.settings.openai_api_key),
            model=self.settings.instagram_judge_model,
            max_photos=self.settings.instagram_judge_max_venue_photos,
        )

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

        # Redis-only DAO used by the projection/rebuild path (writes Redis only,
        # never RDS) so a rebuild does not re-write the system of record.
        self.serving_redis_dao = RedisVenueDAO(self.redis_client)

        # RDS system-of-record store. RDS is the durable truth for all venue +
        # admin data; Redis is the serving/geo projection.
        from app.dao.rds_venue_store import RdsVenueStore

        try:
            self.rds_store = RdsVenueStore(settings.rds_sqlalchemy_url)
            logger.info("[Container] RDS system-of-record initialized")
        except Exception as e:
            logger.error(f"[Container] Failed to init RDS store: {e}")
            raise
        # Pipelines receive this as their venue DAO: it reads its data inputs and
        # cache-freshness gating from RDS (truth) and writes RDS-only — the
        # scheduled projector is the sole Redis writer for pipeline data. Geo reads
        # stay on Redis. Serving uses serving_redis_dao.
        self.pipeline_repository = VenueRepository(
            self.redis_client,
            rds_store=self.rds_store,
        )

        # Data lake writer (raw BestTime responses -> S3). Built BEFORE the
        # BestTime client so it can be handed in. Optional and inert unless
        # explicitly enabled: the client treats a None writer as a no-op, so
        # nothing about ingestion changes while the lake is off.
        self.datalake_writer = None
        if settings.datalake_enabled and settings.datalake_bucket:
            self.datalake_writer = DatalakeWriter(
                bucket=settings.datalake_bucket,
                region=settings.datalake_region,
                queue_maxsize=settings.datalake_queue_maxsize,
                flush_max_bytes=settings.datalake_flush_max_bytes,
                flush_max_seconds=settings.datalake_flush_max_seconds,
                shutdown_flush_seconds=settings.datalake_shutdown_flush_seconds,
                access_key_id=settings.datalake_access_key_id or None,
                secret_access_key=settings.datalake_secret_access_key or None,
            )
            logger.info(
                f"[Container] Data lake writer initialized "
                f"(bucket={settings.datalake_bucket})"
            )
        elif settings.datalake_enabled:
            logger.warning(
                "[Container] Data lake enabled but no bucket configured; "
                "raw BestTime responses will NOT be archived"
            )

        # Initialize BestTime API client
        self.besttime_api = BestTimeAPIClient(
            api_key_public=settings.besttime_public_key,
            api_key_private=settings.besttime_private_key,
            base_url=settings.besttime_endpoint_base_v1,
            add_venue_timeout=settings.besttime_add_venue_timeout_seconds,
            search_rate_per_minute=settings.besttime_search_rate_per_minute,
            search_rate_per_hour=settings.besttime_search_rate_per_hour,
            rate_max_wait_seconds=settings.besttime_rate_max_wait_seconds,
            datalake=self.datalake_writer,
        )

        # Initialize Google Places API client (for enrichment and photos)
        self.google_places_api = None
        self.google_places_enrichment_service = None
        self.photo_enrichment_service = None
        self.media_archive_store = None
        self.venue_photo_archive_service = None
        self.openai_photo_classifier_client = None
        self.photo_classification_service = None
        self.instagram_cascade_service = None

        # SearchApi photo client — the category-aware source. Built before the
        # archive service, which is constructed below.
        self.searchapi_photos_client = None
        if settings.searchapi_api_key:
            from app.api.serpapi_client import SerpApiClient
            self.searchapi_photos_client = SerpApiClient(
                api_key=settings.searchapi_api_key,
            )
            logger.info("[Container] SearchApi photo client initialized")

        # Google Maps Extractor client. Built BEFORE its consumers: it backs the
        # menu-photo fallback AND the archive's apify_gmaps_extractor source,
        # and the archive service is constructed below.
        self.apify_gmaps_extractor_client = None
        if settings.apify_api_token:
            self.apify_gmaps_extractor_client = ApifyGMapsExtractorClient(
                api_token=settings.apify_api_token,
                poll_continuation_seconds=settings.apify_poll_continuation_seconds,
            )
            logger.info("[Container] Apify Google Maps Extractor client initialized")

        if settings.google_places_api_key:
            self.google_places_api = GooglePlacesAPIClient(
                api_key=settings.google_places_api_key,
            )
            logger.info("[Container] Google Places API client initialized")

            # Initialize Google Places Enrichment service
            self.google_places_enrichment_service = GooglePlacesEnrichmentService(
                self.google_places_api,
                self.pipeline_repository,
            )
            logger.info("[Container] Google Places Enrichment service initialized")

            # Initialize Photo Enrichment service. On-demand resolution reads the
            # google_place_id from the RDS system of record (pipeline_repository) with
            # a Redis fallback (serving_redis_dao), and writes the fresh keyless-URL
            # cache to Redis (the fresh key is Redis-only, so the RDS repository's
            # inherited base method lands it in Redis — cs-server sole writer).
            self.photo_enrichment_service = PhotoEnrichmentService(
                self.google_places_api,
                self.pipeline_repository,
                enrichment_limit=_capped(settings.photo_enrichment_limit),
                serving_dao=self.serving_redis_dao,
            )
            logger.info("[Container] Photo Enrichment service initialized")

            # Venue photo archive (Google photos -> S3 media/ prefix). Optional:
            # needs Google Places (above) plus a bucket. Defaults to the data
            # lake bucket so the media archive lives beside the raw lake under
            # one Terraform-managed, instance-role-authenticated bucket.
            archive_bucket = (
                settings.media_archive_bucket or settings.datalake_bucket
            )
            if settings.media_archive_enabled and archive_bucket:
                # Per-photo classification. Optional in the strict sense: with no
                # OpenAI key (or switched off) the classifier stays None and the
                # archive runs exactly as it did before, photos keeping whatever
                # category their source gave them.
                self._init_photo_classifier(settings)
                self.media_archive_store = MediaArchiveStore(
                    bucket=archive_bucket,
                    region=settings.datalake_region,
                    access_key_id=settings.datalake_access_key_id or None,
                    secret_access_key=settings.datalake_secret_access_key or None,
                )
                self.venue_photo_archive_service = VenuePhotoArchiveService(
                    google_places_client=self.google_places_api,
                    venue_dao=self.pipeline_repository,
                    media_store=self.media_archive_store,
                    max_photos_per_venue=settings.media_archive_default_max_photos,
                    photo_timeout_seconds=settings.media_archive_photo_timeout_seconds,
                    max_photo_bytes=settings.media_archive_max_photo_bytes,
                    default_max_venues=settings.media_archive_default_max_venues,
                    rate_per_second=settings.media_archive_rate_per_second,
                    concurrency=settings.media_archive_concurrency,
                    max_retries=settings.media_archive_max_retries,
                    cost_per_1k_usd=settings.google_photo_cost_per_1k_usd,
                    # Second archive source. Left None when APIFY_API_TOKEN is
                    # unset, which the source catalog reports as unavailable
                    # rather than failing a run.
                    apify_gmaps_extractor_client=getattr(
                        self, "apify_gmaps_extractor_client", None
                    ),
                    searchapi_photos_client=getattr(
                        self, "searchapi_photos_client", None
                    ),
                    photo_classifier=self.photo_classification_service,
                    settings=settings,
                )
                logger.info(
                    f"[Container] Venue photo archive initialized "
                    f"(bucket={archive_bucket})"
                )
            elif settings.media_archive_enabled:
                logger.warning(
                    "[Container] Media archive enabled but no bucket configured; "
                    "the venue photo archive job stays unavailable"
                )
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

            # Third archive source. Wired here rather than alongside the other
            # two above because this client does not exist yet at that point in
            # startup — the archive service's attribute name matches
            # `instagram_posts`'s `requires_attr`, so the source catalog and the
            # dispatcher both resolve it by attribute the moment it appears.
            if getattr(self, "venue_photo_archive_service", None) is not None:
                self.venue_photo_archive_service.apify_instagram_client = (
                    self.apify_instagram_client
                )

            validator = InstagramValidator(
                auto_accept_threshold=settings.instagram_auto_accept_threshold,
                low_confidence_threshold=settings.instagram_min_confidence,
            )

            self.instagram_enrichment_service = InstagramEnrichmentService(
                apify_client=self.apify_instagram_client,
                venue_dao=self.pipeline_repository,
                validator=validator,
                search_candidates=settings.instagram_search_candidates,
                enrichment_limit=_capped(settings.instagram_enrichment_limit),
                cache_ttl_days=settings.instagram_cache_ttl_days,
                not_found_ttl_days=settings.instagram_not_found_cache_ttl_days,
            )
            logger.info("[Container] Instagram Enrichment service initialized")

            # Handle cascade: two free sources before the paid one, each
            # candidate verified against the real profile. The paid tier is the
            # SAME Apify client above — the cascade just reaches it last.
            if settings.instagram_cascade_enabled:
                from app.api.instagram_profile_probe import InstagramProfileProbe
                from app.services.instagram_cascade_adapters import (
                    ApifySearchSource,
                    ArchivedGmapsWebsiteSource,
                    ArchivedVenuePhotoSource,
                    GoogleListingWebsiteSource,
    VenueWebsiteScrapeSource,
)
                from app.services.instagram_cascade_service import InstagramCascadeService
                from app.services.instagram_enrichment_service import (
                    InstagramEnrichmentService as _IES,
                )

                probe = (
                    InstagramProfileProbe(
                        user_agent=settings.instagram_probe_user_agent,
                        timeout_seconds=settings.instagram_profile_probe_timeout_seconds,
                    )
                    if settings.instagram_profile_probe_enabled
                    else None
                )
                self.instagram_cascade_service = InstagramCascadeService(
                    venue_dao=self.pipeline_repository,
                    google_listing=GoogleListingWebsiteSource(self.pipeline_repository),
                    google_search=self._build_google_search_source(),
                    venue_website=VenueWebsiteScrapeSource(
                        self.pipeline_repository,
                        timeout_seconds=settings.instagram_website_timeout_seconds,
                        max_bytes=settings.instagram_website_max_bytes,
                    ),
                    archive=ArchivedGmapsWebsiteSource(self.media_archive_store),
                    paid_search=ApifySearchSource(
                        self.apify_instagram_client,
                        city_resolver=_IES._extract_city,
                        candidates=settings.instagram_search_candidates,
                    ),
                    probe=probe,
                    judge=self._build_instagram_judge(),
                    photo_archive=ArchivedVenuePhotoSource(self.media_archive_store),
                    accept_threshold=settings.instagram_auto_accept_threshold,
                    ambiguous_low=settings.instagram_min_confidence,
                    judge_floor=settings.instagram_judge_floor,
                )
                logger.info("[Container] Instagram handle cascade initialized")
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
                venue_dao=self.pipeline_repository,
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
                    venue_dao=self.pipeline_repository,
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
                venue_dao=self.pipeline_repository,
                extraction_model=settings.menu_extraction_model,
                photo_filter_enabled=settings.menu_photo_filter_enabled,
                photo_filter_confidence=settings.menu_photo_filter_confidence,
                # Reads the retrieval pipeline's archive rather than a second
                # copy of the same photos. None when the media archive is not
                # configured, which the service reports rather than crashing.
                media_store=getattr(self, "media_archive_store", None),
                photo_source=settings.menu_extraction_photo_source,
                archive_source=settings.menu_extraction_archive_source,
                archive_category=settings.menu_extraction_archive_category,
                presign_seconds=settings.menu_photo_presign_seconds,
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
                venue_dao=self.pipeline_repository,
                target_photos=settings.vibe_classifier_target_photos,
                escalation_threshold=settings.vibe_classifier_escalation_threshold,
                stage_b_photo_count=settings.vibe_classifier_stage_b_photos,
                enrichment_limit=_capped(settings.vibe_classifier_limit),
                early_stop_enabled=settings.vibe_classifier_early_stop_enabled,
                early_stop_min_photos=settings.vibe_classifier_early_stop_min_photos,
                early_stop_confidence=settings.vibe_classifier_early_stop_confidence,
                stage_a_model=settings.vibe_classifier_stage_a_model,
                stage_b_model=settings.vibe_classifier_stage_b_model,
                priority_venues=settings.dev_vibesense_pipeline_priority_venues,
            )
            logger.info("[Container] Vibe Classifier service initialized")
        else:
            logger.info(
                "[Container] Vibe Classifier disabled "
                "(missing OpenAI API key)"
            )

        # Initialize services. Serving reads the Redis-only DAO (serving_dao) so
        # public serving is independent of RDS at request time (an RDS outage
        # cannot break nearby serving) and unaffected by the pipeline RDS reads.
        self.venues_refresher_service = VenuesRefresherService(
            self.pipeline_repository,
            self.besttime_api,
            redis_client=redis_internal_client,
            fetch_venue_limit_override=settings.fetch_venue_limit_override,
            fetch_venue_total_limit=settings.fetch_venue_total_limit,
            dev_mode=settings.dev_mode,
            dev_lat=settings.dev_lat,
            dev_lng=settings.dev_lng,
            dev_radius=settings.dev_radius,
        )

        # Initialize handlers (serving reads the Redis-only DAO — see above).
        self.venue_handler = VenueHandler(self.serving_redis_dao)

        # Engagement (favorites/hot_likes) write-through API service, and the
        # projection service that rebuilds the Redis serving projection from RDS.
        self.engagement_service = EngagementService(
            redis_client=self.redis_client.client,
            rds_store=self.rds_store,
            pseudonymization_key=settings.engagement_pseudonymization_key,
        )
        self.redis_projection_service = RedisProjectionService(
            redis_only_dao=self.serving_redis_dao,
            rds_store=self.rds_store,
        )

        # Admin config: RDS system of record + synchronous Redis mirror (carve-out
        # like engagement, not the projector). Per-key validators dispatch before
        # any write; eligibility keeps EligibilityConfig validation.
        from app.services.admin_config_service import AdminConfigService
        from app.services.force_update import validate_force_update_config
        from app.services.venue_eligibility import EligibilityConfig
        from app.services.vibe_modes_config import validate_vibe_modes_config
        from app.models.venue_category import validate_category_map_config
        from app.models.post_category import validate_post_category_vocabulary_config
        from app.models.menu_lifecycle import validate_menu_expiry_days_config
        from app.services.event_venue_targeting import (
            validate_event_candidate_categories_config,
        )

        def _validate_eligibility_config(value):
            EligibilityConfig.from_dict(value, from_admin_override=True)  # raises on invalid
            return value  # persist raw body, byte-compatible with the reader

        self.admin_config_service = AdminConfigService(
            redis_client=self.redis_client.client,
            rds_store=self.rds_store,
            validators={
                "venue_eligibility": _validate_eligibility_config,
                "force_update": validate_force_update_config,
                "vibe_modes": validate_vibe_modes_config,
                "venue_category_map": validate_category_map_config,
                "event_candidate_categories": validate_event_candidate_categories_config,
                # plans/260811_post-items-and-categories.md §C: the post-
                # item category vocabulary — editable through the SAME
                # generic admin-config CRUD route every other key here uses,
                # no dedicated endpoint (see event_targeting_summary's own
                # comment on admin_trigger_router.py for the precedent).
                "post_category_vocabulary": validate_post_category_vocabulary_config,
                # plans/260811_menu-item-lifecycle.md §C: how long a dish
                # stays "current" after it was last seen — the SAME generic
                # admin-config CRUD route every key here uses, no dedicated
                # endpoint.
                "menu_expiry_days": validate_menu_expiry_days_config,
            },
        )

        # Event venue targeting (plans/260804_event-venue-targeting.md): free
        # category gate + bounded evidence gate. No optional API key required —
        # unlike venue_photo_archive_service, this makes zero external calls by
        # design, so it is always available. The flyer evidence source degrades
        # to a no-op when the media archive is not configured; the evidence
        # gate still runs on Instagram captions alone.
        from app.services.event_venue_targeting import (
            ArchivedFlyerEvidenceSource,
            EventVenueTargetingService,
            NullFlyerEvidenceSource,
        )
        from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS

        flyer_evidence_source = (
            ArchivedFlyerEvidenceSource(
                media_store=self.media_archive_store,
                archive_source=SOURCE_INSTAGRAM_POSTS,
            )
            if getattr(self, "media_archive_store", None) is not None
            else NullFlyerEvidenceSource()
        )
        self.event_venue_targeting_service = EventVenueTargetingService(
            venue_dao=self.pipeline_repository,
            redis_client=self.redis_client.client,
            flyer_evidence_source=flyer_evidence_source,
        )
        logger.info("[Container] Event venue targeting service initialized")

        # Instagram event extraction (plans/260804_instagram-event-extraction.md):
        # needs an OpenAI key (the vision call) AND the media archive (the
        # archived flyer images + manifests it reads). Optional and
        # dependency-aware like every other AI enrichment path — its absence
        # never breaks core venue serving.
        self.openai_event_extraction_client = None
        self.event_extraction_service = None
        if settings.openai_api_key and getattr(self, "media_archive_store", None) is not None:
            from app.api.openai_event_extraction_client import OpenAIEventExtractionClient
            from app.services.event_extraction_service import (
                EventExtractionService,
                EventPostSource,
            )

            self.openai_event_extraction_client = OpenAIEventExtractionClient(
                api_key=settings.openai_api_key,
                model=settings.event_extraction_model,
                max_completion_tokens=settings.event_extraction_max_tokens,
                # plans/260811_post-items-and-categories.md §C: steers the
                # model with the LIVE admin-configured category vocabulary
                # (app.models.post_category), same Redis mirror
                # AdminConfigService/event_venue_targeting_service already
                # read from.
                redis_client=self.redis_client.client,
            )
            self.event_extraction_service = EventExtractionService(
                venue_dao=self.pipeline_repository,
                post_source=EventPostSource(
                    media_store=self.media_archive_store,
                    archive_source=SOURCE_INSTAGRAM_POSTS,
                ),
                openai_client=self.openai_event_extraction_client,
                min_confidence=settings.event_extraction_min_confidence,
                flyer_confidence_floor=settings.photo_classification_confidence,
                max_events_per_post=settings.event_extraction_max_events_per_post,
                # plans/260811_post-items-and-categories.md §C: canonicalizes
                # each stored `category` against the SAME live vocabulary.
                redis_client=self.redis_client.client,
            )
            logger.info(
                f"[Container] Event extraction service initialized "
                f"(model={settings.event_extraction_model})"
            )
        else:
            logger.info(
                "[Container] Event extraction disabled (needs OpenAI key + media archive)"
            )

        # Instagram promoter events (plans/260804_instagram-promoter-events.md).
        # The registry is pure RDS CRUD and always available; discovery
        # additionally reads archived venue-post captions through the SAME
        # EventPostSource event extraction uses, reused unchanged, and
        # degrades to "nothing considered" (never an error) without the
        # media archive. The crawl needs the Apify Instagram client;
        # extraction and archiving inside it degrade gracefully (skipped,
        # not failed) when OpenAI or the media archive are not configured —
        # the same dependency-aware posture every other optional enrichment
        # path in this container takes.
        from app.services.promoter_registry_service import PromoterRegistryService

        promoter_post_source = None
        if getattr(self, "media_archive_store", None) is not None:
            from app.services.event_extraction_service import (
                EventPostSource as _PromoterDiscoveryPostSource,
            )

            promoter_post_source = _PromoterDiscoveryPostSource(
                media_store=self.media_archive_store,
                archive_source=SOURCE_INSTAGRAM_POSTS,
            )
        self.promoter_registry_service = PromoterRegistryService(
            venue_dao=self.pipeline_repository,
            post_source=promoter_post_source,
        )
        logger.info("[Container] Promoter registry service initialized")

        self.promoter_crawl_service = None
        if self.apify_instagram_client is not None:
            from app.services.promoter_crawl_service import (
                ApifyPromoterPostsClient,
                PromoterCrawlService,
            )
            from app.services.venue_photo_archive_service import HttpPhotoDownloader

            self.promoter_crawl_service = PromoterCrawlService(
                venue_dao=self.pipeline_repository,
                posts_client=ApifyPromoterPostsClient(self.apify_instagram_client),
                media_store=getattr(self, "media_archive_store", None),
                downloader=HttpPhotoDownloader(),
                openai_client=self.openai_event_extraction_client,
                archive_source=SOURCE_INSTAGRAM_POSTS,
                max_posts_per_account_default=settings.promoter_max_posts_per_account,
                confidence_floor=settings.promoter_link_confidence_floor,
                margin=settings.promoter_link_margin,
                min_confidence=settings.event_extraction_min_confidence,
                max_events_per_post=settings.event_extraction_max_events_per_post,
                # plans/260811_post-items-and-categories.md §C: canonicalizes
                # each stored `category` against the SAME live vocabulary.
                redis_client=self.redis_client.client,
            )
            logger.info("[Container] Promoter crawl service initialized")
        else:
            logger.info("[Container] Promoter crawl disabled (needs Apify API token)")

        # Scheduled Incremental Instagram Crawl
        # (plans/260809_scheduled-incremental-instagram-crawl.md). Reuses the
        # SAME Apify Instagram client every other Instagram path already
        # uses, plus event_extraction_service and promoter_crawl_service for
        # chaining (§H) — see instagram_crawl_service.py's own module
        # docstring for why it calls their low-level pieces rather than
        # `.run()` itself. Needs only the Apify client to exist at all;
        # archiving/extraction chaining degrade gracefully (skipped, not
        # failed) when their own dependencies are absent, the same
        # dependency-aware posture every other optional enrichment path in
        # this container takes.
        self.crawl_budget_dao = None
        self.instagram_crawl_service = None
        if self.apify_instagram_client is not None:
            from app.dao.crawl_budget_dao import CrawlBudgetDao
            from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
            from app.services.instagram_crawl_service import (
                CrawlServiceConfig,
                InstagramCrawlChainer,
                ScheduledInstagramCrawlService,
            )
            from app.services.venue_photo_archive_service import HttpPhotoDownloader

            self.crawl_budget_dao = CrawlBudgetDao(redis_internal_client)
            crawl_chainer = InstagramCrawlChainer(
                media_store=getattr(self, "media_archive_store", None),
                downloader=HttpPhotoDownloader(),
                event_extraction_service=self.event_extraction_service,
                promoter_crawl_service=self.promoter_crawl_service,
                # The SAME classifier VenuePhotoArchiveService uses — reused,
                # not duplicated, so an image-only flyer archived by the
                # scheduled crawl gets the SAME flyer detection a manual
                # archive run already gives it. None (not configured/no
                # OpenAI key) degrades to the pre-existing caption-only gate,
                # recorded as such via CRAWL_CHAIN_CLASSIFICATION_TOTAL.
                photo_classifier=self.photo_classification_service,
                archive_source=SOURCE_INSTAGRAM_POSTS,
            )
            self.instagram_crawl_service = ScheduledInstagramCrawlService(
                venue_dao=self.pipeline_repository,
                apify_client=self.apify_instagram_client,
                budget_dao=self.crawl_budget_dao,
                config=CrawlServiceConfig(
                    overlap_hours=settings.crawl_cursor_overlap_hours,
                    default_initial_lookback=settings.crawl_default_initial_lookback,
                    default_results_limit=settings.crawl_default_results_limit,
                    default_seed_results_limit=settings.crawl_default_seed_results_limit,
                    monthly_result_budget=settings.crawl_monthly_result_budget,
                    max_consecutive_failures=settings.crawl_max_consecutive_failures,
                    result_cost_usd=settings.apify_instagram_post_cost_usd,
                ),
                chainer=crawl_chainer,
            )
            logger.info("[Container] Scheduled Instagram crawl service initialized")
        else:
            logger.info(
                "[Container] Scheduled Instagram crawl disabled (needs Apify API token)"
            )

        # The serve handler resolves the live-busyness freshness window through the
        # admin-config mirror; wire it now that the service exists (venue_handler
        # was built above, before admin_config_service).
        self.venue_handler.admin_config_service = self.admin_config_service

        # Ex2: single-rule eligibility editing over admin.eligibility_rule rows;
        # rows are truth, the Redis mirror is reassembled from them on every write.
        from app.services.eligibility_rules import EligibilityRuleService

        self.eligibility_rule_service = EligibilityRuleService(
            self.rds_store, self.admin_config_service
        )
        # Let the periodic projector self-heal the eligibility serving mirror from
        # its rows each cycle (wired after both exist; the projector is built above).
        self.redis_projection_service.eligibility_rule_service = self.eligibility_rule_service

        # Closed-venue detection: reads stored review text and writes
        # admin.venue_closure_signal, which serving.eligible_venue excludes on.
        # Built after admin_config_service so phrase/window overrides are live;
        # the settings flag gates whether the scheduled job runs at all.
        from app.services.closure_detection_service import ClosureDetectionService

        self.closure_detection_service = ClosureDetectionService(
            rds_store=self.rds_store,
            admin_config_service=self.admin_config_service,
            enabled=lambda: settings.closure_detection_enabled,
        )

        # Monthly budget DAO + service (used by add-by-address + discovery).
        self.venue_budget_dao = VenueBudgetDao(redis_internal_client)
        self.venue_budget_service = VenueBudgetService(
            redis_client=redis_internal_client,
            budget_dao=self.venue_budget_dao,
        )

        # Add-by-address handler. The optional Google client lets a manual add with
        # a place_id re-source its price tier (enum + range) via the shared helper.
        self.add_venue_handler = AddVenueHandler(
            venue_dao=self.pipeline_repository,
            besttime_api=self.besttime_api,
            budget_service=self.venue_budget_service,
            redis_client=redis_internal_client,
            google_places_client=self.google_places_api,
            # Inline Google enrichment at add time (shares the handler's DAO).
            google_places_enrichment_service=self.google_places_enrichment_service,
            # System of record for the geo-link undo path (created_at recency
            # guard + soft-delete; the projector then drops it from serving).
            rds_store=self.rds_store,
            # Kill switch for the Google-only catalog path (default off, read
            # fresh on every request; see plans/260804_add-venue-google-only.md).
            admin_config_service=self.admin_config_service,
            # Inline Instagram discovery at add time (default on via
            # settings.add_venue_instagram_enabled). None when the cascade
            # itself is not configured (settings.instagram_cascade_enabled off
            # or no Apify token) — the handler degrades every add's
            # instagram.status to "skipped" in that case.
            # See plans/260811_add-venue-instagram-discovery.md.
            instagram_cascade_service=self.instagram_cascade_service,
        )

        # Server-side batch venue-add: runs a curated list through the same
        # add_venue_handler in one pollable background job.
        self.batch_add_service = BatchAddService(
            handler=self.add_venue_handler,
            redis_client=redis_internal_client,
            google_client=self.google_places_api,
            budget_service=self.venue_budget_service,
        )

        # Expose the budget service to the refresher so discovery can
        # observe the monthly cap and reserve.
        self.venues_refresher_service.set_budget_service(self.venue_budget_service)

        logger.info("[Container] Container initialized successfully")

    def _init_photo_classifier(self, settings: Settings) -> None:
        """Build the per-photo classifier, or leave the archive unclassified.

        Deliberately silent-but-logged when there is no key: the archive job is
        the expensive half of this pipeline and must keep running without the
        cheap half. A photo already paid for is never lost to a missing
        classifier.
        """
        if not settings.photo_classification_enabled:
            logger.info("[Container] Photo classification disabled by config")
            return
        if not settings.openai_api_key:
            logger.warning(
                "[Container] Photo classification enabled but no OpenAI key; "
                "archived photos keep the category their source gave them"
            )
            return
        self.openai_photo_classifier_client = OpenAIPhotoClassifierClient(
            api_key=settings.openai_api_key,
            model=settings.photo_classification_model,
        )
        self.photo_classification_service = PhotoClassificationService(
            client=self.openai_photo_classifier_client,
            model=settings.photo_classification_model,
            confidence_threshold=settings.photo_classification_confidence,
            attribute_confidence_threshold=settings.photo_attribute_confidence,
            batch_size=settings.photo_classification_batch_size,
            cost_per_photo_usd=settings.photo_classification_cost_per_photo_usd,
            cost_per_1k_input_usd=settings.photo_classification_cost_per_1k_input_usd,
            cost_per_1k_output_usd=settings.photo_classification_cost_per_1k_output_usd,
        )
        logger.info(
            f"[Container] Photo classification initialized "
            f"(model={settings.photo_classification_model}, "
            f"attributes={settings.photo_attributes_enabled})"
        )

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

        if self.openai_photo_classifier_client:
            try:
                await self.openai_photo_classifier_client.close()
                logger.info("[Container] OpenAI Photo Classifier client closed")
            except Exception as e:
                logger.error(
                    f"[Container] Error closing OpenAI Photo Classifier client: {e}"
                )
