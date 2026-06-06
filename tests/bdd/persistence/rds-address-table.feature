Feature: RDS schema normalization — Ex3 structured address table
  As the VibeSense platform
  The second umbrella step extracts a venue's address (`venue_address` + lat/lng)
  into a referenced `venues.address` table with structured components
  (street/neighborhood/city/postal_code), so address becomes queryable and
  de-duplicated truth. This is the EXPAND phase: the address table is dual-written
  and reads are sourced from it, while the original `venues.venue` address columns
  stay as a rollback baseline (dropped only by the later batched contract). A venue
  must reconstruct identically and project to Redis at the same coordinates.

  # Umbrella plan: plans/260605_rds-schema-normalization.md (Step Ex3). The Venue
  # model and API are unchanged — this only moves where the RDS layer stores and
  # reads address. Equivalence is guarded by the shared golden diff + Redis
  # shadow-projection harness.
  #
  # bdd-exempt: migration mechanics (DDL ordering, SSM, backfill) and the operator
  # backup gate are infrastructure, covered by the plan + acceptance criteria, not
  # Gherkin. Same posture as Ex1.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  Scenario: A venue's address and coordinates reconstruct from the address table
    Given a venue "a1" with address "Rua Aurora, 100" at latitude -8.06 and longitude -34.87
    When venue "a1" is stored under the address-table schema
    Then a venues.address row exists for "a1" with the raw text and coordinates
    And reconstructing venue "a1" yields address "Rua Aurora, 100" at latitude -8.06 and longitude -34.87

  Scenario: The venue projects to Redis at the address-table coordinates
    Given a venue "a2" with address "Av Boa Viagem, 200" at latitude -8.12 and longitude -34.90
    When venue "a2" is stored under the address-table schema
    And the projector rebuilds Redis from RDS
    Then venue "a2" is served from Redis at latitude -8.12 and longitude -34.90

  Scenario: Structured components are absent until enrichment provides them
    Given a venue "a3" stored under the address-table schema from free text only
    Then the venues.address row for "a3" has null street, neighborhood, city, and postal code
    And reconstructing venue "a3" produces the same serving output as before the migration

  Scenario: Reconstruction stays equivalent to the retained payload baseline
    Given several venues stored under the address-table schema
    When the step's RDS golden diff runs over all venues
    Then the golden diff returns a passing result with zero mismatches

  Scenario: The Redis shadow projection equals the pre-change serving snapshot
    Given a pre-change snapshot of the Redis serving state and geo index
    When the projector re-projects the v2 shape into a separate shadow keyspace
    Then the shadow serving values and geo membership and coordinates equal the snapshot
