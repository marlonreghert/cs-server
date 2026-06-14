Feature: Eligibility as a gold-layer serving view
  Venue eligibility must be a dynamic SQL view that the Redis projection reads,
  not a destructive soft-delete. A venue is served only when it is active and
  passes the live block-list rules, so editing the rules changes serving on the
  next projection in both directions — blocking removes venues, unblocking brings
  them back — without ever changing a venue's lifecycle. Eligibility no longer
  soft-deletes; Google permanently-closed remains a real removal and is excluded
  because the view selects only active venues.

  Background:
    Given the venue eligibility serving view is available

  Scenario: The view returns only active, eligible venues
    Given an active venue with a blocked Google type
    And an active venue with an allowed Google type
    And an unlabeled active venue with no Google type
    When the serving view is evaluated
    Then the allowed venue is in the view
    And the unlabeled venue is in the view
    And the blocked-type venue is absent from the view

  Scenario: Unblocking a type brings its venues back with no lifecycle change
    Given an active venue whose Google type is currently blocked and absent from the view
    When the operator removes that Google type from the block-list
    And the serving view is re-evaluated
    Then the venue is present in the view
    And the venue lifecycle remained active throughout

  Scenario: Blocking a type removes its venues with no soft-delete
    Given an active venue whose Google type is currently allowed and present in the view
    When the operator adds that Google type to the block-list
    And the serving view is re-evaluated
    Then the venue is absent from the view
    And the venue lifecycle remained active

  Scenario: A good-category venue is not removed by an ambiguous name keyword
    Given an active venue whose name matches an ambiguous keyword
    And the venue resolves to a good category
    When the serving view is evaluated
    Then the venue is in the view
    But a non-good-category venue matching the same keyword is absent from the view

  Scenario: The projector reconciles Redis to exactly the view
    Given a venue that has just left the serving view
    And a venue that has just entered the serving view
    When the projector runs
    Then the venue that left the view is removed from the Redis geo index and venue key
    And the venue that entered the view is projected to Redis

  Scenario: Enrichment skips view-excluded venues but still enriches unlabeled ones
    Given an active venue excluded from the serving view
    And an unlabeled active venue in the serving view
    When an enrichment job selects venues to process
    Then the view-excluded venue is skipped
    And the unlabeled venue is enriched

  Scenario: A permanently-closed venue is excluded from the view
    Given a venue deprecated as permanently closed
    When the serving view is evaluated
    Then the venue is absent from the view
