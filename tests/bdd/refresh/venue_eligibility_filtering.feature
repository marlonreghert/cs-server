Feature: Venue eligibility filtering
  As the VibeSense platform
  We must keep ineligible venues (drugstores, markets, churches, empty-named
  places, and other non-nightlife/non-food places) out of the active inventory
  and out of public responses, soft-delete the clearly-ineligible ones with a
  rejection reason so we never spend crawl credits on them, and let operators
  tune and inspect the filter.

  Background:
    Given a clean venue inventory
    And the eligibility filter uses the default blocked types, blocked Google types, and blocked name keywords

  # ── Inventory sync births junk as deprecated, not active ──────────────────
  Scenario: Inventory sync soft-deletes an empty-named venue at write time
    Given the BestTime account inventory contains a venue with an empty name
    When the inventory sync runs
    Then the venue is persisted as deprecated
    And its deprecated reason is "ineligible_empty_name"
    And its deprecated source is "eligibility_filter"
    And the venue is not returned by nearby serving

  Scenario: Inventory sync keeps an unclassified venue active under the block-list policy
    Given the BestTime account inventory contains a venue named "Bar do Zé" with no type
    When the inventory sync runs
    Then the venue is persisted as active
    And the venue is eligible for downstream enrichment

  Scenario: Inventory sync keeps an ambiguous-keyword venue active because no type is available
    Given the BestTime account inventory contains a venue named "Bar do Mercado" with no type
    When the inventory sync runs
    Then the venue is persisted as active

  # ── Eligibility sweep: cheap signals first, label only the survivors ───────
  Scenario: The eligibility sweep soft-deletes a venue whose name matches a hard blocked keyword
    Given an active venue named "Drogaria São Paulo" with no Google type
    When the eligibility sweep runs
    Then the venue is soft-deleted with reason "ineligible_name_keyword"
    And no Google Places lookup is performed for that venue

  Scenario: The eligibility sweep does not Google-label venues already rejected by cheap signals
    Given an active venue with an empty name
    And an active venue named "Igreja Batista Central" with no Google type
    When the eligibility sweep runs
    Then both venues are soft-deleted before any Google Places lookup
    And the Google Places labeling step only runs for venues that survived the cheap filters

  Scenario: The eligibility sweep soft-deletes a venue confirmed ineligible by its Google type
    Given an active venue named "Farmácia Pague Menos" whose Google type resolves to "pharmacy"
    When the eligibility sweep runs
    Then the venue is soft-deleted with reason "ineligible_google_type"
    And its deprecated source is "eligibility_filter"

  Scenario: The eligibility sweep keeps a real bar that Google confirms as nightlife
    Given an active venue named "Boteco da Praça" whose Google type resolves to "bar"
    When the eligibility sweep runs
    Then the venue remains active
    And the venue is not soft-deleted

  Scenario: An ambiguous name keyword never soft-deletes a venue before it is labeled
    Given an active venue named "Bar do Mercado" with no Google type
    When the eligibility sweep runs
    Then the venue remains active
    And the venue is not soft-deleted before a Google Places lookup

  Scenario: The eligibility sweep does not soft-delete a positively-classified venue that incidentally matches an ambiguous name keyword
    Given an active venue named "Parque Bar" whose Google type resolves to "bar"
    When the eligibility sweep runs
    Then the venue remains active

  Scenario: An ambiguous name keyword soft-deletes only after Google confirms a non-good category
    Given an active venue named "Mercado Central" whose Google type resolves to "supermarket"
    When the eligibility sweep runs
    Then the venue is soft-deleted with reason "ineligible_google_type"

  Scenario: Unknown or unlabeled venues remain active under the block-list policy
    Given an active venue named "Espaço Cultural XYZ" with BestTime type OTHER and no Google type
    When the eligibility sweep runs
    Then the venue remains active
    And the venue is not soft-deleted

  # ── Ineligible venues stop consuming crawl credits ────────────────────────
  Scenario: Soft-deleted ineligible venues are skipped by enrichment jobs
    Given an active venue named "Supermercado Bom Preço" whose Google type resolves to "supermarket"
    When the eligibility sweep runs
    And the photo, live forecast, and Instagram enrichment jobs run
    Then no crawl work is performed for "Supermercado Bom Preço"

  # ── Serving filter never returns junk or empty names ──────────────────────
  Scenario: Public nearby serving excludes empty-named and ineligible venues
    Given an active venue with an empty name within the search radius
    And an active venue typed CHURCH within the search radius
    And an active venue named "Bar do Zé" within the search radius
    When a client requests nearby venues
    Then the response includes "Bar do Zé"
    And the response excludes the empty-named venue
    And the response excludes the CHURCH-typed venue

  Scenario: Public nearby serving still returns ambiguous-keyword venues that are not yet labeled
    Given an active venue named "Bar da Praça" within the search radius
    When a client requests nearby venues
    Then the response includes "Bar da Praça"

  # ── Admin-tunable eligibility configuration ───────────────────────────────
  Scenario: Operators read the active eligibility configuration
    When an operator requests the eligibility configuration
    Then the response returns the active blocked types, blocked Google types, and blocked name keywords

  Scenario: Operators update the eligibility configuration and the change takes effect without redeploy
    Given an active venue named "Sunset Lounge" with no Google type
    When an operator adds "lounge" to the blocked name keywords
    And a client requests nearby venues
    Then the response excludes "Sunset Lounge"

  Scenario: Invalid eligibility configuration is rejected and the active filter is unchanged
    When an operator submits an eligibility configuration with a non-list blocked-types value
    Then the update is rejected with a validation error
    And the active eligibility configuration is unchanged

  # ── Observability ─────────────────────────────────────────────────────────
  Scenario: Soft-deleting an ineligible venue emits a reason-labelled metric
    Given an active venue named "Drogaria São Paulo" with no Google type
    When the eligibility sweep runs
    Then the soft-deleted venues metric increments for reason "ineligible_name_keyword" and source "eligibility_filter"
    And the deprecated-venues gauge reflects the new deprecated venue

  # ── Idempotency and lifecycle safety ──────────────────────────────────────
  Scenario: Re-running the eligibility sweep does not reactivate or re-deprecate venues
    Given a venue already deprecated with reason "ineligible_google_type"
    When the eligibility sweep runs again
    Then the venue stays deprecated with its original reason and timestamp
    And the venue is not reactivated
