Feature: RDS schema normalization preserves serving behavior
  As the VibeSense platform
  Three persistence design-smell fixes on the live RDS system-of-record — a
  structured `venues.address` table (Ex3), normalized admin configuration with
  one-row eligibility rules (Ex2), and removal of scalar duplication on
  `venues.venue` in favour of relational columns plus a slim residual JSON (Ex1) —
  must each preserve externally observable serving behavior exactly. The whole
  risk is silent data drift, so every scenario asserts equivalence before/after.

  # Umbrella plan: plans/260605_rds-schema-normalization.md. One branch, three
  # sequenced steps (Ex1 -> Ex3 -> Ex2). Ex1 leads so venue reconstruction is
  # column-based before Ex3 swaps the address source (no payload overlay). Each
  # step is an expand -> backfill -> verify -> cutover -> contract migration on a
  # populated database, gated by a full-dataset equivalence harness that diffs the
  # old (v1) and new (v2) shapes in RDS and in a Redis shadow projection.
  # vibes_bot's migration off the Redis eligibility mirror is a separate
  # coordinated change; the mirror is retained here until then.
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

  # ── Ex3: structured address table ──────────────────────────────────────────
  Scenario: A venue's address and coordinates reconstruct identically from the address table
    Given a venue "v1" with address "Rua X, 100" at latitude -8.05 and longitude -34.88
    When the venue's address is migrated into the structured address table
    Then reconstructing venue "v1" yields address "Rua X, 100" at latitude -8.05 and longitude -34.88
    And venue "v1" remains in the Redis geo index at latitude -8.05 and longitude -34.88

  Scenario: Structured address components stay absent until enrichment provides them
    Given a venue "v2" backfilled into the address table from free text only
    Then reconstructing venue "v2" produces the same serving output as before the migration
    And the structured components street, neighborhood, city, and postal code are absent

  # ── Ex2: normalized admin configuration ────────────────────────────────────
  Scenario: Adding a single eligibility rule is a one-row change that takes effect
    Given the eligibility rules are stored as normalized rows
    And a venue "v3" named "Bar do Centro" that is eligible
    When an operator adds the blocked name keyword "centro" as a single rule row
    Then venue "v3" becomes ineligible by name keyword
    And no other eligibility rule is modified

  Scenario: The effective config assembled from rows equals the previous JSON blob
    Given an existing "venue_eligibility" JSON configuration
    When that configuration is backfilled into normalized eligibility rule rows
    Then the effective eligibility config assembled from the rows equals the config the JSON blob produced

  Scenario: Empty eligibility rules fall back to the hardcoded defaults
    Given the normalized eligibility rule table is empty
    When a venue is evaluated for eligibility
    Then the evaluation uses the hardcoded default block-lists
    And eligibility filtering does not break

  Scenario: The Redis eligibility mirror is still written for vibes_bot compatibility
    When an operator changes the eligibility configuration through the admin API
    Then the Redis "admin_config:venue_eligibility" mirror is written in the same JSON shape as before
    And cs-server runtime readers read eligibility from the normalized rows

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
