@wip
Feature: Venue address components from Google Places
  As the events and venue serving layers
  I want the structured address components Google already returns to be stored
  So that a venue carries its bairro instead of only a raw address string

  Background:
    Given "Casa Bacurau" is a venue with a Google place id

  Scenario: Store the bairro from the Places address components
    Given Google Places returns address components with sublocality level 1 "Santo Amaro"
    When "Casa Bacurau" is enriched
    Then the stored address neighborhood is "Santo Amaro"

  Scenario: Fall back to sublocality when no sublocality level 1 is returned
    Given Google Places returns address components with sublocality "Boa Vista" and no sublocality level 1
    When "Casa Bacurau" is enriched
    Then the stored address neighborhood is "Boa Vista"

  Scenario: Fall back again for a municipality that publishes no sublocality
    Given Google Places returns address components with only an administrative area level 2
    When "Casa Bacurau" is enriched
    Then the stored address neighborhood is the administrative area level 2

  Scenario: Keep a stored bairro when the response carries no component for it
    Given the stored address neighborhood is "Santo Amaro"
    And Google Places returns address components with no sublocality of any kind
    When "Casa Bacurau" is enriched
    Then the stored address neighborhood is still "Santo Amaro"

  Scenario: Store the remaining components alongside the bairro
    Given Google Places returns route, sublocality, locality and postal code components
    When "Casa Bacurau" is enriched
    Then the stored address street, city and postal code are populated

  Scenario: Request the address components without widening the billed field set
    When the Places details request for "Casa Bacurau" is built
    Then the field mask includes the address components
    And the field mask requests no tier above the one it already requested
