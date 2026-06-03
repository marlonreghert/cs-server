Feature: Redis projection decoupled from the pipelines
  As the VibeSense platform
  Pipelines and admin writes must persist only to RDS (the system of record),
  and Redis must be fed for that data exclusively by a projector that reads RDS.
  End users keep reading Redis; pipelines read their inputs from RDS. Engagement
  (favorites/hot_likes) is the one deliberate exception: it is user-action
  latency-critical, so it writes RDS first then projects Redis IMMEDIATELY in the
  same request — never via the slow projector.

  # NOTE: This supersedes the synchronous write-through projection from
  # rds_system_of_record_01_06_26.md. It is gated on that plan's cutover being
  # complete (rds_enabled=true + backfill done). Infrastructure (scheduler cadence
  # wiring, deploy, the off-loop executor) is bdd-exempt: infrastructure.
  #
  # TWO-PASS execution. PASS 1 (this run) = the scheduled projector running
  # ALONGSIDE the existing write-through, with the two correctness fixes B1
  # (remove deprecated) and B2 (photo remaining-TTL / skip aged), plus the
  # engagement carve-out guards. PASS 2 scenarios are tagged @wip below: the DAO
  # split, pipelines reading/writing only RDS (and the refetch/skip-done gating
  # moving to RDS), and admin venue edits. See the plan's REFRAME block.

  Background:
    Given the RDS system-of-record is enabled
    And the Redis projector is wired
    And an empty RDS and an empty Redis

  # ── PASS 2 (@wip): pipelines write only RDS; Redis untouched until projection ──
  @wip
  Scenario: A pipeline venue upsert writes RDS only and does not write Redis directly
    When a pipeline upserts a venue "v1" named "Bar do Zé"
    Then RDS holds venue "v1" as the system of record
    And Redis has no serving projection for venue "v1" yet
    And the venue "v1" is not yet returned by nearby serving

  @wip
  Scenario: The projector reflects RDS into the Redis serving projection
    Given a pipeline has upserted venue "v1" into RDS
    When the Redis projector runs
    Then Redis holds the serving projection for venue "v1" including the geo index
    And the venue "v1" is returned by nearby serving

  @wip
  Scenario: Enrichment outputs persist to RDS and project to Redis on the next projector run
    Given a venue "v1" exists in RDS and is projected to Redis
    When the pipelines persist google places, instagram, photos, reviews, opening hours, menu, vibe profile, weekly forecast, and live busyness for "v1" into RDS
    Then RDS holds each of those records for "v1"
    And after the projector runs, the Redis serving projection for "v1" includes every field the nearby response reads

  # ── PASS 2 (@wip): pipelines read their inputs from RDS, not from a stale Redis ─
  @wip
  Scenario: A later pipeline stage reads a prior stage's output from RDS within the same cycle
    Given the photo pipeline has written photos for "v1" to RDS only
    And the projector has not yet run
    When the vibe classifier reads the photos for "v1"
    Then it reads the photos from RDS, not from the unprojected Redis cache
    And the classifier can proceed without waiting for projection

  # ── PASS 2 (@wip): refetch / skip-done gating moves to RDS (the REFRAME) ──────
  @wip
  Scenario: The Google photos refetch trigger reads RDS staleness after decoupling
    Given a venue "v1" whose photos in RDS have aged past their TTL
    When the photo enrichment job lists which venues need photos
    Then it sees "v1" as needing a refetch using the RDS updated_at, not Redis

  @wip
  Scenario: Skip-already-done gating derives from RDS, not a pipeline Redis write
    Given the pipeline has persisted "v1" enrichment to RDS
    When an enrichment pipeline lists which venues still need processing
    Then it derives the gating set from RDS presence
    And the pipeline does not write a gating set to Redis itself

  # ── PASS 1 (B2): the projector counts the photo TTL down; aged photos drop ────
  Scenario: Repeated projector runs project photos with the remaining TTL, not a fresh full TTL
    Given a venue "v1" whose photos were written to RDS some time ago
    When the Redis projector runs
    Then the projected photo key carries the remaining TTL, not a fresh full TTL
    When the venue photos age past their TTL in RDS
    And the Redis projector runs
    Then the projector projects the aged photos as absent from serving

  # ── PASS 1 (engagement carve-out): DB-first but projected IMMEDIATELY ─────────
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

  # ── PASS 2 (@wip): admin venue edits go to RDS, surface via the projector ─────
  @wip
  Scenario: An admin venue edit writes RDS only and appears in serving after projection
    Given a venue "v1" exists in RDS and is projected to Redis
    When an admin edits venue "v1" name to "Bar Editado" through the admin API
    Then RDS holds the edited name for "v1" as the system of record
    And Redis still shows the old name until the projector runs
    And after the projector runs, nearby serving returns the edited name

  # ── PASS 2 (@wip): decoupling preserved — serving survives an RDS outage ──────
  @wip
  Scenario: When RDS is unavailable, serving continues and pipeline writes fail loudly
    Given a venue "v1" exists in RDS and is projected to Redis
    And RDS is unavailable
    When a client requests nearby venues
    Then the venue "v1" is still returned from Redis
    When a pipeline attempts to persist an update for "v1"
    Then the write fails and is logged without corrupting the Redis projection
    And the projector run is a safe no-op while RDS is unavailable

  # ── PASS 1 (B1): a venue deprecated in RDS is removed from serving ────────────
  Scenario: The projector removes venues deprecated in RDS from the Redis serving set
    Given a venue "v1" is active in RDS and projected to Redis
    When the eligibility sweep deprecates "v1" in RDS only
    And the Redis projector runs
    Then the projector removes "v1" from the Redis serving set and geo index
    And the venue "v1" is no longer returned by nearby serving

  # ── PASS 1 (B1): idempotent + additive, deprecation is a removal signal ───────
  Scenario: Re-running the projector is idempotent and prunes only on a positive deprecate signal
    Given a venue "v1" is active in RDS and projected to Redis
    And a venue "orphan" is present in Redis with no RDS row at all
    And a venue "vdep" is deprecated in RDS after being projected to Redis
    When the Redis projector runs twice
    Then the active venue "v1" is still returned by nearby serving after the second run
    And the venue "orphan" with no RDS row is left untouched in Redis
    But the venue "vdep" deprecated in RDS is removed from Redis
