Feature: RDS as the system of record with Redis serving projection
  As the VibeSense platform
  We must persist every venue pipeline output to an AWS RDS Postgres database as
  the system of record, and keep Redis as a fast serving projection rebuilt from
  RDS, so that public venue serving stays unchanged while all durable state lives
  in RDS — except live busyness, which stays Redis-only to avoid write overload.

  # NOTE: Infrastructure provisioning (RDS, Terraform, AWS SSO, SSM tunnels,
  # networking, DDL/Alembic migrations) is bdd-exempt: infrastructure. This
  # feature covers only the cs-server runtime behavior.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  # ── Write-through: RDS is truth, Redis is the projection ──────────────────
  Scenario: A pipeline venue upsert lands in RDS and projects to Redis
    When a pipeline upserts a venue "v1" named "Bar do Zé"
    And the Redis projector runs
    Then RDS holds venue "v1" as the system of record
    And Redis holds the serving projection for venue "v1"
    And the venue "v1" is returned by nearby serving

  Scenario: Enrichment outputs are persisted to RDS and projected to Redis
    Given a venue "v1" exists in RDS and Redis
    When the pipelines persist google places, instagram, photos, reviews, opening hours, menu, vibe profile, and weekly forecast for "v1"
    And the Redis projector runs
    Then RDS holds each of those records for "v1"
    And the Redis serving projection for "v1" includes every field the nearby response reads

  # ── Live busyness is persisted to RDS too (Redis is just the interface) ───
  Scenario: Live busyness is written to RDS and projected to Redis
    Given a venue "v1" exists in RDS and Redis
    When the live forecast refresh stores live busyness for "v1"
    And the Redis projector runs
    Then RDS holds the current live busyness for "v1"
    And Redis holds the live busyness for "v1" for serving

  # ── Rejection reasons are durable in RDS ──────────────────────────────────
  Scenario: Eligibility soft-delete persists the rejection reason in RDS
    Given a venue "v1" exists in RDS and Redis
    When the eligibility sweep soft-deletes "v1" with reason "ineligible_google_type"
    And the Redis projector runs
    Then RDS records "v1" as deprecated with reason "ineligible_google_type" and source "eligibility_filter"
    And the venue "v1" is excluded from nearby serving

  # ── Admin config resolves from RDS, mirrored to Redis ─────────────────────
  @wip
  Scenario: Admin configuration is stored in RDS and mirrored to Redis
    When an admin updates the venue eligibility configuration
    Then RDS holds the updated eligibility configuration as the system of record
    And the configuration is mirrored to Redis for the existing config readers
    And the running eligibility filter reflects the updated configuration

  # ── Redis is reconstructable from RDS (backfill / disaster recovery) ───────
  Scenario: Rebuilding Redis from RDS restores serving including the geo index
    Given RDS holds venues, enrichment records, and admin config
    And Redis has been flushed
    When the rebuild-Redis-from-RDS job runs
    Then Redis holds the serving projection for every active venue in RDS
    And nearby serving returns those venues from the rebuilt geo index
    And live busyness is restored from RDS (refreshed by the next live cron)

  # ── Decoupling: serving survives an RDS outage ────────────────────────────
  Scenario: When RDS is unavailable, serving continues and pipeline writes fail loudly
    Given a venue "v1" exists in RDS and Redis
    And RDS is unavailable
    When a client requests nearby venues
    Then the venue "v1" is still returned from Redis
    When a pipeline attempts to persist an update for "v1"
    Then the write fails and is logged without corrupting the Redis projection

  # ── Backfill the existing Redis dataset into RDS ──────────────────────────
  Scenario: The one-time backfill imports the existing Redis dataset into RDS
    Given Redis already contains venues and enrichment records from before RDS
    And RDS is empty
    When the one-time Redis-to-RDS backfill runs
    Then RDS holds every venue and enrichment record that Redis contained
    And venue rows are inserted before their enrichment rows
    And serving behavior is unchanged for those venues

  # ── User engagement: write through API, read from Redis ───────────────────
  Scenario: A favorite written through the API lands in RDS pseudonymized and projects to Redis
    Given a venue "v1" exists in RDS and Redis
    When user "user-123" favorites venue "v1" through the engagement API
    Then RDS holds the favorite for venue "v1"
    And RDS stores the user only as a pseudonymized id, never the raw "user-123"
    And Redis holds the favorite so vibes_bot can read it
    When user "user-123" un-favorites venue "v1" through the engagement API
    Then RDS no longer holds an active favorite for "user-123" on "v1"

  Scenario: A hot_like is recorded as a durable append-only event for metrics
    Given a venue "v1" exists in RDS and Redis
    When user "user-123" hot-likes venue "v1" through the engagement API
    Then RDS holds a hot_like event for venue "v1" with a pseudonymized user id
    And the Redis trending hot_like counter for "v1" still reflects the like

  # ── Enrichment labels are durable and never hard-deleted ──────────────────
  Scenario: Deleting an enrichment label soft-deletes it in RDS and keeps history
    Given a venue "v1" has google places vibe labels persisted in RDS
    When the label record for "v1" is deleted
    Then RDS still holds the label record marked soft-deleted with a timestamp
    And the prior label values are recoverable from the enrichment history
    And re-enriching "v1" appends a new history entry without losing the old one
