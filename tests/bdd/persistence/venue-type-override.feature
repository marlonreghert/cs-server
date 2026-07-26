@wip
Feature: Per-venue type override

  An operator corrects a single mis-typed venue by choosing its real category.
  cs-server stores a representative, locked google_primary_type so the venue
  serves in that category, is un-blocked by the live eligibility view, and the
  correction survives re-enrichment. Clearing the override lets Google's value
  return on the next forced re-enrichment.

  Scenario: Setting a type override stores a locked representative type and recategorizes the venue
    Given a venue "IRAQ" whose google primary type is "art_museum"
    When the operator overrides venue "IRAQ" to category "NIGHTCLUB"
    Then the response status is 200
    And venue "IRAQ" has stored google primary type "night_club"
    And venue "IRAQ" has its primary type locked
    And venue "IRAQ" resolves to category "NIGHTCLUB"

  Scenario: A forced re-enrichment does not overwrite a locked type
    Given a venue "IRAQ" whose google primary type is "art_museum"
    And the operator overrides venue "IRAQ" to category "NIGHTCLUB"
    When Google re-enrichment forces venue "IRAQ" back to type "art_museum"
    Then venue "IRAQ" has stored google primary type "night_club"

  Scenario: A normal re-enrichment preserves a locked type
    Given a venue "IRAQ" whose google primary type is "art_museum"
    And the operator overrides venue "IRAQ" to category "NIGHTCLUB"
    When a normal re-enrichment runs for venue "IRAQ"
    Then venue "IRAQ" has stored google primary type "night_club"

  Scenario: Clearing the override unlocks the venue so re-enrichment can restore Google's value
    Given a venue "IRAQ" whose google primary type is "art_museum"
    And the operator overrides venue "IRAQ" to category "NIGHTCLUB"
    When the operator clears the type override for venue "IRAQ"
    Then venue "IRAQ" no longer has its primary type locked
    And Google re-enrichment forces venue "IRAQ" back to type "art_museum"
    And venue "IRAQ" has stored google primary type "art_museum"

  Scenario: Overriding to an unknown category is rejected
    Given a venue "IRAQ" whose google primary type is "art_museum"
    When the operator overrides venue "IRAQ" to category "NOPE"
    Then the response status is 400
    And venue "IRAQ" has stored google primary type "art_museum"

  Scenario: Overriding a venue with no enrichment record is not found
    When the operator overrides venue "GHOST" to category "NIGHTCLUB"
    Then the response status is 404
