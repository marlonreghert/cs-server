Feature: RDS schema normalization — Ex1 drop venue payload duplication
  As the VibeSense platform
  The first step of the RDS schema-normalization umbrella removes scalar
  duplication on `venues.venue`: relational columns become the source of truth and
  only a slim residual JSON holds the genuinely-nested fields. A venue must
  reconstruct identically (columns + residual) and project to Redis identically.
  The whole risk is silent data drift, so a full-dataset equivalence harness
  guards every transformation in RDS and in the Redis serving projection.

  # Umbrella plan: plans/260605_rds-schema-normalization.md. Sequenced steps
  # Ex1 -> Ex3 -> Ex2; THIS branch/PR implements Ex1 + the shared equivalence
  # harness only. The Ex3 (address table) and Ex2 (admin config) scenarios land in
  # their own later PRs, so main never carries a scenario without its code.
  #
  # bdd-exempt: the pre-execution local-dump rollback gate and the migration
  # mechanics (pg_dump/SSM/restore, expand/contract DDL ordering) are operator
  # runbook + infrastructure, not application behavior — the app cannot verify a
  # dump on the operator's device. They live in the plan's pre-execution gate and
  # acceptance criteria. Same posture as admin_config_rds.feature
  # ("Provisioning/migration is bdd-exempt: infrastructure"). The scenarios below
  # cover only the code-backed data-equivalence invariants.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  # ── Ex1: drop venue payload duplication ────────────────────────────────────
  Scenario: A venue reconstructs identically from columns plus residual JSON
    Given a venue "v4" with full scalar fields, dwell times, and a foot-traffic forecast
    When venue "v4" is stored with scalars in columns and only nested fields in the residual JSON
    Then reconstructing venue "v4" from the repository equals the venue rebuilt from the old full payload
    And the projector projects venue "v4" to Redis identically to before the change

  Scenario: No scalar field is duplicated in the residual JSON
    Given a venue "v5" stored under the normalized venue schema
    Then the residual JSON for venue "v5" contains only nested fields
    And it contains none of the scalar fields that exist as columns

  # ── Equivalence harness (RDS golden diff + Redis shadow projection) ─────────
  Scenario: The RDS golden diff reports a non-passing result when a row does not match
    Given a venue whose v2 reconstruction differs from its retained v1 reconstruction
    When the step's RDS golden diff runs over all venues
    Then the golden diff returns a non-passing result
    And it reports the mismatching venue id and field with no payload secrets

  Scenario: The RDS golden diff passes when every row reconstructs identically
    Given venues whose v2 reconstruction equals their retained v1 reconstruction
    When the step's RDS golden diff runs over all venues
    Then the golden diff returns a passing result with zero mismatches

  Scenario: The Redis shadow projection equals the pre-change serving snapshot
    Given a pre-change snapshot of the Redis serving state and geo index
    When the projector re-projects the v2 shape into a separate shadow keyspace
    Then the shadow serving values and geo membership and coordinates equal the snapshot
    And live busyness is exempt from the comparison
