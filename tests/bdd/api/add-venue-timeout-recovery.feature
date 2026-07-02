@wip
Feature: Add-venue 60s timeout with self-recovery of timed-out creates
  As the venue platform
  I must wait long enough for BestTime's slow create endpoint,
  recover creates that time out but completed on BestTime's side,
  and return honest error details,
  so a paid venue create is never lost and operators always know what happened.

  Scenario: A timed-out create that exists in the account inventory is recovered
    Given a venue create that exceeds the BestTime timeout
    And the venue appears in the BestTime account inventory
    When an operator adds the venue by name and address
    Then the add returns created with a recovered-from-timeout marker
    And the venue is persisted and counted against the monthly ledger
    And the venue is enriched from Google inline
    And no second create call is made to BestTime

  Scenario: A timed-out create absent from the inventory returns an honest error
    Given a venue create that exceeds the BestTime timeout
    And the venue does not appear in the BestTime account inventory
    When an operator adds the venue
    Then the add fails telling the operator the create timed out unconfirmed
    And the reserved quota slot is released
    And the operator is told a later retry maps to the same venue id

  Scenario: A failing reconcile read degrades to the timeout error
    Given a venue create that exceeds the BestTime timeout
    And the account inventory read also fails
    When an operator adds the venue
    Then the add fails with the timeout error
    And no second create call is made to BestTime

  Scenario: Error responses carry BestTime's own message when present
    Given BestTime rejects a venue create with an explanatory message
    When an operator adds the venue
    Then the error response includes BestTime's message alongside the detail

  Scenario: The create call waits up to the configured sixty seconds
    Given the add-venue timeout is configured at sixty seconds
    When an operator adds a venue and BestTime responds within that window
    Then the add succeeds instead of timing out early
