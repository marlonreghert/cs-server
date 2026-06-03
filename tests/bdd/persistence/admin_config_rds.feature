Feature: Admin configuration is the system of record in RDS, mirrored to Redis
  As the VibeSense platform
  Admin configuration (eligibility, discovery points, budget quota, photo TTL,
  and the vibes_bot admin-panel keys) must be durably owned by RDS
  (`admin.admin_config`) and written through a cs-server admin API that mirrors
  the existing Redis `admin_config:*` keys in the same request, so the durable
  source survives Redis loss while every current runtime reader keeps reading the
  Redis mirror unchanged.

  # This completes Phase 2 of plans/rds_system_of_record_01_06_26.md. Config is a
  # synchronous RDS-write-then-Redis-mirror carve-out (like engagement), NOT the
  # venue projector. Provisioning/migration is bdd-exempt: infrastructure.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  # ── Write-through: RDS is truth, Redis mirror is for existing readers ──────
  Scenario: Updating a config key writes RDS as the system of record and mirrors Redis
    When an admin sets config key "venue_eligibility" through the admin config API
    Then RDS holds "venue_eligibility" as the system of record
    And the Redis "admin_config:venue_eligibility" mirror holds the same JSON value
    And reading config key "venue_eligibility" from the admin API returns the RDS value

  Scenario: A running reader reflects the updated config via the Redis mirror, unchanged
    Given a venue "v1" that is eligible under the default eligibility config
    When an admin updates "venue_eligibility" to block the venue's type through the admin config API
    Then RDS holds the updated eligibility configuration as the system of record
    And the running eligibility filter reflects the updated configuration from the Redis mirror
    And no runtime reader had to change how it reads configuration

  # ── Delete ─────────────────────────────────────────────────────────────────
  Scenario: Deleting a config key removes it from RDS and from the Redis mirror
    Given config key "discovery_points" is stored in RDS and mirrored to Redis
    When an admin deletes config key "discovery_points" through the admin config API
    Then RDS no longer holds "discovery_points"
    And the Redis "admin_config:discovery_points" mirror is removed
    And the reader falls back to its built-in default

  # ── Partial-failure: mirror fails after the durable write ───────────────────
  Scenario: A failed Redis mirror after the RDS commit returns a retryable error
    Given RDS is writable and the Redis mirror write will fail
    When an admin sets config key "venue_eligibility" through the admin config API
    Then RDS holds the durable value as the system of record
    And the admin API returns a non-success status so the caller retries
    And retrying the same write succeeds idempotently and restores the Redis mirror

  # ── Decoupling preserved: config write needs RDS, readers survive an outage ─
  Scenario: When RDS is unavailable a config write fails loudly and readers keep working
    Given config key "venue_eligibility" is stored in RDS and mirrored to Redis
    And RDS is unavailable
    When an admin sets config key "venue_eligibility" through the admin config API
    Then the write fails and is logged without changing the existing Redis mirror
    And the running eligibility filter keeps reading the last mirrored configuration
