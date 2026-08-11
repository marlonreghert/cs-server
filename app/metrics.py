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

# Client-side pacing + 429 handling for the BestTime venue-search family
# (POST /forecasts create, /venues/filter, /venues/search, /venues/progress),
# which BestTime limits to 30 req/min and 300 req/hour.
BESTTIME_SEARCH_RATE_LIMIT_TOTAL = Counter(
    "besttime_search_rate_limit_total",
    "BestTime venue-search rate-limit events",
    ["endpoint", "event"],  # event: waited (paced before send),
    # retry_429 (server 429, retrying),
    # rejected (wait budget exhausted)
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

# On-demand venue photo resolution (POST /internal/venues/{id}/photos/resolve):
# resolves a single venue's Google photos to FRESH, KEYLESS googleusercontent.com
# URLs and caches them under venue_photos_fresh_v1:*. The Google Place Details +
# per-photo media calls also emit GOOGLE_PLACES_API_* under
# endpoint="place_photos" / endpoint="place_photo_media".
VENUE_PHOTO_RESOLVE_TOTAL = Counter(
    "venue_photo_resolve_total",
    "Outcomes of on-demand venue photo resolution",
    ["result"],  # resolved (>=1 photo, full Google resolve) | empty (no place_id
    # or zero photos) | error (Google exception) | cache_hit (served
    # from the cached entry, no Google call) | upgraded (cached entry
    # held fewer than requested; re-resolved and overwritten)
)

VENUE_PHOTO_RESOLVE_DURATION_SECONDS = Histogram(
    "venue_photo_resolve_duration_seconds",
    "Latency of on-demand venue photo resolution (Google fetch + cache write)",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Photos actually fetched from Google (billed media calls) across on-demand
# resolves. VENUE_PHOTO_RESOLVE_TOTAL counts *resolve calls*; this counts
# *photos* — a cache_hit/empty/error outcome contributes 0, a resolve of
# max_photos=N contributes up to N. This is the number that tracks the
# invoice: watch it, not the resolve count.
VENUE_PHOTOS_FETCHED_TOTAL = Counter(
    "venue_photos_fetched_total",
    "Photos actually fetched (billed Google Places media calls) via on-demand "
    "venue photo resolution",
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

# Poll-budget exhaustion, broken down by the last NON-TERMINAL status the run was
# seen in. A separate series rather than a third label on APIFY_API_ERRORS_TOTAL:
# that counter is shared by four Apify clients across 14 call sites, and widening
# its label set would churn all of them for a dimension only this path has.
#
# The distinction is the whole point of the metric. READY means the run was still
# queued and never started — lower the concurrency. RUNNING means it started and
# was genuinely slow — raise the budget. Same symptom, opposite fixes, and
# without this label they are indistinguishable after the fact.
APIFY_POLL_TIMEOUTS_TOTAL = Counter(
    "apify_poll_timeouts_total",
    "Apify runs still non-terminal when the poll budget was exhausted",
    ["endpoint", "last_status"],
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
    [
        "endpoint",
        "status",
    ],  # endpoint: resolve_data_id, fetch_photos; status: success, error
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
    [
        "result"
    ],  # result: enriched, cached, no_place_id, no_photos_found, error, credit_exhausted
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

# =============================================================================
# Data lake (raw external-API responses archived to S3)
# =============================================================================
# Deliberately separate from the S3_UPLOAD_* metrics above: those count menu
# photo uploads, and folding two unrelated write paths into one series would
# make both dashboards lie.

DATALAKE_RECORDS_ENQUEUED_TOTAL = Counter(
    "datalake_records_enqueued_total",
    "Raw API responses accepted for archival to the data lake",
    ["source", "dataset"],
)

DATALAKE_RECORDS_DROPPED_TOTAL = Counter(
    "datalake_records_dropped_total",
    "Raw API responses that never reached the data lake",
    ["source", "dataset", "reason"],  # reason: queue_full, serialize_error,
    # flush_failed, unexpected
)

DATALAKE_FLUSH_TOTAL = Counter(
    "datalake_flush_total",
    "Data lake object uploads",
    ["dataset", "status"],  # status: success, error
)

DATALAKE_FLUSH_DURATION_SECONDS = Histogram(
    "datalake_flush_duration_seconds",
    "Data lake object upload latency in seconds",
    ["dataset"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

DATALAKE_FLUSH_BYTES_TOTAL = Counter(
    "datalake_flush_bytes_total",
    "Compressed bytes written to the data lake",
    ["dataset"],
)

# Queue backlog: rises when the flusher cannot keep up, and a full queue is what
# turns into datalake_records_dropped_total{reason="queue_full"}.
DATALAKE_QUEUE_DEPTH = Gauge(
    "datalake_queue_depth",
    "Records buffered in the data lake writer queue",
)

# Staleness alert source: time() - this > 1h while archival is enabled means the
# lake stopped receiving data even though nothing is visibly erroring.
DATALAKE_LAST_SUCCESS_TIMESTAMP = Gauge(
    "datalake_last_success_timestamp",
    "Unix timestamp of the last successful data lake upload",
)

# =============================================================================
# Venue media archive (Google photos -> S3 media/ prefix)
# =============================================================================
# Google bills per photo request, so MEDIA_ARCHIVE_PHOTOS_STORED_TOTAL doubles as
# the run's cost meter: photos stored x the per-request price is the Google spend,
# readable in Grafana without opening a log.

MEDIA_ARCHIVE_RUNS_TOTAL = Counter(
    "media_archive_runs_total",
    "Venue photo archive runs",
    # status: success (every selected venue reached a good terminal state),
    #         partial (completed, but some venue failed/timed out/found nothing),
    #         error   (did not complete: credit exhausted, aborted, exception)
    #
    # `partial` exists because this counter previously only ever emitted
    # "success" — from a single hardcoded call site — so a run that archived 1 of
    # 8 venues looked identical to a perfect one, and "error" was dead.
    ["source", "status"],
)

MEDIA_ARCHIVE_VENUES_TOTAL = Counter(
    "media_archive_venues_total",
    "Venues processed by the photo archive, by outcome",
    # result: archived, skipped_existing, no_place_id, google_error, info_only,
    #         timeout, no_query, no_result
    #
    # These are deliberately distinct, and `no_match` was retired because it
    # absorbed all three. `no_query` = the venue has no name/address to search
    # with, so nothing was spent and a re-run cannot help. `no_result` = the
    # source was called and BILLED and found nothing, so a re-run might. `timeout`
    # = the source was still working when we stopped waiting. Conflating them
    # once reported 35 mid-scrape venues as "not on Google Maps" and sent an
    # investigation after the wrong cause entirely.
    ["source", "result"],
)

MEDIA_ARCHIVE_PHOTOS_STORED_TOTAL = Counter(
    "media_archive_photos_stored_total",
    "Photos stored in the media archive",
    ["source"],
)

MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL = Counter(
    "media_archive_photo_failures_total",
    "Photos that could not be archived",
    ["source", "reason"],  # reason: download_error, store_error, too_large
)

MEDIA_ARCHIVE_BYTES_STORED_TOTAL = Counter(
    "media_archive_bytes_stored_total",
    "Bytes written to the media archive",
    ["source"],
)

MEDIA_ARCHIVE_RUN_DURATION_SECONDS = Histogram(
    "media_archive_run_duration_seconds",
    "Venue photo archive run duration in seconds",
    ["source"],
    buckets=(1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0),
)

MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP = Gauge(
    "media_archive_last_success_timestamp",
    "Unix timestamp of the last successful media archive run",
)

# Deliberately NO job_id label anywhere below: a run id is unbounded cardinality
# and would degrade the whole metrics store. Per-run detail lives in the logs
# (queryable by job_id in Loki) and in the run record.

MEDIA_ARCHIVE_GOOGLE_CALLS_TOTAL = Counter(
    "media_archive_google_calls_total",
    "Google photo requests issued by the archive pipeline (the billed unit)",
    ["source"],
)

MEDIA_ARCHIVE_THROTTLED_TOTAL = Counter(
    "media_archive_throttled_total",
    "Throttled or transient-server responses from Google, by kind",
    ["source", "reason"],  # reason: 429, 5xx
)

MEDIA_ARCHIVE_RATE_LIMIT_WAIT_SECONDS = Histogram(
    "media_archive_rate_limit_wait_seconds",
    "Time the archive pipeline waited on its own rate limiter",
    ["source"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

MEDIA_ARCHIVE_VENUES_SELECTED = Gauge(
    "media_archive_venues_selected",
    "Venues selected by the most recent archive run, after caps",
    ["source"],
)

MEDIA_ARCHIVE_VENUES_TRUNCATED_TOTAL = Counter(
    "media_archive_venues_truncated_total",
    "Eligible venues dropped by the max_venues cap",
    ["source"],
)

MEDIA_ARCHIVE_ESTIMATED_COST_USD = Gauge(
    "media_archive_estimated_cost_usd",
    "Estimated Google cost of the most recently estimated archive run, in USD",
    ["source"],
)

MEDIA_ARCHIVE_VENUES_WITH_MEDIA = Gauge(
    "media_archive_venues_with_media",
    "Venues archived by the most recent run — catalog photo coverage",
    ["source"],
)

# Instagram-specific image outcomes, split out from the generic
# media_archive_photo_failures_total{reason="download_error"} so an EXPIRY WAVE
# is visible as itself. Instagram signs its CDN urls with a short-lived
# signature: if the scrape-then-download gap ever widens, every image in the
# run starts failing the same way, and that must not read as an ordinary
# download error.
INSTAGRAM_ARCHIVE_IMAGES_TOTAL = Counter(
    "instagram_archive_images_total",
    "Instagram post images, by what happened to them",
    ["result"],  # downloaded | expired | failed | skipped_cap
)

# Photo classification. `fallbacks` is the one to watch: it separates "the model
# failed" (photos keep their source category) from "the model was unsure" (filed
# as `other`), which look identical in the category counts but mean opposite
# things about whether the classifier is working.
PHOTO_CLASSIFICATION_TOTAL = Counter(
    "photo_classification_total",
    "Photos classified, by the category they were filed under",
    ["category"],
)

PHOTO_CLASSIFICATION_FALLBACKS_TOTAL = Counter(
    "photo_classification_fallbacks_total",
    "Photos that did not get a confident category, by reason",
    ["reason"],
)

PHOTO_CLASSIFICATION_COST_USD = Counter(
    "photo_classification_cost_usd",
    "Cumulative vision-model spend on photo classification, in USD, priced from "
    "the token counts the API reports",
)

# What the model ACTUALLY consumed, straight off `response.usage` — not a
# per-photo guess. Input and output are separated because they are billed at
# very different rates (output is several times dearer) and because they move
# for different reasons: input grows when the prompt or the batch size grows,
# output grows when the schema grows. A cost surprise is always one or the
# other, and a single total cannot tell you which.
OPENAI_TOKENS_TOTAL = Counter(
    "openai_tokens_total",
    "Tokens reported by the OpenAI API, by endpoint and direction",
    ["endpoint", "direction"],
)

# =============================================================================
# Instagram handle cascade
# =============================================================================
# The number this feature exists to move: tier attempts versus PAID calls. A
# handle resolved from data we already own costs nothing; only apify_search bills.

INSTAGRAM_CASCADE_RESULTS_TOTAL = Counter(
    "instagram_cascade_results_total",
    "Outcomes of the Instagram handle cascade",
    ["source", "result"],  # result: accepted, low_confidence, not_found, error
)

INSTAGRAM_CASCADE_TIER_ATTEMPTS_TOTAL = Counter(
    "instagram_cascade_tier_attempts_total",
    "Times each cascade source was consulted",
    ["source"],
)

INSTAGRAM_CASCADE_PAID_CALLS_TOTAL = Counter(
    "instagram_cascade_paid_calls_total",
    "Paid Instagram search calls made by the cascade",
)

INSTAGRAM_HANDLE_REJECTED_TOTAL = Counter(
    "instagram_handle_rejected_total",
    "Candidate URLs rejected before becoming a handle",
    ["reason"],  # link_shim, non_profile_path, empty, malformed, not_instagram
)

# `unknown` is a probe failure, NOT evidence of absence — kept as its own label
# so a spike (Instagram changing its crawler response) is visible immediately.
# A candidate discarded before it is ever weighed. This exists because the loss
# was previously INVISIBLE: a per-profile WARNING and an empty list, which is
# indistinguishable from "the search found nothing". When Apify changed
# externalUrls from a string to an object, every linked profile was dropped and
# the pipeline looked merely unlucky for weeks.
INSTAGRAM_SEARCH_CANDIDATES_DROPPED_TOTAL = Counter(
    "instagram_search_candidates_dropped_total",
    "Instagram search results discarded before becoming a candidate",
    ["reason"],  # parse_error, no_username, error_item
)

INSTAGRAM_PROFILE_PROBE_TOTAL = Counter(
    "instagram_profile_probe_total",
    "Instagram profile existence probes",
    ["result"],  # present, absent, unknown, blocked
)

INSTAGRAM_JUDGE_TOTAL = Counter(
    "instagram_judge_total",
    "LLM adjudications of ambiguous Instagram candidates",
    ["mode", "verdict"],  # mode: vision_both, vision_partial, text_only, unavailable
)

# =============================================================================
# Pipeline run identity
# =============================================================================
# One series per RECENT run, bounded by the registry's ring (see
# app/services/pipeline_run_registry.py). The value is the run's start time, so
# `sort_desc` orders runs newest-first and Grafana can build a run picker with
# `label_values(pipeline_run_info{pipeline="X"}, job_id)`.
#
# job_id is a label HERE and deliberately not in Loki: Prometheus lets the
# registry bound cardinality with .remove(); a Loki label would mean one stream
# per run on a 2.9.8/schema-v11 stack with no structured metadata.
PIPELINE_RUN_INFO = Gauge(
    "pipeline_run_info",
    "Recent pipeline runs; value is the run start time (unix seconds)",
    ["pipeline", "job_id", "status"],  # status: running, success, error
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

# The shared scheduler+admin concurrency guard (app/services/job_lock.py):
# a trigger refused because the OTHER side (scheduler vs admin, or vice
# versa) already holds the lock for this job_name. Visibility into how often
# an admin operator races a scheduled paid-refresh cycle.
JOB_LOCK_REJECTED_TOTAL = Counter(
    "job_lock_rejected_total",
    "Total job starts refused because the shared concurrency lock for that "
    "job_name was already held",
    ["job_name", "source"],  # source: scheduler | admin
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

# Closed-venue detection: venues whose newest review reports permanent closure.
# A third reversible serve-time filter alongside eligibility and the geo-fence —
# the venue stays active in RDS and returns to serving once newer evidence
# clears its signal. Split by confidence because only `high` excludes from
# serving; `low` is recorded for operator review and must be visible without
# being mistaken for an exclusion.
VENUES_CLOSED_FLAGGED = Gauge(
    "venues_closed_flagged",
    "Venues currently flagged as permanently closed by review evidence, by "
    "confidence (only high-confidence signals exclude from serving)",
    ["confidence"],
)

CLOSURE_DETECTION_RUNS_TOTAL = Counter(
    "closure_detection_runs_total",
    "Closure-detection runs by outcome",
    ["outcome"],
)

CLOSURE_DETECTION_DURATION_SECONDS = Histogram(
    "closure_detection_duration_seconds",
    "Wall-clock duration of a closure-detection run",
)

CLOSURE_DETECTION_ERRORS_TOTAL = Counter(
    "closure_detection_errors_total",
    "Per-venue closure-detection failures (isolated; the run continues)",
)

REDIS_PROJECTION_REMOVED_TOTAL = Counter(
    "redis_projection_removed_total",
    "Total venues reconciled out of the Redis serving set by the projector "
    "because they are not in the serving view (deprecated OR active-but-ineligible)",
)

# Per-entity delete propagation: for a servable venue, when an enrichment /
# weekly-day / live record is absent or soft-deleted in RDS, the projector
# deletes the matching Redis key so Redis converges to RDS in both directions.
# Counts REAL removals only (Redis DEL's own removed-count, zero extra
# round-trips) -- a venue that simply never had a sparse enrichment type
# (menu_data, instagram, ...) does not inflate this counter, so a sustained
# rise is a genuine signal of a deletion storm, not baseline noise.
REDIS_PROJECTION_ENTITY_DELETES_TOTAL = Counter(
    "redis_projection_entity_deletes_total",
    "Total Redis keys actually removed by the projector because the matching "
    "RDS row is absent or soft-deleted, labeled by entity",
    ["entity"],
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
    [
        "attribute"
    ],  # attribute: address, lat_lng, rating, reviews, price_level, type, dwell_time, forecast
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
    # reason: duplicate_id, duplicate_name, no_id_or_name, geo_name_duplicate
    ["reason"],
)

# Live forecast fetch results
LIVE_FORECAST_FETCH_RESULTS = Counter(
    "live_forecast_fetch_results_total",
    "Results of live forecast fetch operations",
    # result: cached, deleted_not_ok, deleted_not_available, error,
    # skipped_venue_absent (benign: the live-forecast payload's venue_id has no
    # row in venues.venue — RdsVenueStore.upsert_live_forecast no-ops instead of
    # raising ForeignKeyViolation; see venues_refresher_service.py)
    ["result"],
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
    ["result"],  # created | created_recovered_timeout | already_exists |
    # matched_via_geo_fallback | quota_exhausted |
    # besttime_monthly_cap | besttime_error |
    # besttime_bad_response | besttime_rejected_no_geo_match |
    # timeout_unconfirmed | validation_error |
    # geo_link_undone | geo_link_undo_rejected |
    # created_google_only (the geo fallback found no match, the flag
    #   is on, and Google resolved the venue) |
    # google_only_enrichment_failed (the flag is on but Google could
    #   not resolve the place or fetch its details)
    #
    # NOTE: with the Google-only flag off, the geo-fallback-no-match
    # branch still emits besttime_rejected_no_geo_match (unchanged) —
    # it does NOT get its own label, so the flag-off deploy stays a
    # genuine metric no-op.
)

ADD_VENUE_INSTAGRAM_TOTAL = Counter(
    "add_venue_instagram_total",
    "Outcomes of the Instagram handle discovery run inline at venue-add time",
    ["result"],  # found | low_confidence | not_found | timeout | skipped | error
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

# Active venues catalogued from Google metadata alone (venue_source='google_only',
# no BestTime id, never selected for BestTime refresh). Refreshed by the same
# stats pass as VENUES_WITH_ATTRIBUTE so the minority class's growth rate is
# watchable (see plans/260804_add-venue-google-only.md Rollout And Rollback).
VENUES_GOOGLE_ONLY_TOTAL = Gauge(
    "venues_google_only_total",
    "Active venues catalogued from Google Places metadata alone (venue_source='google_only')",
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

# Outcomes of DELETE /v1/user-data (account-deletion erasure). A silently broken
# erasure path is an App Store compliance risk, not just a bug — it must be
# observable here rather than inferred from client retries. As with the session
# counter, the raw user_id is never a label.
ENGAGEMENT_USER_DELETION_TOTAL = Counter(
    "engagement_user_deletion_total",
    "Outcomes of DELETE /v1/user-data account-data erasures",
    ["result"],  # ok | invalid | error
)

# A retried hot-like write (vibes_bot retries on 5xx per the engagement_router
# contract) is deduped by the RDS unique index on
# (user_pseudo, venue_id, business_period) + ON CONFLICT DO NOTHING. Counts
# conflict-suppressed inserts so a retry storm is visible without inflating
# the durable hot_like_event row count.
ENGAGEMENT_HOT_LIKE_DEDUP_TOTAL = Counter(
    "engagement_hot_like_dedup_total",
    "Total hot-like writes suppressed as duplicates of an existing "
    "(user, venue, business_period) row via ON CONFLICT DO NOTHING",
)

# POST /v1/blocks: how often blocking a venue also cleared an existing
# favorite for that (user, venue) pair (the atomic block+auto-unfavorite
# transaction — see RdsVenueStore.block_venue). Plain counter, no labels, same
# style as ENGAGEMENT_HOT_LIKE_DEDUP_TOTAL — the first engagement counter tied
# to the block path specifically (favorites has none today).
ENGAGEMENT_BLOCK_FAVORITE_CLEARED_TOTAL = Counter(
    "engagement_block_favorite_cleared_total",
    "Total POST /v1/blocks calls that atomically cleared an existing favorite "
    "for the same (user, venue) pair",
)

# =============================================================================
# EVENT VENUE TARGETING (plans/260804_event-venue-targeting.md)
# =============================================================================

# Per-venue verdicts from each stage of the event targeting run. `stage` is
# "category" (free, whole catalog) or "evidence" (bounded, top N by priority).
EVENT_TARGETING_VENUES_TOTAL = Counter(
    "event_targeting_venues_total",
    "Event venue targeting verdicts per stage",
    ["stage", "verdict"],
)

# A malformed admin_config:event_candidate_categories write must never empty
# the candidate set — this counts every time the loader fell back to the
# in-code defaults, so a bad write is visible instead of silently shrinking
# the next event run's target list.
EVENT_TARGETING_CONFIG_FALLBACK_TOTAL = Counter(
    "event_targeting_config_fallback_total",
    "Times the event candidate category config fell back to in-code defaults",
    ["reason"],
)

# Snapshot of venue_event_profile after the most recent run, by tier AND
# venue_source. The second label is only ever "besttime" or "google_only", so
# it costs nothing, and it answers the question this plan would otherwise leave
# unanswerable: whether the venues BestTime could not forecast are actually
# reaching the evidence gate, or are being silently dropped by a filter meant
# for BestTime refresh (see list_event_candidate_ids_by_priority).
EVENT_CANDIDATE_VENUES = Gauge(
    "event_candidate_venues",
    "Venues in each event-targeting tier, by venue source",
    ["tier", "venue_source"],
)

# =============================================================================
# INSTAGRAM EVENT EXTRACTION (plans/260804_instagram-event-extraction.md)
# =============================================================================

# Every post the job looked at, by what happened to it. `not_event_like` is
# the cost-gate proof: a post filed here never reached the model. `job_id`
# stays out of every label here (unbounded cardinality, §7 of
# docs/venue-retrieval-storage.md) — per-run narrative goes to Loki instead.
EVENT_EXTRACTION_POSTS_TOTAL = Counter(
    "event_extraction_posts_total",
    "Instagram posts examined by event extraction, by outcome and kind",
    ["outcome", "kind"],  # outcome: extracted, not_event_like, no_date,
    # low_confidence, extraction_failed, skipped_seen, unread_time,
    # truncated (a venue post's multi-event response cut off mid-list —
    # plans/260806_venue-post-multi-event.md), weekday_mismatch
    # (the explicit date resolved but disagreed with a stated
    # weekday — plans/260807_date-resolution-correctness.md).
    # A rise in "no_date" after that same change is the date
    # resolver correctly refusing a guess it used to silently
    # make, not a regression — a stale outcome list here is how
    # the next reader misreads a dashboard.
    #
    # kind (plans/260810_post-kind-and-post-extraction-attribution.md
    # §Error Handling): event, promotion, menu, food, other, "unknown"
    # (the model left it missing/blank), "mixed" (a roundup post whose
    # several events do not share one kind), or "not_applicable" (no event
    # was even parsed for this post). WATCH THE EVENT SHARE: if nearly
    # everything still classifies as event, the prompt's precedence is not
    # landing; if almost nothing does, the classifier is eating real
    # events — and that failure is silent everywhere else, since a
    # misclassified event never reaches the review queue.
)

# Cumulative vision-model spend on event extraction, in USD, priced from the
# token counts the API reports — never a per-post guess (see
# OPENAI_TOKENS_TOTAL{endpoint="event_extract"} for the raw counts this is
# derived from). The 9x photo-classification cost-estimate error (§4 of
# docs/venue-retrieval-storage.md) is why nothing here is asserted as fact
# until it is actually measured against a live run.
EVENT_EXTRACTION_COST_USD = Counter(
    "event_extraction_cost_usd",
    "Cumulative vision-model spend on event extraction, in USD",
)

# Snapshot of events.event after the most recent run, by status. Lets an
# operator see the review queue size (pending_review) without a DB console.
EVENTS_TOTAL = Gauge(
    "events_total",
    "Rows in events.event, by status",
    ["status"],
)

# =============================================================================
# PROMOTER EVENTS (plans/260804_instagram-promoter-events.md)
# =============================================================================

# Snapshot of events.promoter_account after the most recent registry change,
# by lifecycle status — the first question this feature has to answer: how
# many discovered handles are still awaiting an operator's decision.
PROMOTER_ACCOUNTS_TOTAL = Gauge(
    "promoter_accounts_total",
    "Rows in events.promoter_account, by status",
    ["status"],
)

PROMOTER_CRAWL_POSTS_TOTAL = Counter(
    "promoter_crawl_posts_total",
    "Promoter posts examined by the promoter crawl, by outcome",
    ["outcome"],  # archived, not_event_like, extracted, extraction_failed,
    # manual_preserved, account_unavailable, truncated
)

# =============================================================================
# MULTI-EVENT POSTS (plans/260806_multi-event-posts.md)
# =============================================================================

# How many events ONE post yielded. A listings account collapsing back to
# one event per post (the regression this feature exists to prevent) shows
# up only in the SHAPE of this distribution, never in a single scalar —
# `promoter_crawl_posts_total{outcome="extracted"}` alone cannot distinguish
# "17 posts, 1 event each" from "1 post, 17 events".
EVENT_EXTRACTION_EVENTS_PER_POST = Histogram(
    "event_extraction_events_per_post",
    "Number of events extracted from a single post",
    buckets=(1, 2, 3, 5, 8, 13, 20),
)

# plans/260811_extract-by-handle.md §Error Handling: a post re-extracted
# after a date/attribution fix supersedes the stale row(s) it previously
# produced (event_reconciliation.reconcile_post_events' existing "an event
# this run did not re-emit" rule — see that module) — but the SAME rule also
# fires on an ORDINARY re-extraction (a post processed twice by the venue_ids/
# event_candidates path, no operator-triggered fix involved). Conflating the
# two would hide extract-by-handle's own §B going wrong (a corrected date
# quietly failing to supersede its stale sibling would look identical to
# "nothing superseded because nothing needed to be", on this counter alone,
# unless the trigger is split out). `trigger`: "handle_reextraction" (this
# feature's mode="handles" path) vs "extraction" (every other path).
EVENT_EXTRACTION_SUPERSEDED_TOTAL = Counter(
    "event_extraction_superseded_total",
    "Event rows moved to status=superseded by a post's own reconciliation, by what triggered the reconciliation",
    ["trigger"],
)

# One malformed entry inside an otherwise-valid multi-event list must be
# skipped and counted, never fatal to its siblings — this is the counter
# that proves the skip actually happened rather than silently vanishing.
EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL = Counter(
    "event_extraction_malformed_events_total",
    "Individual malformed events skipped within an otherwise-valid multi-event extraction",
)

# plans/260808_event-ticket-info-and-attractions.md §Error Handling: one
# malformed attraction (not an object, or with no usable name) is skipped
# and counted, never fatal to its siblings — the SAME shape as
# EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL above, one level down (a defect
# inside one event's attraction list, not the event itself).
EVENT_EXTRACTION_MALFORMED_ATTRACTIONS_TOTAL = Counter(
    "event_extraction_malformed_attractions_total",
    "Individual malformed attraction entries skipped within an otherwise-valid extraction",
)

# plans/260811_post-items-and-categories.md §C/§Error Handling: `category`
# is free text steered toward app.models.post_category's admin-configurable
# vocabulary but never confined to it — this counts every answer that did
# NOT match, labeled by the raw value, so the vocabulary can be grown from
# evidence rather than guesswork. `category` is CAPPED by
# app.models.post_category.record_off_vocabulary_category (the only writer
# of this counter) at `_MAX_TRACKED_OFF_VOCABULARY_LABELS` distinct values,
# bucketing the rest under `OFF_VOCABULARY_OVERFLOW_LABEL` — an unbounded
# label fed directly by model output is a cardinality bomb. WATCH THE
# OFF-VOCABULARY RATE: high means the vocabulary is wrong; near zero means
# the model is being over-steered and everything is forced onto the
# nearest listed word.
POST_CATEGORY_OFF_VOCABULARY_TOTAL = Counter(
    "post_category_off_vocabulary_total",
    "Post-item category answers that did not match the configured vocabulary, by raw value (capped)",
    ["category"],
)

# `method` is what makes this worth reading: whether links are coming from
# exact @-mentions/location tags or from fuzzy name matching is the
# difference between a resolver that is working and one that is guessing
# successfully so far. `result` is auto, queued, unresolved, or manual.
EVENT_VENUE_LINK_TOTAL = Counter(
    "event_venue_link_total",
    "Promoter event venue resolution outcomes, by method and result",
    ["method", "result"],
)

# The operator-load signal. Widened by plans/260807_review-queue-
# completeness-and-venue-names.md: this NO LONGER means "ambiguous promoter
# links" — it is every event awaiting a human decision (anything
# `pending_review`, which every persisted event starts as, plus any promoter
# event still lacking a location decision even once its data is confirmed;
# see VenueRepository.list_events_awaiting_decision). A dashboard built
# against the old, narrower meaning will silently start reading "unconfirmed
# events" instead of "ambiguous promoter links" — re-baseline any alert
# threshold rather than assume the old scale still applies.
EVENT_REVIEW_QUEUE_DEPTH = Gauge(
    "event_review_queue_depth",
    "Events awaiting an operator decision (pending_review, or a promoter "
    "event with no location decision yet)",
)

# plans/260806_event-cover-presign.md — `result` distinguishes a working sign
# (signed) from the three ways it doesn't: no archived cover on the event
# (no_key), no such event (not_found), and MediaArchiveStore.presign()
# returning None (failed). Kept separate from EVENT_VENUE_LINK_TOTAL, whose
# `result` values mean something unrelated (auto/queued/unresolved/manual).
EVENT_COVER_PRESIGN_TOTAL = Counter(
    "event_cover_presign_total",
    "Event cover-photo presign outcomes",
    ["result"],
)

# =============================================================================
# ONE EVENT, MANY POSTS (plans/260807_one-event-many-posts.md)
# =============================================================================

# How many SOURCES (announcing posts) one merged event carries. A campaign
# collapsing back to one source per event — three countdown posts silently
# staying three separate events — is exactly the regression this feature
# exists to prevent, and only the SHAPE of this distribution shows it; a
# single scalar (e.g. "events created today") cannot distinguish "3 posts, 3
# events" from "3 posts, 1 event, 3 sources". Observed once per successful
# merge, with the CANONICAL event's post-merge source count.
EVENT_SOURCES_PER_EVENT = Histogram(
    "event_sources_per_event",
    "Number of sources (announcing posts) attached to one event after a merge",
    buckets=(1, 2, 3, 5, 8, 13, 20),
)

# Every cross-post merge attempt, by WHICH identity found the group and by
# outcome — plans/260811_merge-unresolved-into-resolved-sibling.md's own
# observability ask: the new handle-identity path's volume must be visible
# from the first run, separate from the pre-existing venue-identity path.
#
# `identity="venue"` (the original path, app.services.event_merge.
# compute_event_identity — runtime half of 0026_event_sources's one-time
# historical collapse): `no_identity` covers both a missing venue and a
# missing date; `two_confirmed` is the guard that leaves an ambiguous group
# alone for an operator to resolve instead of guessing which of two
# confirmed rows is "the" event.
#
# `identity="handle"` (compute_handle_identity — attaches a venue-less event
# to a resolved sibling from the SAME account): `no_identity` is a missing
# handle/date; `no_match` is no resolved-or-unresolved counterpart sharing
# the identity; `ambiguous_venue` is the refusal a shared handle mapping to
# more than one venue requires (WATCH this one — a rising count means the
# attribution upstream is unstable, not that this merge is broken);
# `confirmed_member`/`operator_edited` are the SAME per-candidate group
# protections a venue-identity merge already honours, extended rather than
# bypassed, each counted by which reason refused that one candidate.
#
# `identity="menu"` (plans/260811_menu-item-lifecycle.md — app.services.
# event_merge.compute_menu_identity, `(venue_id, normalized title)`, NO
# date): `no_identity` is a missing venue (an unresolved dish, the ONLY gap
# this identity has — there is no date to be missing); `no_match` is no
# other `post_type="menu"` row sharing the identity; `two_confirmed` mirrors
# the venue-identity guard (two confirmed rows for what would be the same
# dish are left alone for an operator). Never `ambiguous_venue`/
# `confirmed_member`/`operator_edited` — a menu item has no handle-identity
# analog (see `app.services.event_merge._merge_menu_item`'s docstring).
EVENT_MERGE_TOTAL = Counter(
    "event_merge_total",
    "Cross-post event identity merge attempts, by identity kind and outcome",
    ["identity", "outcome"],
    # identity=venue: merged, no_identity, no_match, two_confirmed
    # identity=handle: merged, no_identity, no_match, ambiguous_venue,
    #                  confirmed_member, operator_edited
    # identity=menu: merged, no_identity, no_match, two_confirmed
)

# Snapshot of events.post_item (post_type="menu" only) by current-vs-expired
# state, using the DEFAULT expiry window (app.models.menu_lifecycle.
# DEFAULT_MENU_EXPIRY_DAYS) as an approximation — deliberately NOT the live
# admin-configured override: this gauge is refreshed from
# app.services.event_reconciliation.update_events_gauge, shared by both
# extraction paths, one of which (the scheduled shared-handle crawl) runs
# with no Redis client wired in some deployments, and a background
# observability snapshot degrading to "roughly right" is the correct trade
# against adding a hard Redis dependency there. plans/260811_menu-item-
# lifecycle.md's own ask: "watch the expired share" — a rising
# `state="expired"` share means either a venue stopped posting or extraction
# stopped recognising its dishes, and those need telling apart (not this
# gauge's job alone).
MENU_ITEM_FRESHNESS_TOTAL = Gauge(
    "menu_item_freshness_total",
    "post_type='menu' rows by current-vs-expired state (default expiry window)",
    ["state"],
    # state: current, expired
)

# =============================================================================
# SCHEDULED INCREMENTAL INSTAGRAM CRAWL
# (plans/260809_scheduled-incremental-instagram-crawl.md)
# =============================================================================

# Every scheduled crawl attempt, by handle kind, result type, and outcome.
# `handle_kind` is 'venue'|'promoter' (bounded, matching events.crawl_target.
# kind's own CHECK constraint) — NOT the handle itself, which never becomes a
# label here (unbounded-ish in principle, and `crawl_cursor_age_seconds`
# below is where a per-handle label is actually needed and deliberately
# allowed instead).
CRAWL_RUNS_TOTAL = Counter(
    "crawl_runs_total",
    "Scheduled Instagram crawl attempts, by handle kind, result type, and outcome",
    ["handle_kind", "result_type", "outcome"],
    # result_type: posts, reels
    # outcome: success, empty, failed, skipped_disabled, skipped_failures,
    #          skipped_budget, credit_exhausted
)

# The number that maps to money: every BILLED result the actor returned,
# summed regardless of what happened to it afterwards (kept, or dropped as an
# out-of-bound pinned post — §G. A pinned post is billed whether or not it is
# processed, so it must still be counted here).
CRAWL_RESULTS_TOTAL = Counter(
    "crawl_results_total",
    "Instagram results returned by the scheduled crawl (billed, whether kept or dropped)",
    ["result_type"],
)

# §F's monthly ceiling, the direct successor to docs/venue-retrieval-
# storage.md §3's retired "No cron" guarantee. Sampled after every gate
# check (spend or refusal) so a dashboard shows the SAME number the next
# gate check will compare against, not a stale one.
CRAWL_BUDGET_REMAINING = Gauge(
    "crawl_budget_remaining",
    "Results remaining in this calendar month's scheduled-crawl budget",
)

# §Error Handling: "watch this above all" — a cursor that stops advancing
# while runs keep succeeding is the ONLY observable symptom every silent
# failure mode in §B/§E/§G would produce (a wall-clock cursor, a shared
# posts/reels cursor, or a pinned post moving the cursor). `handle` is
# explicitly allowed as a label here (unlike CRAWL_RUNS_TOTAL's
# `handle_kind`): targets are opt-in and few, so its cardinality is bounded
# by how many an operator has scheduled, not by the catalog.
CRAWL_CURSOR_AGE_SECONDS = Gauge(
    "crawl_cursor_age_seconds",
    "Seconds between now and a crawl target's cursor timestamp",
    ["handle", "result_type"],
)

# Whether a scheduled crawl's own archiving step actually ran the photo
# classifier before writing its manifest — an image-only flyer (no caption
# event-marker at all) only reaches extraction when this ran. Distinguishes
# "we classified and there was no flyer" (a real `not_event_like` outcome,
# tracked separately by EVENT_EXTRACTION_POSTS_TOTAL) from "classification
# never ran for this crawl" (classifier not configured, or the target's own
# `classify_images` toggle is off) — the second case is a coverage GAP, not
# a verdict, and must never look identical to the first in a dashboard.
CRAWL_CHAIN_CLASSIFICATION_TOTAL = Counter(
    "crawl_chain_classification_total",
    "Whether the scheduled crawl's archiving step classified images before chaining into extraction",
    ["outcome"],
    # outcome: classified, classification_failed, skipped_target_disabled,
    #          skipped_no_classifier, skipped_no_photos
)

# plans/260810_stream-dedupe-and-venue-attribution.md §Evidence: a reel is
# also a grid post, so the posts and reels streams largely overlap — this
# counts, per run, how many of a LATER stream's kept results duplicated a
# shortcode an EARLIER stream in the same run already returned (posts always
# runs first, so in practice this is reels items already carried by posts).
# Makes the reels stream's marginal value visible ("reels: 16 fetched, 3
# new") instead of assumed.
CRAWL_STREAM_OVERLAP_TOTAL = Counter(
    "crawl_stream_overlap_total",
    "Instagram results a later stream returned that an earlier stream in the same run already supplied",
    ["result_type"],
)

# plans/260810_stream-dedupe-and-venue-attribution.md §C: how a shared
# handle's post was attributed once resolution ran. `single_venue` is the
# common, unchanged case (the handle maps to exactly one venue, resolution
# never runs); `resolved`/`ambiguous` only occur for a handle mapping to
# several venues. Watch `ambiguous`: a shared handle whose posts never
# resolve means the caption signals §C matches on are not present in that
# account's captions — the fix is to change the matching, not loosen the
# floor.
CRAWL_VENUE_ATTRIBUTION_TOTAL = Counter(
    "crawl_venue_attribution_total",
    "How a scheduled crawl attributed a post to a venue",
    ["outcome"],
    # outcome: single_venue, resolved, ambiguous
)

# =============================================================================
# APPLICATION INFO
# =============================================================================

APP_INFO = Info(
    "csserver",
    "CS-Server application information",
)

# Set application info at module load
APP_INFO.info(
    {
        "version": "1.0.0",
        "description": "Venue discovery and crowd tracking service",
    }
)
