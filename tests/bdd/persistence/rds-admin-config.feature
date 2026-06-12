@persistence
Feature: RDS schema normalization — Ex2 admin eligibility config normalization
  As the VibeSense platform
  The third umbrella step makes admin eligibility config editable as data instead
  of a monolithic JSON blob: the four block-lists become rows in
  admin.eligibility_rule (one row per rule), so adding or removing a single rule is
  a one-row change. The rows are the source of truth; the Redis
  admin_config:venue_eligibility mirror is demoted to a derived projection,
  reassembled on every write into an equivalent JSON (same effective config; the
  block-lists are membership sets, so element order may normalize), so serving and
  vibes_bot keep reading it unchanged.

  # Umbrella plan: plans/260605_rds-schema-normalization.md (Step Ex2). Expand
  # phase: rows + reassembly; the admin_config blob is retained as a rollback
  # baseline (dropped by the batched contract). vibes_bot stays on the mirror;
  # its migration off it is a separate cross-repo step.
  #
  # bdd-exempt: migration DDL + the two admin observability views
  # (v_blocked_google_type_effect, v_rejection_reason_effect) are SQL-only
  # infrastructure verified by the post-provisioning smoke test, not the in-memory
  # fake. Same posture as Ex1/Ex3.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  Scenario: Adding a single eligibility rule is a one-row change that takes effect
    Given the eligibility rules are stored as normalized rows
    And a venue named "Qzqz Bar" with no Google type that is currently eligible
    When an operator adds the blocked name keyword "qzqz" as a single rule row
    Then the venue named "Qzqz Bar" becomes ineligible by name keyword
    And exactly one eligibility rule row was added and no other rule changed

  Scenario: Removing a single eligibility rule restores eligibility
    Given the blocked name keyword "qzqz" is stored as a single rule row
    And a venue named "Qzqz Bar" with no Google type that is currently ineligible
    When an operator removes the blocked name keyword "qzqz"
    Then the venue named "Qzqz Bar" becomes eligible again

  Scenario: The effective config assembled from rows equals the previous JSON blob
    Given an existing "venue_eligibility" JSON override with a blocked Google type "casino"
    When that configuration is decomposed into normalized rule rows
    Then the effective eligibility config assembled from the rows equals the config the JSON blob produced

  Scenario: Empty eligibility rules fall back to the hardcoded defaults
    Given the normalized eligibility rule table is empty
    When the effective eligibility config is assembled
    Then the evaluation uses the hardcoded default block-lists
    And eligibility filtering does not break

  Scenario: The Redis mirror is reassembled with the edit after a single-rule change
    When an operator adds the blocked Google type "casino" as a single rule row
    Then the Redis "admin_config:venue_eligibility" mirror is written as the reassembled JSON
    And load_eligibility_config reading that mirror blocks the Google type "casino"

  Scenario: The admin read reflects rules stored directly as rows
    Given the blocked Google type "casino" is stored as a single rule row
    When the admin eligibility config is read from the rows
    Then the blocked Google types include "casino"
