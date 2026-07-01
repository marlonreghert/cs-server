"""Prometheus metrics definitions for cs-server.

Exposes metrics for:
1. HTTP API metrics (requests, latency, errors)
2. BestTime API client metrics (calls, latency, errors)
3. Background job metrics (runs, duration, errors)
4. Data quality metrics (venues with various attributes)
"""
from prometheus_client import Counter, Histogram, Gauge, Info

# =============================================================================
# HTTP API METRICS
# =============================================================================

# Request counter with method, endpoint, and status labels
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["method", "endpoint", "status_code"],
)

# Request latency histogram
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Active requests gauge
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being processed",
    ["method", "endpoint"],
)

# Request size histogram
HTTP_REQUEST_SIZE_BYTES = Histogram(
    "http_request_size_bytes",
    "HTTP request body size in bytes",
    ["method", "endpoint"],
    buckets=(100, 500, 1000, 5000, 10000, 50000, 100000),
)

# Response size histogram
HTTP_RESPONSE_SIZE_BYTES = Histogram(
    "http_response_size_bytes",
    "HTTP response body size in bytes",
    ["method", "endpoint"],
    buckets=(100, 500, 1000, 5000, 10000, 50000, 100000, 500000),
)

# =============================================================================
# BESTTIME API CLIENT METRICS
# =============================================================================

# API call counter
BESTTIME_API_CALLS_TOTAL = Counter(
    "besttime_api_calls_total",
    "Total number of BestTime API calls",
    ["endpoint", "status"],  # status: success, error
)

# API call latency
BESTTIME_API_CALL_DURATION_SECONDS = Histogram(
    "besttime_api_call_duration_seconds",
    "BestTime API call latency in seconds",
    ["endpoint"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# API error counter by error type
BESTTIME_API_ERRORS_TOTAL = Counter(
    "besttime_api_errors_total",
    "Total number of BestTime API errors",
    ["endpoint", "error_type"],  # error_type: http_error, timeout,
                                 # connection_error, invalid_json,
                                 # invalid_response_schema
)

# Analysis day entries dropped while parsing a POST /forecasts (create venue)
# response. Analysis is best-effort on creates: a malformed day never fails
# the envelope, but each drop is counted here (and WARNING-logged).
BESTTIME_ADD_VENUE_ANALYSIS_DROPPED_TOTAL = Counter(
    "besttime_add_venue_analysis_days_dropped_total",
    "Analysis day entries dropped while parsing BestTime POST /forecasts responses",
)

# =============================================================================
# GOOGLE PLACES API CLIENT METRICS
# =============================================================================

# API call counter
GOOGLE_PLACES_API_CALLS_TOTAL = Counter(
    "google_places_api_calls_total",
    "Total number of Google Places API calls",
    ["endpoint", "status"],  # status: success, error
)

# API call latency
GOOGLE_PLACES_API_CALL_DURATION_SECONDS = Histogram(
    "google_places_api_call_duration_seconds",
    "Google Places API call latency in seconds",
    ["endpoint"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# API error counter by error type
GOOGLE_PLACES_API_ERRORS_TOTAL = Counter(
    "google_places_api_errors_total",
    "Total number of Google Places API errors",
    ["endpoint", "error_type"],  # error_type: http_error, timeout, connection_error
)

# Vibe attributes fetch results
VIBE_ATTRIBUTES_FETCH_RESULTS = Counter(
    "vibe_attributes_fetch_results_total",
    "Results of vibe attributes fetch operations",
    ["result"],  # result: cached, skipped_no_place_id, error
)

# Venues with vibe attributes
VENUES_WITH_VIBE_ATTRIBUTES = Gauge(
    "venues_with_vibe_attributes",
    "Number of venues with cached vibe attributes",
)

# Eligibility Redis mirror rehydration (rebuild admin_config:venue_eligibility
# from the admin.eligibility_rule rows on startup + the periodic projector cycle)
ELIGIBILITY_MIRROR_REHYDRATION_TOTAL = Counter(
    "eligibility_mirror_rehydration_total",
    "Eligibility Redis mirror rehydrations from admin.eligibility_rule rows",
    ["result"],  # success | failure
)

# =============================================================================
# VENUE BUSINESS STATUS METRICS (from Google Places API)
# =============================================================================

# Venues by business status
VENUES_BY_BUSINESS_STATUS = Counter(
    "venues_by_business_status_total",
    "Total number of venues checked by business status",
    ["status"],  # status: operational, closed_temporarily, closed_permanently, unknown
)

# Legacy hard-removal counter retained for dashboards; the soft-delete path
# must not increment it.
VENUES_PERMANENTLY_CLOSED_REMOVED = Counter(
    "venues_permanently_closed_removed_total",
    "Legacy counter for permanently closed venues hard-removed from database",
)

# Current count of permanently closed venues detected (snapshot)
VENUES_PERMANENTLY_CLOSED_DETECTED = Gauge(
    "venues_permanently_closed_detected",
    "Number of permanently closed venues detected in last refresh",
)

# Legacy hard-removal counter retained for dashboards; temporarily closed venues
# stay active and this must not increment.
VENUES_TEMPORARILY_CLOSED_REMOVED = Counter(
    "venues_temporarily_closed_removed_total",
    "Legacy counter for temporarily closed venues hard-removed from database",
)

# Current count of temporarily closed venues detected (snapshot)
VENUES_TEMPORARILY_CLOSED_DETECTED = Gauge(
    "venues_temporarily_closed_detected",
    "Number of temporarily closed venues detected in last refresh",
)

# Soft-deleted venues retained for troubleshooting
VENUES_SOFT_DELETED_TOTAL = Counter(
    "venues_soft_deleted_total",
    "Total number of venues soft-deprecated and retained in Redis",
    ["reason", "source"],
)

# Current deprecated venue count
VENUES_DEPRECATED_TOTAL = Gauge(
    "venues_deprecated_total",
    "Number of venues marked as deprecated and retained in Redis",
)

# Current deprecated venue count broken down by rejection reason. Lets Grafana
# show *why* venues were vetoed (e.g. ineligible_google_type vs
# ineligible_name_keyword vs google_places_closed_permanently).
VENUES_DEPRECATED_BY_REASON = Gauge(
    "venues_deprecated_by_reason",
    "Number of deprecated venues grouped by deprecated_reason",
    ["reason"],
)

# Current active venue count
VENUES_ACTIVE_TOTAL = Gauge(
    "venues_active_total",
    "Number of venues eligible for serving and enrichment",
)

# =============================================================================
# APIFY INSTAGRAM DISCOVERY METRICS
# =============================================================================

# API call counter
APIFY_API_CALLS_TOTAL = Counter(
    "apify_api_calls_total",
    "Total number of Apify API calls",
    ["endpoint", "status"],
)

# API call latency
APIFY_API_CALL_DURATION_SECONDS = Histogram(
    "apify_api_call_duration_seconds",
    "Apify API call latency in seconds",
    ["endpoint"],
    buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

# API error counter by error type
APIFY_API_ERRORS_TOTAL = Counter(
    "apify_api_errors_total",
    "Total number of Apify API errors",
    ["endpoint", "error_type"],
)

# Instagram enrichment results
INSTAGRAM_ENRICHMENT_RESULTS = Counter(
    "instagram_enrichment_results_total",
    "Results of Instagram enrichment operations",
    ["result"],
)

# Venues with Instagram handle (snapshot gauge)
INSTAGRAM_VENUES_WITH_HANDLE = Gauge(
    "instagram_venues_with_handle",
    "Number of venues with a discovered Instagram handle",
)

# Instagram validation confidence score distribution
INSTAGRAM_VALIDATION_SCORES = Histogram(
    "instagram_validation_scores",
    "Distribution of Instagram validation confidence scores",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# Estimated Apify cost (cumulative)
INSTAGRAM_APIFY_COST_ESTIMATE = Counter(
    "instagram_apify_cost_estimate_usd",
    "Estimated cumulative Apify API cost in USD",
)

# =============================================================================
# SERPAPI METRICS
# =============================================================================

# API call counter
SERPAPI_API_CALLS_TOTAL = Counter(
    "serpapi_api_calls_total",
    "Total number of SerpApi API calls",
    ["endpoint", "status"],  # endpoint: resolve_data_id, fetch_photos; status: success, error
)

# API call latency
SERPAPI_API_CALL_DURATION_SECONDS = Histogram(
    "serpapi_api_call_duration_seconds",
    "SerpApi API call latency in seconds",
    ["endpoint"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# API error counter by error type
SERPAPI_API_ERRORS_TOTAL = Counter(
    "serpapi_api_errors_total",
    "Total number of SerpApi API errors",
    ["endpoint", "error_type"],  # error_type: http_error, timeout, quota_exceeded
)

# =============================================================================
# MENU ENRICHMENT METRICS
# =============================================================================

# Menu photo enrichment results
MENU_PHOTO_ENRICHMENT_RESULTS = Counter(
    "menu_photo_enrichment_results_total",
    "Results of menu photo enrichment operations",
    ["result"],  # result: enriched, cached, no_place_id, no_photos_found, error, credit_exhausted
)

# Venues with menu photos (snapshot gauge)
MENU_VENUES_WITH_PHOTOS = Gauge(
    "menu_venues_with_photos",
    "Number of venues with cached menu photos",
)

# Menu photos stored total
MENU_PHOTOS_STORED_TOTAL = Counter(
    "menu_photos_stored_total",
    "Total number of menu photos stored in S3",
)

# Menu extraction results
MENU_EXTRACTION_RESULTS = Counter(
    "menu_extraction_results_total",
    "Results of menu data extraction operations",
    ["result"],  # result: extracted, cached, no_photos, error
)

# Venues with extracted menu data (snapshot gauge)
MENU_VENUES_WITH_DATA = Gauge(
    "menu_venues_with_data",
    "Number of venues with extracted menu data",
)

# Menu items extracted total
MENU_ITEMS_EXTRACTED_TOTAL = Counter(
    "menu_items_extracted_total",
    "Total number of menu items extracted across all venues",
)

# S3 upload metrics
S3_UPLOADS_TOTAL = Counter(
    "s3_uploads_total",
    "Total number of S3 upload operations",
    ["status"],  # status: success, error
)

S3_UPLOAD_DURATION_SECONDS = Histogram(
    "s3_upload_duration_seconds",
    "S3 upload latency in seconds",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# OpenAI API metrics
OPENAI_API_CALLS_TOTAL = Counter(
    "openai_api_calls_total",
    "Total number of OpenAI API calls",
    ["endpoint", "status"],
)

OPENAI_API_CALL_DURATION_SECONDS = Histogram(
    "openai_api_call_duration_seconds",
    "OpenAI API call latency in seconds",
    ["endpoint"],
    buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# =============================================================================
# VIBE CLASSIFIER METRICS
# =============================================================================

# Vibe classification results
VIBE_CLASSIFIER_RESULTS = Counter(
    "vibe_classifier_results_total",
    "Results of vibe classification operations",
    ["result"],  # classified, cached, no_photos, error
)

# Stage B trigger tracking
VIBE_CLASSIFIER_STAGE_B_TRIGGERS = Counter(
    "vibe_classifier_stage_b_triggers_total",
    "Number of times Stage B was triggered",
    ["reason"],  # low_confidence, contradictions
)

# Venues with vibe profile (snapshot gauge)
VENUES_WITH_VIBE_PROFILE = Gauge(
    "venues_with_vibe_profile",
    "Number of venues with AI vibe profiles",
)

# Confidence score distribution
VIBE_CLASSIFIER_CONFIDENCE = Histogram(
    "vibe_classifier_confidence",
    "Distribution of vibe classifier confidence scores",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# =============================================================================
# BACKGROUND JOB METRICS
# =============================================================================

# Job run counter
BACKGROUND_JOB_RUNS_TOTAL = Counter(
    "background_job_runs_total",
    "Total number of background job runs",
    ["job_name", "status"],  # status: success, error
)

# Job duration
BACKGROUND_JOB_DURATION_SECONDS = Histogram(
    "background_job_duration_seconds",
    "Background job execution duration in seconds",
    ["job_name"],
    buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

# Last job run timestamp
BACKGROUND_JOB_LAST_RUN_TIMESTAMP = Gauge(
    "background_job_last_run_timestamp_seconds",
    "Unix timestamp of the last successful job run",
    ["job_name"],
)

# Redis projection (RDS -> Redis off-loop projector).
# Run counts/duration use BACKGROUND_JOB_* with job_name="redis_projection";
# these add projection-specific observability.
REDIS_PROJECTION_VENUES = Gauge(
    "redis_projection_venues",
    "Number of active venues projected to Redis on the last projector run",
)

REDIS_PROJECTION_DEPRECATED_REMOVED_TOTAL = Counter(
    "redis_projection_deprecated_removed_total",
    "Total venues removed from the Redis serving set by the projector because "
    "they are deprecated in RDS (B1)",
)

# Eligibility-as-a-view serving layer. The projector reconciles Redis to exactly
# the serving view's set (active AND eligible under the live block-list rules).
SERVING_VIEW_VENUES = Gauge(
    "serving_view_venues",
    "Size of the eligibility serving view (active + eligible venues) on the last "
    "projector run",
)

# Active venues currently dropped from serving by the Recife-metro geo-fence
# (coordinates outside the enabled box). Reversible serve-time filter — these
# venues stay active in RDS and re-enter serving if the box is widened/disabled.
# Distinct from SERVING_VIEW_VENUES (which conflates geo with type/name eligibility)
# so an operator can see the geo-fence's specific effect. Set on each projector run.
VENUES_GEO_EXCLUDED = Gauge(
    "venues_geo_excluded",
    "Active venues excluded from serving by the geo-fence (outside the enabled box) "
    "on the last projector run",
)

REDIS_PROJECTION_REMOVED_TOTAL = Counter(
    "redis_projection_removed_total",
    "Total venues reconciled out of the Redis serving set by the projector "
    "because they are not in the serving view (deprecated OR active-but-ineligible)",
)

# Venues flipped active again (deprecated_* cleared). Emitted by the one-time
# eligibility-serving-view migration that reactivates eligibility_filter-deprecated
# venues so the view governs them; `source` is the prior deprecated_source.
VENUES_REACTIVATED_TOTAL = Counter(
    "venues_reactivated_total",
    "Total venues reactivated (lifecycle flipped active, deprecated_* cleared)",
    ["source"],
)

# =============================================================================
# VENUE DATA QUALITY METRICS
# =============================================================================

# Total venues in cache
VENUES_TOTAL = Gauge(
    "venues_total",
    "Total number of venues in the cache",
)

# Venues by attribute presence
VENUES_WITH_ATTRIBUTE = Gauge(
    "venues_with_attribute",
    "Number of venues with a specific attribute populated",
    ["attribute"],  # attribute: address, lat_lng, rating, reviews, price_level, type, dwell_time, forecast
)

# Venues by type
VENUES_BY_TYPE = Gauge(
    "venues_by_type",
    "Number of venues by venue type",
    ["venue_type"],
)

# Venues with live forecast data
VENUES_WITH_LIVE_FORECAST = Gauge(
    "venues_with_live_forecast",
    "Number of venues with available live busyness data",
)

# Venues with weekly forecast data
VENUES_WITH_WEEKLY_FORECAST = Gauge(
    "venues_with_weekly_forecast",
    "Number of venues with weekly forecast data cached",
)

# Live forecast availability rate (venues with live / total venues)
VENUES_LIVE_FORECAST_AVAILABILITY_RATIO = Gauge(
    "venues_live_forecast_availability_ratio",
    "Ratio of venues with live forecast data to total venues (0-1)",
)

# Serve-time live-busyness freshness outcomes (nearby-serve minified path).
# served: a fresh live value was served as-is.
# suppressed_stale: a present live value was omitted because its payload age
#   exceeded the freshness window (downstream falls back to forecast).
# suppressed_unparseable: a present live value was omitted because its
#   venue_current_gmttime was missing/unparseable (fail toward forecast).
VENUE_SERVE_LIVE_BUSYNESS_TOTAL = Counter(
    "venue_serve_live_busyness_total",
    "Live busyness outcomes when serving nearby venues",
    ["outcome"],  # served | suppressed_stale | suppressed_unparseable
)

# Age (minutes) of the live forecast payload at serve time, split by outcome.
# Lets you tell "really stale" from normal refresh desync: with the window set to
# ~2x the refresh cadence, a healthy venue is re-touched well inside it, so
# suppressed_stale ages clustered just past the window are pipeline desync while a
# long tail (hours) is a venue whose refresh is genuinely failing/skipped.
VENUE_SERVE_LIVE_FORECAST_AGE_MINUTES = Histogram(
    "venue_serve_live_forecast_age_minutes",
    "Age in minutes of the live forecast payload at nearby-serve time",
    ["outcome"],  # served | suppressed_stale
    buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 120, 240),
)

# =============================================================================
# REFRESH OPERATION METRICS
# =============================================================================

# Venues discovered in last refresh
REFRESH_VENUES_DISCOVERED = Gauge(
    "refresh_venues_discovered",
    "Number of venues discovered in the last refresh operation",
    ["location"],  # location identifier
)

# Venues upserted in last refresh
REFRESH_VENUES_UPSERTED = Gauge(
    "refresh_venues_upserted",
    "Number of venues successfully upserted in the last refresh",
    ["operation"],  # operation: venue_filter, live_forecast, weekly_forecast
)

# Duplicates skipped during refresh
REFRESH_DUPLICATES_SKIPPED = Counter(
    "refresh_duplicates_skipped_total",
    "Total number of duplicate venues skipped during refresh",
    ["reason"],  # reason: duplicate_id, duplicate_name, no_id_or_name
)

# Live forecast fetch results
LIVE_FORECAST_FETCH_RESULTS = Counter(
    "live_forecast_fetch_results_total",
    "Results of live forecast fetch operations",
    ["result"],  # result: cached, deleted_not_ok, deleted_not_available, error
)

# Weekly forecast fetch results
WEEKLY_FORECAST_FETCH_RESULTS = Counter(
    "weekly_forecast_fetch_results_total",
    "Results of weekly forecast fetch operations",
    ["result"],  # result: cached, skipped_not_ok, error
)

# =============================================================================
# PRIORITY-BOUNDED REFRESH + MONTHLY UNIQUE-VENUE LEDGER METRICS
# =============================================================================

# Distinct venues touched against BestTime's monthly unique-venue cap, by month.
BESTTIME_UNIQUE_VENUES_TOUCHED = Gauge(
    "besttime_unique_venues_touched",
    "Distinct venue_ids interacted with via BestTime this calendar month "
    "(counts against BestTime's monthly unique-venue cap)",
    ["year_month"],
)

# BestTime reads refused by the monthly ledger before the network call.
BESTTIME_READ_SKIPPED_TOTAL = Counter(
    "besttime_read_skipped_total",
    "BestTime reads skipped before the network call",
    ["reason"],  # reason: monthly_cap
)

# Venues selected for refresh per run (bounded by refresh_budget).
REFRESH_SELECTED_TOTAL = Counter(
    "refresh_selected_total",
    "Total venues selected for priority-bounded refresh",
    ["job"],  # job: live_forecast, weekly_forecast
)

# =============================================================================
# DATA QUALITY STATS (SNAPSHOT GAUGES)
# =============================================================================

# Average rating across all venues
VENUES_AVERAGE_RATING = Gauge(
    "venues_average_rating",
    "Average rating across all venues with ratings",
)

# Average reviews count
VENUES_AVERAGE_REVIEWS = Gauge(
    "venues_average_reviews",
    "Average review count across all venues with reviews",
)

# Price level distribution
VENUES_BY_PRICE_LEVEL = Gauge(
    "venues_by_price_level",
    "Number of venues by price level",
    ["price_level"],  # 1, 2, 3, 4, unknown
)

# Distribution of which rule produced the served price tier. Lets us watch the
# enum-vs-range-fallback mix (expect enum to dominate, range to fill the enum-less
# tail) and detect regressions after the price-signal re-source.
VENUES_BY_PRICE_LEVEL_SOURCE = Gauge(
    "venues_by_price_level_source",
    "Number of venues by the source that produced the served price tier",
    ["source"],  # google_enum, google_range, besttime, none
)

# =============================================================================
# ADD-VENUE-BY-ADDRESS + MONTHLY BUDGET METRICS
# =============================================================================

ADD_VENUE_BY_ADDRESS_TOTAL = Counter(
    "add_venue_by_address_total",
    "Outcomes of POST /admin/venues/by-address",
    ["result"],  # created | already_exists | matched_via_geo_fallback |
                 # quota_exhausted | besttime_monthly_cap | besttime_error |
                 # besttime_bad_response | besttime_rejected_no_geo_match |
                 # validation_error
)

INVENTORY_SYNC_VENUES_TOTAL = Counter(
    "inventory_sync_venues_total",
    "Per-venue outcomes during the monthly BestTime inventory sync",
    ["result"],  # seen | upserted | skipped | error
)

INVENTORY_SYNC_RUNS_TOTAL = Counter(
    "inventory_sync_runs_total",
    "Outcomes of the monthly BestTime inventory sync runs",
    ["outcome"],  # ok | partial | failed
)

DISCOVERY_SKIPPED_DUE_TO_MONTHLY_CAP_TOTAL = Counter(
    "discovery_skipped_due_to_monthly_cap_total",
    "Discovery cycles or batches skipped because the monthly new-venue cap was reached",
)

VENUE_MONTHLY_NEW_COUNT = Gauge(
    "venue_monthly_new_count",
    "Current month's running count of new venue additions to the BestTime account inventory",
)

LIVE_REFRESH_INTERVAL_MINUTES = Gauge(
    "live_refresh_interval_minutes",
    "Currently effective live_forecast_refresh interval in minutes (admin override or settings default)",
)

# =============================================================================
# ENGAGEMENT METRICS
# =============================================================================

# Outcomes of POST /v1/sessions (app-activity write-through). The raw user_id is
# never a label — only the success/error result is recorded.
ENGAGEMENT_SESSION_TOTAL = Counter(
    "engagement_session_total",
    "Outcomes of POST /v1/sessions app-activity recordings",
    ["result"],  # success | error
)

# =============================================================================
# APPLICATION INFO
# =============================================================================

APP_INFO = Info(
    "csserver",
    "CS-Server application information",
)

# Set application info at module load
APP_INFO.info({
    "version": "1.0.0",
    "description": "Venue discovery and crowd tracking service",
})
