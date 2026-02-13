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
    ["endpoint", "error_type"],  # error_type: http_error, timeout, connection_error
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

# =============================================================================
# VENUE BUSINESS STATUS METRICS (from Google Places API)
# =============================================================================

# Venues by business status
VENUES_BY_BUSINESS_STATUS = Counter(
    "venues_by_business_status_total",
    "Total number of venues checked by business status",
    ["status"],  # status: operational, closed_temporarily, closed_permanently, unknown
)

# Permanently closed venues removed
VENUES_PERMANENTLY_CLOSED_REMOVED = Counter(
    "venues_permanently_closed_removed_total",
    "Total number of permanently closed venues removed from database",
)

# Current count of permanently closed venues detected (snapshot)
VENUES_PERMANENTLY_CLOSED_DETECTED = Gauge(
    "venues_permanently_closed_detected",
    "Number of permanently closed venues detected in last refresh",
)

# Temporarily closed venues removed
VENUES_TEMPORARILY_CLOSED_REMOVED = Counter(
    "venues_temporarily_closed_removed_total",
    "Total number of temporarily closed venues removed from database",
)

# Current count of temporarily closed venues detected (snapshot)
VENUES_TEMPORARILY_CLOSED_DETECTED = Gauge(
    "venues_temporarily_closed_detected",
    "Number of temporarily closed venues detected in last refresh",
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
