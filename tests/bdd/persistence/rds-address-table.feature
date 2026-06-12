@persistence
Feature: RDS schema normalization — Ex3 structured address table
  As the VibeSense platform
  A venue's address (`venue_address` + lat/lng) lives in a referenced
  `venues.address` table with structured components
  (street/neighborhood/city/postal_code), so address is queryable and
  de-duplicated truth. The batched contract dropped the original `venues.venue`
  address columns, so the table is the sole address source and feeds the geo
  rebuild. A venue must reconstruct its address + coordinates from the table and
  project to Redis at those coordinates.

  # Umbrella plan: plans/260605_rds-schema-normalization.md (Step Ex3). The Venue
  # model and API are unchanged — this only moves where the RDS layer stores and
  # reads address.
  #
  # bdd-exempt: migration mechanics (DDL ordering, SSM, backfill) and the operator
  # backup gate are infrastructure, covered by the plan + runbook, not Gherkin.

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
