@wip
Feature: Address components backfill sources Google's answer from Place Details
  As the events UI and the venue catalog
  I want the address-components backfill's Google rung to use the Place
  Details address-components lookup instead of the disabled legacy Geocoding
  API
  So that the backfill can actually reach Google's authoritative bairro data,
  the free-tier spend gate still makes every paid call unreachable when off,
  and a transient failure is retried on a future sweep instead of poisoning a
  row with a false answer

  Scenario: Google's Place Details answer fills the bairro for a venue with a stored place id
    Given "Casa Bacurau" is a venue with a Google place id and no stored address components
    And the address backfill geocoding switch is on
    And Google's Place Details address lookup returns address components with sublocality level 1 "Santo Amaro"
    When the address backfill processes "Casa Bacurau"
    Then the stored address neighborhood is "Santo Amaro" with source "google"

  Scenario: A response with no address components is a normal outcome, not an error
    Given "Boteco da Maré" is a venue with a Google place id and no stored address components
    And the address backfill geocoding switch is on
    And Google's Place Details address lookup returns no address components for "Boteco da Maré"
    And its raw address text is "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil"
    And "Niterói" is in the approved city vocabulary
    When the address backfill processes "Boteco da Maré"
    Then the stored address neighborhood is "Santa Rosa" with source "parsed"

  Scenario: A Place Details failure leaves the row's nulls untouched for a future sweep
    Given "Padaria Casa Caiada" is a venue with a Google place id and no stored address components
    And the address backfill geocoding switch is on
    And Google's Place Details address lookup fails with a transport error for "Padaria Casa Caiada"
    When the address backfill processes "Padaria Casa Caiada"
    Then the stored address neighborhood is still unset
    And the address lookup outcome "api_error" is recorded, not "success"

  Scenario: The backfill makes no Place Details address lookups while the free-tier switch is off
    Given the address backfill geocoding switch is off
    And "Boteco da Maré" is a venue with a Google place id and no stored address components
    And its raw address text is "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil"
    And "Niterói" is in the approved city vocabulary
    When the address backfill processes "Boteco da Maré"
    Then no Place Details address lookup was made
    And the stored address neighborhood is "Santa Rosa" with source "parsed"

  Scenario: Google's Place Details answer still outranks a previously parsed bairro
    Given "Boteco da Maré" has a stored address neighborhood of "Santa Rosa" with source "parsed"
    And "Boteco da Maré" has a Google place id
    And the address backfill geocoding switch is on
    And Google's Place Details address lookup returns address components with sublocality level 1 "Santa Rosa Baixa"
    When the address backfill processes "Boteco da Maré"
    Then the stored address neighborhood is "Santa Rosa Baixa" with source "google"

  Scenario: An operator-entered bairro is still never overwritten by the new Google rung
    Given "Restaurante do Porto" has a stored address neighborhood of "Recife Antigo" with source "operator"
    And "Restaurante do Porto" has a Google place id
    And the address backfill geocoding switch is on
    And Google's Place Details address lookup returns address components with sublocality level 1 "Bairro do Recife"
    When the address backfill processes "Restaurante do Porto"
    Then the stored address neighborhood is still "Recife Antigo" with source "operator"

  Scenario: One backfill trigger never processes more than the configured batch size
    Given 5 venues are waiting to be backfilled, all with a Google place id
    And the address backfill batch size is configured to 2
    And the address backfill geocoding switch is on
    When the address backfill runs one trigger with no explicit limit
    Then exactly 2 venues are processed
    And at most 2 Place Details address lookups were made

  Scenario: An operator resets the backfill cursor to sweep the catalog again
    Given the backfill cursor is parked at the end of the catalog from a completed sweep
    When an operator resets the backfill cursor to the start
    And the address backfill runs a trigger
    Then venues from the beginning of the catalog are selected again
