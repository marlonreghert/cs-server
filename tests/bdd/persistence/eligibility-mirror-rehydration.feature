Feature: Eligibility mirror rehydration from rows on startup
  As the VibeSense platform
  Eligibility rules live in admin.eligibility_rule (the durable truth) and serving
  reads a Redis admin_config:venue_eligibility mirror. The mirror is written only
  on admin edits, so a Redis flush silently reverts eligibility filtering to the
  hardcoded defaults until the next edit. cs-server must rebuild the mirror from
  the rows on startup so a flush no longer degrades filtering, and the rows must be
  the sole durable truth (no redundant RDS blob copy).

  # Plan: plans/260608_eligibility-mirror-rehydration.md.
  #
  # bdd-exempt: nothing here — the startup rehydration and the no-RDS-blob
  # invariant are externally observable (filtering behaviour + persisted state),
  # so they are covered by the scenarios below.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS and an empty Redis

  Scenario: A flushed eligibility mirror is rebuilt from the rows on startup
    Given an eligibility rule blocks venue type "drugstore"
    And the Redis eligibility mirror is then cleared
    When cs-server rehydrates the eligibility mirror on startup
    Then a venue of type "drugstore" is excluded by the eligibility filter
    And the rehydrated mirror reflects the configured rules, not the defaults

  Scenario: Startup rehydration writes a mirror equal to the rows' effective config
    Given eligibility rules block venue type "drugstore" and name keyword "pharmacy"
    And the Redis eligibility mirror is then cleared
    When cs-server rehydrates the eligibility mirror on startup
    Then the Redis eligibility mirror exists
    And its effective config equals the effective config of the rows

  Scenario: A runtime flush is healed by the periodic projector without a restart
    Given an eligibility rule blocks venue type "drugstore"
    And the Redis eligibility mirror is then cleared
    When the periodic projector runs a rebuild cycle
    Then a venue of type "drugstore" is excluded by the eligibility filter
    And the rehydrated mirror reflects the configured rules, not the defaults

  Scenario: With no eligibility rules, rehydration leaves no override and defaults apply
    Given there are no eligibility rules
    When cs-server rehydrates the eligibility mirror on startup
    Then no eligibility override mirror is present
    And the eligibility filter uses the hardcoded defaults

  Scenario: Rehydration degrades safely when RDS is unavailable at startup
    Given an eligibility rule blocks venue type "drugstore"
    And the RDS system-of-record is unavailable
    When cs-server rehydrates the eligibility mirror on startup
    Then startup completes without raising
    And a rehydration failure is logged and counted

  Scenario: Eligibility writes keep the rows as the sole durable truth
    When an eligibility rule blocks venue type "drugstore" is added
    Then no RDS admin_config row for "venue_eligibility" is persisted
    And the Redis eligibility mirror reflects the configured rules
