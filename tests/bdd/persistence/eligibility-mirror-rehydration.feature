@persistence
Feature: Eligibility mirror rehydration from rows on startup
  As the VibeSense platform
  Eligibility rules live in admin.eligibility_rule (the durable truth) and serving
  reads a Redis admin_config:venue_eligibility mirror. The mirror is written only
  on admin edits, so a Redis flush silently reverts eligibility filtering to the
  hardcoded defaults until the next edit. cs-server must rebuild the mirror from
  the rows on startup and on the periodic projector cycle so a flush no longer
  degrades filtering, and the rows must be the sole durable truth (no redundant
  RDS blob copy).

  # Plan: plans/260608_eligibility-mirror-rehydration.md.
  #
  # The name keyword "casino" is deliberately NOT in the hardcoded defaults, so a
  # config that blocks it can only have come from the rows — distinguishing a
  # rebuilt-from-rows mirror from the defaults a flush would otherwise leave.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  Scenario: A flushed eligibility mirror is rebuilt from the rows on startup
    Given an eligibility rule blocks the name keyword "casino"
    And the Redis eligibility mirror is then cleared
    When cs-server rehydrates the eligibility mirror on startup
    Then a venue named "Casino Royale" is excluded by the eligibility filter
    And the live eligibility config came from the rows, not the hardcoded defaults

  Scenario: Startup rehydration writes a mirror equal to the rows' effective config
    Given an eligibility rule blocks the name keyword "casino"
    And the Redis eligibility mirror is then cleared
    When cs-server rehydrates the eligibility mirror on startup
    Then the Redis eligibility mirror exists
    And its effective config equals the effective config of the rows

  Scenario: A runtime flush is healed by the periodic projector without a restart
    Given an eligibility rule blocks the name keyword "casino"
    And the Redis eligibility mirror is then cleared
    When the periodic projector runs a rebuild cycle
    Then a venue named "Casino Royale" is excluded by the eligibility filter
    And the live eligibility config came from the rows, not the hardcoded defaults

  Scenario: With no eligibility rules, rehydration leaves the defaults in force
    Given there are no eligibility rules
    When cs-server rehydrates the eligibility mirror on startup
    Then no eligibility override mirror is present
    And a venue named "Casino Royale" is allowed by the eligibility filter

  Scenario: Rehydration degrades safely when RDS is unavailable at startup
    Given an eligibility rule blocks the name keyword "casino"
    And RDS is unavailable
    When cs-server rehydrates the eligibility mirror on startup
    Then startup completes without raising
    And a rehydration failure is recorded in the metrics

  Scenario: Eligibility writes keep the rows as the sole durable truth
    When an eligibility rule blocking the name keyword "casino" is added
    Then no RDS admin_config row for "venue_eligibility" is persisted
    And the live eligibility config blocks the name keyword "casino"
