Feature: Parse the real BestTime create-venue response
  As the venue platform
  I must correctly parse BestTime's real POST /forecasts success response
  and persist the venue it created,
  so a paid, successful venue create is never dropped by my own parser
  and operators see honest errors instead of a fake BestTime outage.

  Scenario: A real-shape success response persists the venue
    Given BestTime accepts a new venue and replies with its real success payload
    And each analysis entry nests the day number inside its day info block
    When an operator adds the venue by name and address
    Then the add returns created
    And the venue is persisted and counted against the monthly ledger
    And the venue is enriched from Google inline

  Scenario: Parsed analysis days are cached and bad entries are dropped
    Given a BestTime success payload whose analysis mixes parseable and malformed day entries
    When an operator adds the venue
    Then the parseable days are cached as weekly forecast days
    And the malformed entries are dropped with a warning
    And the add still returns created

  Scenario: A valid envelope with unparseable analysis still persists the venue
    Given a BestTime success payload whose analysis cannot be parsed at all
    When an operator adds the venue
    Then the add returns created
    And the venue is persisted without cached weekly forecast days

  Scenario: An unparseable envelope is reported as a bad response, not an outage
    Given BestTime replies with a body that has no usable status or venue info
    When an operator adds the venue
    Then the add fails with a bad-response error that names an unparseable response
    And the error is not reported as BestTime being unavailable
    And the reserved quota slot is released

  Scenario: A genuine transport error still reports BestTime as unavailable
    Given BestTime cannot be reached at all
    When an operator adds the venue
    Then the add fails reporting BestTime is unavailable
    And the reserved quota slot is released
