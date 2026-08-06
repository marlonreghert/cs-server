"""Configuration management using Pydantic BaseSettings with JSON file support."""
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def flatten_json_config(config: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested JSON config into flat key-value pairs.

    Supports nested structures like:
    {
        "redis": {"redis_host": "localhost", "redis_port": 6379},
        "server": {"server_port": 8080}
    }

    Becomes:
    {"redis_host": "localhost", "redis_port": 6379, "server_port": 8080}

    Keys starting with "_" (like "_comment") are skipped.
    """
    result = {}

    for key, value in config.items():
        # Skip comment keys
        if key.startswith("_"):
            continue

        if isinstance(value, dict):
            # Recursively flatten nested dicts
            nested = flatten_json_config(value)
            result.update(nested)
        else:
            result[key] = value

    return result


def load_json_config(config_file: Optional[str] = None) -> dict[str, Any]:
    """Load configuration from a JSON file.

    Supports both flat and nested JSON structures. Nested structures are
    automatically flattened. Keys starting with "_" are treated as comments
    and ignored.

    Args:
        config_file: Path to JSON config file. If None, checks CONFIG_FILE env var.

    Returns:
        Dictionary of configuration values (flattened), or empty dict if no file found.
    """
    file_path = config_file or os.getenv("CONFIG_FILE")

    if not file_path:
        return {}

    path = Path(file_path)
    if not path.exists():
        logger.warning(f"Config file not found: {file_path}")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
            logger.info(f"Loaded configuration from: {file_path}")
            # Flatten nested structure
            return flatten_json_config(config)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file {file_path}: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error reading config file {file_path}: {e}")
        return {}


class Settings(BaseSettings):
    """Application configuration with JSON file and environment variable support.

    Configuration priority (highest to lowest):
    1. Environment variables
    2. JSON config file (specified via CONFIG_FILE env var)
    3. Default values
    """

    # Redis Configuration
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    # RDS (Postgres) system-of-record connection. See
    # plans/rds_system_of_record_01_06_26.md.
    rds_host: str = ""
    rds_port: int = 5432
    rds_db: str = "vibesense"
    rds_user: str = ""
    rds_password: str = ""
    rds_sslmode: str = "require"
    # Secret key used to HMAC-pseudonymize end-user ids before they are written
    # to RDS (favorites/hot_likes). Never store raw user ids in RDS.
    engagement_pseudonymization_key: str = ""

    # Redis projection decoupling (plans/redis_projection_decoupling_01_06_26.md).
    # With RDS enabled, a scheduled off-loop projector is the sole Redis writer for
    # pipeline data: it re-asserts the Redis serving projection from RDS (removing
    # venues deprecated in RDS and counting the photo cache TTL down). Pipelines
    # write RDS-only and read their inputs/gating from RDS. This is the interval
    # the projector runs on (a cadence knob).
    redis_projection_minutes: int = 2

    # Closed-venue detection: derives a closure signal from stored review text
    # and drops flagged venues from the serving view. OFF by default — a false
    # positive removes a live venue from serving, so enabling it is deliberate
    # and should follow a dry run over production data. Reviews change slowly,
    # so the cadence is hours, not minutes.
    closure_detection_enabled: bool = False
    closure_detection_hours: int = 12

    # Kill switch for cataloging a venue from Google metadata alone when
    # BestTime's geo fallback finds no name match (the terminal-502 case).
    # Read through AdminConfigService at request time (reversible in seconds,
    # no redeploy); this Settings attribute is only the fallback for when the
    # admin read fails or the key is absent — the admin value always wins when
    # present. OFF by default: creation is automatic on every qualifying
    # rejection, so a false-off-to-on flip catalogs indiscriminately.
    # See plans/260804_add-venue-google-only.md.
    add_venue_google_only_enabled: bool = False

    @property
    def rds_sqlalchemy_url(self) -> str:
        """SQLAlchemy URL for the RDS Postgres connection (psycopg v3 driver)."""
        return (
            f"postgresql+psycopg://{self.rds_user}:{self.rds_password}"
            f"@{self.rds_host}:{self.rds_port}/{self.rds_db}"
            f"?sslmode={self.rds_sslmode}"
        )

    # Venues Refresher Configuration
    # 43200 minutes = 30 days
    venues_catalog_refresh_minutes: int = 43200
    venues_live_refresh_minutes: int = 5
    weekly_forecast_cron: str = "0 0 * * 0"  # Sundays at 00:00

    # Serve-time live-busyness freshness gate. The stale window is DERIVED from
    # the live refresh cadence so the two never desync: a cached live value is
    # "stale" once older than live_freshness_refresh_factor × the effective
    # refresh interval (admin_config:live_refresh_minutes, else
    # venues_live_refresh_minutes above), floored at live_freshness_min_minutes to
    # absorb BestTime/clock skew at short intervals. A slower refresh therefore
    # auto-widens the window (venues get re-touched well inside it); suppressed
    # venues fall back to the forecast estimate in vibes_bot.
    live_freshness_refresh_factor: float = 2.0
    live_freshness_min_minutes: int = 5

    # Serve-time attachment of the previous business day's weekly forecast
    # (plans/260710_prev-day-weekly-forecast.md). Under the BestTime day_raw
    # convention, day index 0 is 6 AM of that calendar day, so a moment between
    # 00:00-05:59 lives in the PREVIOUS day's array. When enabled, /v1/venues/
    # nearby additionally attaches `weekly_forecast_prev` (besttime_day_int - 1
    # mod 7) so readers can select by the 6 AM anchor. Additive and ignored by
    # readers that don't know about it; the flag exists purely as an instant
    # rollback lever, default on.
    weekly_forecast_prev_day_enabled: bool = True

    # Venue discovery (catalog refresh + venue-filter). Disabled by default so
    # discovery does not spend BestTime's scarce monthly unique-venue cap; the
    # bounded live/weekly refresh and the manual add-venue flow are the only
    # intended consumers. When False, Job 1 is not scheduled and the manual
    # `venue_catalog` trigger is rejected.
    discovery_enabled: bool = False

    # BestTime API Configuration
    besttime_private_key: str = "pri_aff50a71a038456db88864b16d9d6800"
    besttime_public_key: str = "pub_4f4f184e1a5f4f50a48e945fde7ab2ea"
    besttime_endpoint_base_v1: str = "https://besttime.app/api/v1"
    besttime_search_polling_wait_seconds: int = 15
    # Dedicated timeout (seconds) for the slow, synchronous POST /forecasts
    # "create venue" call (add_venue_to_account). BestTime builds a fresh
    # forecast on this request, so it is far slower than the live/read calls;
    # keep it well above the tight client-wide default so a manual add survives
    # slow-but-healthy BestTime latency instead of raising ReadTimeout (prod
    # incidents 2026-07-01/02 saw healthy creates outlive 30s).
    besttime_add_venue_timeout_seconds: float = 60.0
    # BestTime's documented Venue Search rate limits (30 requests/minute,
    # 300 requests/hour). The client paces the search family — POST /forecasts
    # create, /venues/filter, /venues/search, /venues/progress — inside these
    # windows and fails fast (BestTimeRateLimitedError) when a call would need
    # to wait longer than the max-wait budget. <=0 disables a window. The
    # budget covers a full minute window (60s + slack) so per-minute pacing
    # always waits, while an exhausted hour window (waits up to 3600s) fails
    # fast instead of hanging the caller.
    besttime_search_rate_per_minute: int = 30
    besttime_search_rate_per_hour: int = 300
    besttime_rate_max_wait_seconds: float = 75.0

    # Google Places API Configuration
    # Enrichment includes: vibe attributes, business status checks, permanently closed detection
    google_places_api_key: str = ""
    google_places_enrichment_enabled: bool = False  # Disabled by default
    google_places_enrichment_cron: str = "0 3 * * *"  # Daily at 3 AM
    google_places_enrichment_on_startup: bool = False  # If True, run enrichment on startup

    # Price-range -> tier bucketing thresholds, per ISO currency. This is the
    # PRIMARY price signal: the objective Google `priceRange` is bucketed whenever
    # it is present (the coarse `priceLevel` enum is only the range-less fallback).
    # Each value is three ascending cut points [c1, c2, c3] applied to the range
    # midpoint ((min+max)/2, or `min`/startPrice when the upper bound is unbounded):
    #   midpoint < c1 -> 1 | < c2 -> 2 | < c3 -> 3 | >= c3 -> 4
    # A currency with no configured table yields no tier (the derivation falls
    # through to the enum, then BestTime, then NULL). BRL cuts anchored to the
    # observed Recife range-midpoint distribution (deciles ~R$30-110), tuned so
    # venues spread across tiers instead of piling at $$ (the prior [40,80,160]
    # was calibrated too high for this market).
    price_range_tier_thresholds: dict[str, list[float]] = {"BRL": [40.0, 70.0, 110.0]}

    # Permanently closed venue handling (uses Google Places API businessStatus)
    # When enabled, venues marked as CLOSED_PERMANENTLY by Google are soft-deprecated
    # and retained in Redis for troubleshooting.
    # This runs during the google_places_enrichment refresh
    remove_permanently_closed_venues: bool = True  # Enabled by default when enrichment runs

    # Temporarily closed venue handling (uses Google Places API businessStatus)
    # Accepted for backward-compatible config only. CLOSED_TEMPORARILY venues
    # remain active so live busyness can continue to refresh.
    remove_temporarily_closed_venues: bool = True  # Enabled by default when enrichment runs

    # Business-status recheck for ALREADY-enriched venues: a cheap, status-only
    # Google Details call (fields mask "businessStatus" only — no vibe/opening
    # hours/reviews refetch) so a venue that has gone CLOSED_PERMANENTLY /
    # CLOSED_TEMPORARILY since its first enrichment is detected (and, when
    # remove_permanently_closed_venues is set, deprecated + dropped from live
    # refresh selection) without waiting for an explicit force_refresh sweep.
    # LOCKED DEFAULT: False. Flipping this on spends one Details call per
    # already-enriched venue on the FIRST recheck-enabled nightly run (and every
    # run thereafter) — a deliberate, human-approved decision, never a silent
    # side effect of deploying this code. business_status_recheck_limit bounds
    # how many venues one run rechecks (0 = no bound, i.e. the full catalog).
    business_status_recheck_enabled: bool = False
    business_status_recheck_limit: int = 0

    # Photo enrichment configuration (uses Google Places API)
    photo_enrichment_enabled: bool = False  # Disabled by default, set PHOTO_ENRICHMENT_ENABLED=true to enable
    photo_enrichment_on_startup: bool = False  # If True, fetch photos on startup
    photo_enrichment_limit: int = 20  # Max venues to enrich with photos per refresh cycle (to control API costs)
    photos_per_venue: int = 5  # Number of photos to fetch per venue
    # TTL for `venue_photos_v1:*` Redis keys. Google rotates photo `name` tokens
    # periodically; once rotated, the cached /media URL returns 400
    # INVALID_ARGUMENT and the mobile app shows nothing. Eviction at this TTL
    # forces the daily enrichment cron to repopulate the venue with fresh
    # tokens. Can be overridden live via the vibesadmin
    # `admin_config:venue_photos_cache_ttl_days` key.
    photo_cache_ttl_days: int = 5
    # TTL (hours) for the ON-DEMAND `venue_photos_fresh_v1:*` Redis cache, which
    # holds FRESH, KEYLESS googleusercontent.com URLs resolved per-venue on
    # demand (POST /internal/venues/{id}/photos/resolve). A day is acceptable
    # because vibes_bot's dead-URL retry forces a re-resolve (max_photos +
    # force=true) and overwrites this key on first sighting of a rotated/dead
    # URL, instead of waiting out the TTL — so staleness is bounded by the
    # repair path, not by this number alone. Admin-tunable live via the
    # vibesadmin `admin_config:photo_fresh_cache_ttl_hours` key.
    # ROLLOUT: this code default ships ahead of the retry path. Production must
    # stay at 6h via the admin_config override until vibes_bot's retry is
    # deployed — see plans/260729_photo-resolve-cost-controls.md Rollout Note.
    photo_fresh_cache_ttl_hours: int = 24

    # Instagram Discovery (Apify) Configuration
    apify_api_token: str = ""
    instagram_enrichment_enabled: bool = False
    instagram_enrichment_cron: str = "0 4 * * 1"  # Weekly: Monday at 4 AM
    instagram_enrichment_on_startup: bool = False
    instagram_min_confidence: float = 0.50
    instagram_auto_accept_threshold: float = 0.75
    # The venue-website tier fetches arbitrary third-party pages during a
    # full-catalogue run; both bounds exist so one hostile site cannot stall it.
    instagram_website_timeout_seconds: float = 10.0
    instagram_website_max_bytes: int = 1_500_000
    instagram_search_candidates: int = 3
    instagram_enrichment_limit: int = 0  # Max venues per run (0 = unlimited)
    instagram_cache_ttl_days: int = 30

    # Instagram handle cascade. Sources run cheapest-first; only the Apify
    # search costs money, so tier_apify_search_enabled=false gives a zero-cost
    # pass over the whole catalog.
    instagram_cascade_enabled: bool = True
    instagram_profile_probe_enabled: bool = True
    instagram_profile_probe_timeout_seconds: float = 10.0
    # A crawler UA is what makes Instagram serve og: tags at all; a browser UA
    # gets a JS shell with no profile data.
    instagram_probe_user_agent: str = (
        "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
    )
    # The LLM adjudicator is opt-in: it costs money and only helps in the
    # ambiguous band.
    # The judge settles candidates the cheap signals cannot. Opt-in: it is the
    # only part of the cascade that costs money per venue beyond the paid search.
    instagram_judge_enabled: bool = False
    # Worth a fraction of a cent to settle. Deliberately BELOW instagram_min_confidence:
    # a paid-search candidate tops out at 0.60 while the probe is blocked, and the
    # paid tier is the only one reaching venues with no website at all.
    instagram_judge_floor: float = 0.30
    # Google-search tier: the only source that reaches a venue with no web
    # presence. Paid per venue, so opt-in, and it can never accept a handle on
    # its own — the judge must confirm it (see PROVENANCE_WEIGHT).
    instagram_google_search_enabled: bool = False
    instagram_google_search_actor: str = "apify~google-search-scraper"
    instagram_google_search_results: int = 10
    instagram_google_search_country: str = "br"
    instagram_judge_model: str = "gpt-5.6-luna"
    instagram_judge_max_venue_photos: int = 3

    # Pipeline run registry: how many recent runs per pipeline stay selectable
    # in Grafana. This is the cardinality ceiling — series = pipelines x size.
    pipeline_run_registry_enabled: bool = True
    pipeline_run_registry_size: int = 10
    instagram_not_found_cache_ttl_days: int = 7

    # Instagram Posts Scraping (feeds post captions into vibe classifier)
    ig_posts_enrichment_enabled: bool = False
    ig_posts_enrichment_on_startup: bool = False
    ig_posts_enrichment_cron: str = "0 4 * * 3"  # Weekly: Wednesday at 4 AM
    ig_posts_enrichment_limit: int = 20
    ig_posts_per_venue: int = 10
    ig_posts_cache_ttl_days: int = 30

    # Menu Enrichment (Apify menu photo scraping + S3 storage)
    menu_enrichment_enabled: bool = False
    menu_enrichment_on_startup: bool = False
    menu_enrichment_cron: str = "0 5 1 * *"  # Monthly: 1st at 5 AM
    menu_enrichment_limit: int = 10           # Max venues per run
    menu_photos_per_venue: int = 20
    menu_photo_categories: list[str] = [
        "menu", "cardapio", "preco", "valor",
        "drink", "drinq", "bebid", "bebe",
        "comid", "comes", "prato",
        "entrada", "aperitiv", "petisco",
        "porcao", "combo",
    ]

    # SerpApi (deprecated — no longer used, kept for backwards compat)
    serpapi_api_key: str = ""

    # Apify fallback for menu photos (deprecated — replaced by menu_gmaps_fallback_enabled)
    menu_apify_fallback_enabled: bool = False

    # Google Maps menu photo fallback (compass/google-maps-extractor via Apify)
    menu_gmaps_fallback_enabled: bool = False

    # GPT-4o-mini photo pre-filter
    menu_photo_filter_enabled: bool = True
    menu_photo_filter_confidence: float = 0.6

    # S3 (for menu photo storage)
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # S3 data lake (raw BestTime responses). Separate bucket from menu photos:
    # different lifecycle, different access pattern, put-only IAM policy.
    # Disabled by default — nothing is archived until this is turned on.
    # Credentials are deliberately absent: the writer resolves the DEFAULT
    # credential chain (EC2 instance role in prod, SSO profile locally). The
    # optional key pair below is a local-dev escape hatch only.
    datalake_enabled: bool = False
    datalake_bucket: str = ""
    datalake_region: str = "us-east-1"
    datalake_access_key_id: str = ""
    datalake_secret_access_key: str = ""
    # Bounded queue: when the flusher falls behind, records are DROPPED (counted
    # in datalake_records_dropped_total) rather than slowing ingestion down.
    datalake_queue_maxsize: int = 10000
    # Flush thresholds: whichever hits first. 256KB keeps objects above S3's
    # 128KB minimum billable size for cold tiers; 15 minutes bounds how much
    # buffered data a crash can lose.
    datalake_flush_max_bytes: int = 262144
    datalake_flush_max_seconds: int = 900
    datalake_shutdown_flush_seconds: int = 10

    # Venue media archive (Google photos -> S3 media/ prefix). Off by default.
    # Bucket falls back to datalake_bucket so the media archive lives beside the
    # raw lake; credentials come from the same instance role (no keys here).
    media_archive_enabled: bool = False
    media_archive_bucket: str = ""
    # Google returns at most ~10 photo references per place, so 10 is "all".
    media_archive_max_photos_per_venue: int = 10
    media_archive_photo_timeout_seconds: float = 15.0
    media_archive_max_photo_bytes: int = 10485760  # 10 MiB
    # Per-run defaults when the operator does not say otherwise. Deliberately
    # SMALL: Google bills per photo request, so the default click must be cheap
    # (50 x 5 = 250 requests, inside the free tier) and scaling up must be a
    # conscious edit. See the $10/month spend gate in the hero-photo plan.
    media_archive_default_max_venues: int = 50
    media_archive_default_max_photos: int = 5
    # Outbound pacing for Google photo requests, and how many venues may be in
    # flight at once. Keeps a large run from bursting into a 429 wall.
    media_archive_rate_per_second: float = 5.0
    media_archive_concurrency: int = 4
    media_archive_max_retries: int = 3
    # Unit price used ONLY for the pre-run estimate. Unverified against Google's
    # current rate card (see Q1b in plans/260726_venue-list-hero-photo.md), which
    # is why it is a setting and why the estimate carries a caveat.
    google_photo_cost_per_1k_usd: float = 7.0
    # Apify pay-per-event rates for the compass google-maps-extractor, used ONLY
    # by the pre-run estimate. Published rates as of 2026-07-27; the per-image
    # charge is NOT published, so an Apify estimate is a floor.
    apify_place_scraped_cost_usd: float = 0.004
    apify_place_details_cost_usd: float = 0.002
    # How much longer to keep polling an Apify actor run that is STILL ALIVE when
    # the 300s base poll budget runs out. Continuing costs nothing extra — the
    # scrape is already billed — whereas abandoning it loses the venue and
    # re-running it bills a second time.
    #
    # Ships at 0 (disabled) on purpose. Whether stalled runs need more time or
    # less concurrency depends on whether they sit in READY or RUNNING, which
    # apify_poll_timeouts_total now reports; size this from that data rather than
    # guessing. Suggested first value once measured: 300.
    apify_poll_continuation_seconds: float = 0.0
    # SearchApi.io — the only source that can name a photo's Google tab. Billed
    # per search, one per category per venue. Developer plan rate.
    searchapi_api_key: str = ""
    searchapi_cost_per_1k_usd: float = 4.0
    # Apify's instagram-scraper bills per RESULT ITEM (post), unlike the Maps
    # extractor which bills per place regardless of photo count. Unverified
    # against Apify's current rate card — no APIFY_API_TOKEN was available to
    # confirm it against a live run — so, like the Google unit price, this is a
    # setting and the estimate that uses it carries a caveat.
    apify_instagram_post_cost_usd: float = 0.003
    # Default for the instagram_posts archive source's `posts_per_venue` config
    # field, used both as the admin-panel default and the runtime fallback when
    # a saved config omits it.
    instagram_archive_posts_per_venue: int = 10

    # Per-photo classification (our own taxonomy + per-category attributes).
    # Runs between fetch and store, and ONLY for sources that do not return
    # Google's real tab — SearchApi's category is a fact and must not be
    # overwritten by a guess. Needs `openai_api_key`; without it the archive
    # runs exactly as before, because classification is an enhancement and
    # never a dependency.
    photo_classification_enabled: bool = True
    photo_classification_model: str = "gpt-5.6-luna"
    # Below this, a photo is filed as `other` rather than guessed. Matches the
    # menu filter's threshold: a wrong label is worse than an honest unknown,
    # because everything downstream will trust it.
    photo_classification_confidence: float = 0.6
    # The bar for ONE attribute, and higher than the category's on purpose: a
    # wrong category misfiles a photo, a wrong attribute is read as a fact about
    # the venue. Confidence is reported per attribute, so a model that is sure
    # about the room and unsure about the screens keeps the first and loses only
    # the second — anything under this becomes `not_classified`.
    photo_attribute_confidence: float = 0.8
    photo_classification_batch_size: int = 10
    # The attributes half of the single classification call. Switchable on its
    # own so a run can categorize cheaply without paying for the attribute JSON,
    # which is most of the output cost.
    photo_attributes_enabled: bool = True
    # Unit prices used ONLY to report what a run cost, never to change what it
    # does. The token rates are the real ones — the API reports exactly what it
    # consumed — and gpt-4o-mini's list rates are UNVERIFIED against OpenAI's
    # current card, which is why they are settings. The per-photo figure is now
    # only a fallback, for a client that cannot report token usage at all.
    photo_classification_cost_per_photo_usd: float = 0.00006
    photo_classification_cost_per_1k_input_usd: float = 0.0002
    photo_classification_cost_per_1k_output_usd: float = 0.00125

    # Menu Data Extraction (OpenAI GPT-4o-mini)
    openai_api_key: str = ""
    menu_extraction_enabled: bool = False
    menu_extraction_on_startup: bool = False
    menu_extraction_cron: str = "0 6 1 * *"  # Monthly: 1st at 6 AM
    menu_extraction_model: str = "gpt-5.6-luna"
    # Where extraction gets its photos. `archive` reads the newest run of the
    # retrieval pipeline — real Google "Menu" tab shots, already paid for —
    # instead of a second private copy. `redis` restores the original path
    # without a deploy.
    menu_extraction_photo_source: str = "archive"
    menu_extraction_archive_source: str = "searchapi_gmaps_photos"
    menu_extraction_archive_category: str = "menu"
    # Short: the url is handed to OpenAI, not kept.
    menu_photo_presign_seconds: int = 900

    # Vibe Classifier (OpenAI Vision - 2-stage hybrid)
    vibe_classifier_enabled: bool = False
    vibe_classifier_on_startup: bool = False
    vibe_classifier_cron: str = "0 7 1 * *"        # Monthly: 1st at 7 AM
    vibe_classifier_limit: int = 20                 # Max venues per run (0 = unlimited)
    vibe_classifier_target_photos: int = 10         # Photos to send to Stage A
    vibe_classifier_escalation_threshold: float = 0.80  # Below this -> Stage B
    vibe_classifier_stage_b_photos: int = 5         # Photos for Stage B (highest relevance)
    vibe_classifier_stage_a_model: str = "gpt-5.6-luna"
    vibe_classifier_stage_b_model: str = "gpt-5.6-luna"
    vibe_classifier_early_stop_enabled: bool = True
    vibe_classifier_early_stop_min_photos: int = 6

    # Instagram Event Extraction (plans/260804_instagram-event-extraction.md).
    # One OpenAI vision call per qualifying post (never a batch — see
    # app/services/event_extraction_service.py for why). gpt-5.6 is a
    # reasoning model whose reasoning tokens count against
    # max_completion_tokens, so the default carries real headroom above a
    # typical lineup+description payload.
    event_extraction_model: str = "gpt-5.6-luna"
    # Below this, an otherwise-successful extraction is queued for review
    # rather than trusted — the operator outranks an unsure model.
    event_extraction_min_confidence: float = 0.5
    event_extraction_max_tokens: int = 4096
    # Multi-Event Posts (plans/260806_multi-event-posts.md). A sanity bound on
    # how many events ONE post can yield — also the input the output-token
    # budget scales from (app.api.openai_event_extraction_client.
    # compute_multi_event_max_completion_tokens), since the real count is
    # unknown before the call completes. 20 comfortably covers the largest
    # observed roundup (17 images) with headroom.
    event_extraction_max_events_per_post: int = 20
    vibe_classifier_early_stop_confidence: float = 0.92

    # Instagram Promoter Events (plans/260804_instagram-promoter-events.md).
    # Two independent gates decide an auto-link (app/services/
    # event_venue_resolution.py): an absolute floor, AND a margin over the
    # runner-up. The margin is the one that matters — a top score of 0.91
    # against a runner-up of 0.89 is a coin toss wearing a high score.
    # Calibrated by the first hand-audited crawl (see the plan's Open
    # Questions), not asserted correct here.
    promoter_link_confidence_floor: float = 0.55
    promoter_link_margin: float = 0.08
    # A promoter posts far more than a venue does, so this bound is NOT
    # optional and defaults small — unlike event extraction's per-venue cap.
    promoter_max_posts_per_account: int = 15
    # How many distinct already-extracted event posts must mention a handle
    # before discovery proposes it as a candidate account.
    promoter_mention_threshold: int = 3

    # Dev Mode - overrides default locations for venue discovery
    dev_mode: bool = False
    dev_lat: float = -8.07834       # Default: Recife ZS/ZN
    dev_lng: float = -34.90938
    dev_radius: int = 6000          # Meters
    dev_vibesense_pipeline_priority_venues: list[str] = []  # Venue names to classify first

    # Server Configuration
    server_port: int = 8080
    log_level: str = "INFO"

    # Startup Configuration
    # If False, skip initial venue refresh on startup (only schedule jobs)
    refresh_on_startup: bool = True
    # If set (> 0), overrides the limit for each location when fetching venues from BestTime API
    fetch_venue_limit_override: int = 0
    # Global cap on total venues fetched from BestTime API across all locations (-1 = disabled, 0 = fetch none)
    fetch_venue_total_limit: int = -1
    # Global cap on how many venues get processed by enrichment services (photo, instagram, menu, vibe classifier)
    # -1 = disabled (use each service's own limit), 0 = process none
    process_venue_total_limit: int = -1

    # Project Paths
    project_root: str = ""
    resources_path_prefix: str = "resources"

    # Resource Files
    search_venue_response_resource: str = "search_venues_response.json"
    venue_static_resource: str = "venue_static.json"
    search_progress_response_resource: str = "search_progress_response.json"
    live_forecast_response_resource: str = "live_forecast_response.json"
    venue_filter_response_resource: str = "venue_filter_response.json"
    venues_ids_resource: str = "static_venues_ids.json"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    def __init__(self, **kwargs):
        """Initialize settings from JSON file and environment variables.

        Priority: env vars > JSON config > defaults
        """
        # Load JSON config first (if CONFIG_FILE is set)
        json_config = load_json_config()

        # Remove JSON config keys that have env var overrides set,
        # so pydantic-settings can pick up the env var value instead.
        # (pydantic-settings treats kwargs as highest priority, above env vars)
        for key in list(json_config.keys()):
            if os.getenv(key.upper()) is not None:
                logger.info(f"Env var {key.upper()} overrides JSON config for '{key}'")
                del json_config[key]

        # Merge: kwargs override JSON config
        merged_kwargs = {**json_config, **kwargs}

        super().__init__(**merged_kwargs)

        if not self.project_root:
            # Use PROJECT_ROOT env var or current working directory
            self.project_root = os.getenv("PROJECT_ROOT", os.getcwd())

    @property
    def base_dir(self) -> Path:
        """Get the project root directory as a Path object."""
        return Path(self.project_root)

    def get_resource_path(self, resource_file: str) -> Path:
        """Get the full path to a resource file."""
        return self.base_dir / self.resources_path_prefix / resource_file

    @property
    def redis_address(self) -> str:
        """Get Redis connection address in host:port format."""
        return f"{self.redis_host}:{self.redis_port}"


# Global settings instance
settings = Settings()
