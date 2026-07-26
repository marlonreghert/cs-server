Feature: Per-venue type override

  An operator corrects a single mis-typed venue by choosing its real category.
  cs-server stores a representative, locked google_primary_type so the venue
  serves in that category and is un-blocked by the live eligibility view; the
  lock protects the correction from re-enrichment (enrichment-guard behavior is
  covered by unit tests). Clearing the override unlocks the venue.

  Scenario: Overriding a venue to a category stores a locked representative type
    Given a venue "IRAQ" whose google primary type is "art_museum"
    When the operator overrides venue "IRAQ" to category "NIGHTCLUB"
    Then the response status is 200
    And the response category is "NIGHTCLUB"
    And the response google primary type is "night_club"
    And venue "IRAQ" has stored google primary type "night_club"
    And venue "IRAQ" has its primary type locked

  Scenario: Clearing an override unlocks the venue
    Given a venue "IRAQ" whose google primary type is "art_museum"
    And the operator overrides venue "IRAQ" to category "NIGHTCLUB"
    When the operator clears the type override for venue "IRAQ"
    Then the response status is 200
    And venue "IRAQ" no longer has its primary type locked

  Scenario: Overriding to an unknown category is rejected
    Given a venue "IRAQ" whose google primary type is "art_museum"
    When the operator overrides venue "IRAQ" to category "NOPE"
    Then the response status is 400
    And venue "IRAQ" has stored google primary type "art_museum"

  Scenario: Overriding to OTHER is rejected
    Given a venue "IRAQ" whose google primary type is "art_museum"
    When the operator overrides venue "IRAQ" to category "OTHER"
    Then the response status is 400

  Scenario: Overriding a venue with no enrichment record is not found
    When the operator overrides venue "GHOST" to category "NIGHTCLUB"
    Then the response status is 404
