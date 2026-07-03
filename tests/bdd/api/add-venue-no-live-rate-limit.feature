Feature: Add venue spends no live-busyness credits and survives BestTime rate limits
  Adding a venue must not retrieve live busyness — live data is fetched only by
  the live pipeline once the venue is prioritized through the serving view,
  because live retrieval is what spends BestTime credits. A transient BestTime
  429 rate-limit answer on the create call must be retried instead of failing
  the add or being misread as a venue rejection.

  Scenario: A successful add never calls the live-busyness endpoint
    Given BestTime accepts a new venue and replies with its real success payload
    When an operator adds the venue
    Then the add returns created
    And no request was made to the live-busyness endpoint

  Scenario: A transient BestTime 429 on create is retried and the add succeeds
    Given BestTime rate-limits the create once and then accepts it
    When an operator adds the venue
    Then the add returns created
    And the create endpoint was called twice
    And no request was made to the live-busyness endpoint
