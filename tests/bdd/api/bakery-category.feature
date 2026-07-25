@wip
Feature: BAKERY ("Padaria") venue category

  Google Places "bakery" venues resolve to a dedicated VibeSense category BAKERY
  (label "Padaria", emoji croissant) instead of OTHER. The category is resolved
  at serve time and its label/emoji flow through to the app unchanged. The admin
  type-to-category map may target BAKERY like any other category.

  Scenario: A bakery venue is served in the BAKERY category
    Given a venue whose google primary type is "bakery"
    When that venue's display is resolved
    Then the resolved category is "BAKERY"
    And the resolved label is "Padaria"
    And the resolved emoji is the croissant emoji

  Scenario: The bakery granular label is Portuguese
    Given a venue whose google primary type is "bakery"
    When that venue's display is resolved
    Then the resolved granular label is "Padaria"

  Scenario: The admin category-map accepts BAKERY as a target category
    When the operator saves a google type mapping of "padaria_artesanal" to "BAKERY"
    Then the response status is 200
    And a subsequent GET /admin/venues/category-map maps google type "padaria_artesanal" to category "BAKERY"
