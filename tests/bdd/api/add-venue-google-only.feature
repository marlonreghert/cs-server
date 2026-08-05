Feature: Catalog a venue from Google metadata when BestTime cannot forecast it
  As the venue platform
  I must persist a venue that Google knows about but BestTime cannot forecast,
  under a venue id I mint myself and populated from Google metadata alone,
  so operators can catalog it and the app can show it as having no busyness data
  instead of the venue being lost to a terminal rejection.

  Background:
    Given BestTime rejects the create as unforecastable with a venue info block that has no venue id
    And no nearby venue matches in the geo fallback

  # ── the new path ────────────────────────────────────────────────────────────

  Scenario: An unforecastable venue is cataloged from Google metadata
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    When an operator adds the venue by name and address
    Then the add succeeds as created from Google metadata
    And the persisted venue carries a minted venue id that is not a BestTime id
    And the persisted venue is recorded as having no forecast
    And the persisted venue is recorded as sourced from Google

  Scenario: The minted venue is populated from Google, not from BestTime
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    When an operator adds the venue by name and address
    Then the persisted venue carries the name, address and coordinates Google returned
    And the persisted venue carries the rating and review count from BestTime's rejection block
    And the persisted venue carries no BestTime venue type

  Scenario: The minted venue reaches serving
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    When an operator adds the venue by name and address
    Then the minted venue is eligible for serving despite having no BestTime venue type
    And the minted venue is returned by the public nearby venues endpoint

  # ── the cost guarantees ─────────────────────────────────────────────────────

  Scenario: Cataloging from Google spends no BestTime credit
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    When an operator adds the venue by name and address
    Then the BestTime create endpoint is not called a second time
    And no paid BestTime endpoint is called
    And the monthly BestTime new-venue counter does not increase
    And the venue is not recorded as touched in the BestTime ledger

  Scenario: A minted venue is never selected for BestTime live refresh
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    And an operator has added the venue by name and address
    When the bounded live forecast refresh selects venues
    Then the minted venue is not selected
    And no BestTime live forecast is requested for the minted venue

  Scenario: A minted venue is never selected for BestTime weekly refresh
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    And an operator has added the venue by name and address
    And the minted venue is the only active venue
    When the bounded weekly forecast refresh selects venues
    Then no venues are selected

  Scenario: BestTime-sourced venues are still selected for refresh
    Given an active venue that was created from BestTime
    When the bounded live forecast refresh selects venues
    Then that venue is selected

  # ── the kill switch ─────────────────────────────────────────────────────────

  Scenario: With the path disabled the rejection stays terminal
    Given the Google-only add path is disabled
    When an operator adds the venue by name and address
    Then the add fails as a rejection with the geo-fallback message
    And no venue is persisted

  Scenario: An unreadable admin config disables the path rather than enabling it
    Given the admin configuration cannot be read
    When an operator adds the venue by name and address
    Then the add fails as a rejection with the geo-fallback message
    And no venue is persisted

  # ── quality gate and failure handling ───────────────────────────────────────

  Scenario: A venue Google cannot resolve is not cataloged
    Given the Google-only add path is enabled
    And Google Places cannot resolve the submitted venue
    When an operator adds the venue by name and address
    Then the add fails as a rejection with the geo-fallback message
    And no venue is persisted

  Scenario: A Google details failure leaves nothing behind
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue but the details request fails
    When an operator adds the venue by name and address
    Then the add fails as a rejection with the geo-fallback message
    And no venue is persisted
    And no partial venue row remains

  Scenario: Re-adding the same venue returns the venue already minted
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    And an operator has added the venue by name and address
    When an operator adds the same venue by name and address again
    Then the add reports that the venue already exists
    And only one venue is persisted for that name and address

  # ── the new path must never preempt the existing ones ───────────────────────

  Scenario: A geo-fallback match still wins over minting a venue
    Given the Google-only add path is enabled
    And a nearby venue matches in the geo fallback
    When an operator adds the venue by name and address
    Then the add completes as matched via geo fallback
    And no venue id is minted

  Scenario: A geo-fallback outage is terminal and does not mint a venue
    Given the Google-only add path is enabled
    And the geo fallback request to BestTime fails
    When an operator adds the venue by name and address
    Then the add fails with a geo-fallback unavailable error
    And no venue is persisted

  Scenario: A monthly-cap rejection never reaches the Google-only path
    Given the Google-only add path is enabled
    And BestTime rejects the create because its monthly venue cap is reached
    When an operator adds the venue by name and address
    Then the add fails with the BestTime monthly cap status
    And no venue is persisted

  Scenario: A successful BestTime create is unaffected
    Given the Google-only add path is enabled
    And BestTime accepts the create and returns a forecast
    When an operator adds the venue by name and address
    Then the add succeeds as a normal creation
    And the persisted venue is recorded as sourced from BestTime

  # ── batch triage ────────────────────────────────────────────────────────────

  Scenario: A batch row cataloged from Google counts as a success
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    When a batch add job processes a row for the venue
    Then the row is counted as a success in the job summary
    And the job is not stopped

  # ── observability ───────────────────────────────────────────────────────────

  Scenario Outline: Each outcome of the Google-only path is separately countable
    Given the Google-only add path is <path state>
    And Google Places <google outcome> the submitted venue
    When an operator adds the venue by name and address
    Then the add-venue counter records the result <result>

    Examples:
      | path state | google outcome | result                         |
      | enabled    | resolves       | created_google_only            |
      | disabled   | resolves       | besttime_rejected_no_geo_match |
      | enabled    | cannot resolve | google_only_enrichment_failed  |

  Scenario: The count of Google-sourced venues is observable
    Given the Google-only add path is enabled
    And Google Places resolves the submitted venue
    When an operator adds the venue by name and address
    Then the gauge of active Google-sourced venues reports one venue
