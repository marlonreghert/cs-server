@wip
Feature: Redis projection decoupled from the pipelines
  As the VibeSense platform
  Pipelines and admin writes must persist only to RDS (the system of record),
  and Redis must be fed for that data exclusively by a projector that reads RDS.
  End users keep reading Redis; pipelines read their inputs from RDS. The only
  Redis state a pipeline still touches is cache-freshness bookkeeping (TTL
  "is-this-fresh / already-done" gating), a cache concern, not a system of record.
  Engagement (favorites/hot_likes) is the one deliberate exception: it is
  user-action latency-critical, so it writes RDS first then projects Redis
  IMMEDIATELY in the same request — never via the slow projector.

  # NOTE: This supersedes the synchronous write-through projection from
  # rds_system_of_record_01_06_26.md. It is gated on that plan's cutover being
  # complete (rds_enabled=true + backfill done) so Redis is fed from a populated
  # RDS. Infrastructure (scheduler wiring cadence, deploy) is bdd-exempt.

  Background:
    Given the RDS system-of-record is enabled
    And the Redis projector is wired
    And an empty RDS and an empty Redis

  # ── Pipelines write only RDS; Redis is untouched until the projector runs ──
  Scenario: A pipeline venue upsert writes RDS only and does not write Redis directly
    When a pipeline upserts a venue "v1" named "Bar do Zé"
    Then RDS holds venue "v1" as the system of record
    And Redis has no serving projection for venue "v1" yet
    And the venue "v1" is not yet returned by nearby serving

  Scenario: The projector reflects RDS into the Redis serving projection
    Given a pipeline has upserted venue "v1" into RDS
    When the Redis projector runs
    Then Redis holds the serving projection for venue "v1" including the geo index
    And the venue "v1" is returned by nearby serving

  Scenario: Enrichment outputs persist to RDS and project to Redis on the next projector run
    Given a venue "v1" exists in RDS and is projected to Redis
    When the pipelines persist google places, instagram, photos, reviews, opening hours, menu, vibe profile, weekly forecast, and live busyness for "v1" into RDS
    Then RDS holds each of those records for "v1"
    And after the projector runs, the Redis serving projection for "v1" includes every field the nearby response reads

  # ── Pipelines read their inputs from RDS, not from a stale Redis ──────────
  Scenario: A later pipeline stage reads a prior stage's output from RDS within the same cycle
    Given the photo pipeline has written photos for "v1" to RDS only
    And the projector has not yet run
    When the vibe classifier reads the photos for "v1"
    Then it reads the photos from RDS, not from the unprojected Redis cache
    And the classifier can proceed without waiting for projection

  # ── Cache-freshness carve-out: TTL gating stays Redis-only ───────────────
  Scenario: The Google photos refetch trigger still reads Redis only after decoupling
    Given a venue "v1" whose photos are projected to Redis with a TTL
    When the venue photos TTL expires in Redis
    Then the photo refetch trigger sees "v1" as missing photos using Redis only
    And RDS is never consulted to decide whether photos need refetching

  Scenario: Repeated projector runs let the photo TTL count down instead of resetting it
    Given a venue "v1" whose photos were written to RDS some time ago
    When the projector runs repeatedly while the photos age past their TTL
    Then each run projects photos with the remaining TTL based on when they were fetched
    And the photo key eventually expires in Redis instead of being re-stamped fresh
    And the refetch trigger then fires so stale Google URLs are refreshed

  Scenario: Skip-already-done gating uses the Redis cache set, not a pipeline Redis write
    Given the projector has reflected "v1" enrichment into the Redis cache sets
    When an enrichment pipeline lists which venues still need processing
    Then it derives the gating set from Redis cache bookkeeping
    And the pipeline does not write that gating set to Redis itself

  # ── Engagement is the exception: DB-first but projected IMMEDIATELY ──────
  Scenario: A hot-like writes RDS first and appears in Redis in the same request
    Given a venue "v1" exists in RDS and is projected to Redis
    When user "user-123" hot-likes venue "v1" through the engagement API
    Then RDS records the hot-like event first
    And Redis reflects the hot-like immediately in the same request, without a projector run
    And the user sees the hot-like on their next read with no projector-tick delay

  Scenario: A favorite is visible immediately and is not deferred to the slow projector
    Given a venue "v1" exists in RDS and is projected to Redis
    When user "user-123" favorites venue "v1" through the engagement API
    Then RDS holds the favorite as the system of record
    And Redis holds the favorite immediately for the user's next read
    And the engagement write does not depend on the scheduled projector

  # ── Admin writes go to RDS, surface via the projector ────────────────────
  Scenario: An admin venue edit writes RDS only and appears in serving after projection
    Given a venue "v1" exists in RDS and is projected to Redis
    When an admin edits venue "v1" name to "Bar Editado" through the admin API
    Then RDS holds the edited name for "v1" as the system of record
    And Redis still shows the old name until the projector runs
    And after the projector runs, nearby serving returns the edited name

  # ── Decoupling preserved: serving survives an RDS outage ─────────────────
  Scenario: When RDS is unavailable, serving continues and pipeline writes fail loudly
    Given a venue "v1" exists in RDS and is projected to Redis
    And RDS is unavailable
    When a client requests nearby venues
    Then the venue "v1" is still returned from Redis
    When a pipeline attempts to persist an update for "v1"
    Then the write fails and is logged without corrupting the Redis projection
    And the projector run is a safe no-op while RDS is unavailable

  # ── A venue deprecated in RDS must be removed from serving by the projector ─
  Scenario: The projector removes venues deprecated in RDS from the Redis serving set
    Given a venue "v1" is active in RDS and projected to Redis
    When the eligibility sweep deprecates "v1" in RDS
    And the projector runs
    Then the projector removes "v1" from the Redis serving set and geo index
    And the venue "v1" is no longer returned by nearby serving

  # ── Idempotent + additive, but deprecation is a removal signal (not a prune) ─
  Scenario: Re-running the projector is idempotent and prunes only on a positive deprecate signal
    Given RDS holds active venues and enrichment records projected to Redis
    When the Redis projector runs twice
    Then the Redis serving projection is unchanged after the second run
    And a venue present in Redis with no RDS row at all is left untouched
    But a venue deprecated in RDS is removed from Redis
