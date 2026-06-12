@persistence
Feature: Redis projection decoupled from the pipelines
  As the VibeSense platform
  Pipelines and admin writes must persist only to RDS (the system of record),
  and Redis must be fed for that data exclusively by a projector that reads RDS.
  End users keep reading Redis; pipelines read their inputs from RDS. Engagement
  (favorites/hot_likes) is the one deliberate exception: it is user-action
  latency-critical, so it writes RDS first then projects Redis IMMEDIATELY in the
  same request — never via the slow projector.

  # The scheduled off-loop projector is the sole Redis writer for pipeline data;
  # it also applies B1 (remove RDS-deprecated venues) and B2 (photo remaining-TTL
  # / drop aged). Infrastructure (scheduler cadence wiring, deploy, the off-loop
  # executor) is bdd-exempt: infrastructure.

  Background:
    Given the RDS system-of-record is enabled
    And the Redis projector is wired
    And an empty RDS and an empty Redis

  # ── Pipelines write only RDS; Redis untouched until the projector ─────────────
  Scenario: A pipeline venue upsert writes RDS only and does not write Redis directly
    Given the pipeline is decoupled to RDS-only
    When a pipeline upserts a venue "v1" named "Bar do Zé"
    Then RDS holds venue "v1" as the system of record
    And Redis has no serving projection for venue "v1" yet
    And the venue "v1" is not yet returned by nearby serving

  # ── Pipelines read their inputs from RDS, not from a stale Redis ──────────────
  Scenario: A later pipeline stage reads a prior stage's output from RDS within the same cycle
    Given the photo pipeline has written photos for "v1" to RDS only
    And the projector has not yet run
    When the vibe classifier reads the photos for "v1"
    Then it reads the photos from RDS, not from the unprojected Redis cache
    And the classifier can proceed without waiting for projection

  # ── Refetch / skip-done gating reads RDS, not Redis ───────────────────────────
  Scenario: The Google photos refetch trigger reads RDS freshness, not Redis
    Given the pipeline is decoupled to RDS-only
    And a venue "v1" has fresh photos in RDS but none projected to Redis
    And a venue "v2" has photos in RDS aged past their TTL
    When the photo enrichment job lists which venues have fresh photos
    Then "v1" counts as fresh from RDS even though Redis has no photo key
    And "v2" is excluded because its RDS photos aged past the TTL

  Scenario: Skip-already-done gating derives from RDS presence, not a Redis cache key
    Given the pipeline is decoupled to RDS-only
    And a venue "v1" has a vibe profile in RDS but none projected to Redis
    When an enrichment pipeline lists which venues already have a vibe profile
    Then "v1" counts as done from RDS even though Redis has no vibe-profile key

  Scenario: Instagram re-search gating uses status-aware RDS staleness, not Redis TTL
    Given the pipeline is decoupled to RDS-only
    And a venue "v1" was found on instagram in RDS 10 days ago
    And a venue "v2" was marked not_found on instagram in RDS 10 days ago
    When the instagram enrichment lists which venues have fresh instagram
    Then "v1" counts as fresh because found results live 30 days
    But "v2" is stale because not_found results expire after 7 days

  # ── B2: the projector counts the photo TTL down; aged photos drop ─────────────
  Scenario: Repeated projector runs project photos with the remaining TTL, not a fresh full TTL
    Given a venue "v1" whose photos were written to RDS some time ago
    When the Redis projector runs
    Then the projected photo key carries the remaining TTL, not a fresh full TTL
    When the venue photos age past their TTL in RDS
    And the Redis projector runs
    Then the projector projects the aged photos as absent from serving

  # ── Engagement carve-out: DB-first but projected IMMEDIATELY ──────────────────
  Scenario: A hot-like writes RDS first and appears in Redis in the same request
    Given a venue "v1" exists in RDS and is projected to Redis
    When user "user-123" hot-likes venue "v1" through the engagement API
    Then RDS records the hot-like event first
    And Redis reflects the hot-like immediately in the same request, without a projector run

  Scenario: A favorite is visible immediately and is not deferred to the slow projector
    Given a venue "v1" exists in RDS and is projected to Redis
    When user "user-123" favorites venue "v1" through the engagement API
    Then RDS holds the favorite as the system of record
    And Redis holds the favorite immediately for the user's next read without a projector run

  # ── B1: a venue deprecated in RDS is removed from serving ─────────────────────
  Scenario: The projector removes venues deprecated in RDS from the Redis serving set
    Given a venue "v1" is active in RDS and projected to Redis
    When the eligibility sweep deprecates "v1" in RDS only
    And the Redis projector runs
    Then the projector removes "v1" from the Redis serving set and geo index
    And the venue "v1" is no longer returned by nearby serving

  # ── B1: idempotent + additive, deprecation is a removal signal ────────────────
  Scenario: Re-running the projector is idempotent and prunes only on a positive deprecate signal
    Given a venue "v1" is active in RDS and projected to Redis
    And a venue "orphan" is present in Redis with no RDS row at all
    And a venue "vdep" is deprecated in RDS after being projected to Redis
    When the Redis projector runs twice
    Then the active venue "v1" is still returned by nearby serving after the second run
    And the venue "orphan" with no RDS row is left untouched in Redis
    But the venue "vdep" deprecated in RDS is removed from Redis
