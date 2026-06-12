@persistence
Feature: RDS schema normalization — venue storage contract (columns + residual + address)
  As the VibeSense platform
  The RDS schema-normalization umbrella made the relational columns the source of
  truth: scalar fields live in their own `venues.venue` columns, a slim residual
  JSON (`extra`) holds only the genuinely-nested fields, and address lives in
  `venues.address`. The batched contract dropped the duplicated `payload` JSONB
  and the old `venues.venue` address columns. A venue must reconstruct from
  columns + residual + the address table and project to Redis identically, with
  no `payload` baseline anywhere.

  # Umbrella plan: plans/260605_rds-schema-normalization.md.
  #
  # bdd-exempt: the migration mechanics (fresh pg_dump, relax-NOT-NULL before
  # deploy, drop-columns after verify, SSM/restore) are operator runbook +
  # infrastructure, not application behavior — the app cannot verify a dump or a
  # DDL ordering on the operator's device. They live in the plan's runbook. The
  # scenarios below cover only the code-backed data-equivalence invariants.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  Scenario: A venue reconstructs from columns plus residual JSON without any payload
    Given a venue "v4" with full scalar fields, dwell times, and a foot-traffic forecast
    When venue "v4" is stored with scalars in columns and only nested fields in the residual JSON
    Then reconstructing venue "v4" from the repository equals the original venue
    And the stored venue row carries no payload baseline

  Scenario: No scalar field is duplicated in the residual JSON
    Given a venue "v5" stored under the normalized venue schema
    Then the residual JSON for venue "v5" contains only nested fields
    And it contains none of the scalar fields that exist as columns

  Scenario: The projector projects active venues to Redis matching the RDS reconstruction
    Given venues stored under the normalized venue schema
    When the projector rebuilds Redis from RDS
    Then the redis-to-rds serving diff passes with zero mismatches
