Feature: Admin-configurable venue type to category mapping

  An operator remaps Google Places or BestTime venue types to VibeSense
  categories from the admin panel. cs-server serves the new category on the next
  request with no venue re-fetch. The mapping is validated, persisted (RDS truth
  + Redis mirror), and falls open to hardcoded defaults on missing or bad config.

  Scenario: GET returns the hardcoded defaults when no override is stored
    Given no venue category-map override is stored
    When the operator requests GET /admin/venues/category-map
    Then the response maps google type "bar" to category "BAR"
    And the response maps besttime type "CLUBS" to category "NIGHTCLUB"

  Scenario: POST a valid override is persisted and reflected in a later GET
    Given no venue category-map override is stored
    When the operator saves a google type mapping of "bakery" to "FOOD_DRINK"
    Then the response status is 200
    And a subsequent GET /admin/venues/category-map maps google type "bakery" to category "FOOD_DRINK"
    And the subsequent GET still maps google type "bar" to category "BAR"

  Scenario: POST with an unknown category value is rejected
    Given no venue category-map override is stored
    When the operator saves a google type mapping of "bakery" to "FOO"
    Then the response status is 400
    And a subsequent GET /admin/venues/category-map does not map google type "bakery"

  Scenario: POST normalizes key casing
    Given no venue category-map override is stored
    When the operator saves both a google mapping "BAKERY" to "FOOD_DRINK" and a besttime mapping "juice" to "FOOD_DRINK"
    Then a subsequent GET /admin/venues/category-map maps google type "bakery" to category "FOOD_DRINK"
    And the subsequent GET maps besttime type "JUICE" to category "FOOD_DRINK"

  Scenario: A remapped type changes the served category without re-fetching venues
    Given a stored venue whose google primary type is "bakery" and besttime type is absent
    And the served category for that venue is "OTHER"
    When the operator saves a google type mapping of "bakery" to "FOOD_DRINK"
    And nearby venues are served
    Then that venue is served with category "FOOD_DRINK"

  Scenario: Serving falls open to defaults when the stored override is malformed
    Given the venue category-map override is stored as malformed data
    When nearby venues are served for a venue whose google primary type is "bar"
    Then that venue is served with category "BAR"
