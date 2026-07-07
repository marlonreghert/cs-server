Feature: Objective priceRange primary, priceLevel enum fallback for the price tier
  As the price pipeline
  I must derive the served price tier from Google's objective priceRange first,
  fall back to the coarse priceLevel enum only when no usable range exists and then
  to BestTime, never serve tier 0, persist the raw signals for audit, and project a
  structured price range.

  Background:
    Given enrichment derives the served price tier in the order
      | rank | source        |
      | 1    | google_range  |
      | 2    | google_enum   |
      | 3    | besttime      |
      | 4    | null          |
    And the served price tier is an integer 1 to 4 or null
    And the served price tier is never 0

  Scenario: A venue with a Google priceLevel enum is tiered from the enum
    Given a price-relevant venue whose Google details carry a priceLevel enum of PRICE_LEVEL_VERY_EXPENSIVE
    When the venue is enriched
    Then its served price_level is derived from the enum as tier 4
    And its price_level_source is recorded as "google_enum"
    And its served price_level is not 0

  Scenario: When both Google signals are present the objective range wins over the enum
    Given a price-relevant venue whose Google details carry both a priceLevel enum of PRICE_LEVEL_MODERATE and a priceRange of BRL 80 to 160
    When the venue is enriched
    Then its served price_level resolves to an expensive tier of 3 or 4 from the range
    And its price_level_source is recorded as "google_range"
    And its price_range is persisted as currency "BRL" with min 80 and max 160

  Scenario: An enum-less expensive venue is tiered from its price range
    Given a price-relevant venue whose Google details carry a priceRange of BRL 80 to 200
    And the venue has no usable Google priceLevel enum
    When the venue is enriched
    Then its served price_level resolves to an expensive tier of 3 or 4
    And its price_range is persisted as currency "BRL" with min 80 and max 200
    And its price_level_source is recorded as "google_range"
    And its served price_level is not 0

  Scenario: A free-priced venue with no usable range resolves to unknown
    Given a price-relevant venue whose Google priceLevel enum is PRICE_LEVEL_FREE
    And the venue has no usable Google priceRange
    When the venue is enriched
    Then its served price_level is null
    And its price_level_source is null

  Scenario: A venue with no Google price signal and no BestTime price resolves to unknown
    Given a price-relevant venue whose Google details carry neither a priceLevel enum nor a priceRange
    And the venue has no BestTime price
    When the venue is enriched
    Then its served price_level is null
    And its price_range is null
    And its price_level_source is null
    And its served price_level is not 0

  Scenario: A venue with only a BestTime price falls back to BestTime
    Given a price-relevant venue with no Google price signal
    And the venue has a BestTime price tier of 2
    When the venue is enriched
    Then its served price_level is tier 2 from BestTime
    And its price_level_source is recorded as "besttime"
    And the raw BestTime price is retained in besttime_price_level

  Scenario: An enum-less venue range with an unbounded upper bound is bucketed from the lower bound
    Given a price-relevant venue whose Google priceRange has a startPrice of BRL 180 and no endPrice
    And the venue has no usable Google priceLevel enum
    When the venue is enriched
    Then its price_range is persisted as currency "BRL" with min 180 and a null max
    And its served price_level resolves to an expensive tier of 3 or 4
    And its price_level_source is recorded as "google_range"

  Scenario: Applying the migration converts every legacy zero tier to unknown
    Given existing venues persisted with price_level values of 0, 1, 2, 3 and 4
    When the price-signal migration is applied
    Then every venue previously at price_level 0 now has price_level null
    And venues at price_level 1 through 4 are left unchanged

  Scenario: A non-priceable venue is never assigned a price tier
    Given a venue selected by Google primaryType as a non-priceable place such as a mall or park
    When the venue is enriched
    Then its served price_level is null
    And its price_level_source is null

  Scenario: Manually adding an enum-less venue by address applies the same never-zero rule
    Given a venue added by address whose Google match carries a priceRange of BRL 80 to 200 and no priceLevel enum
    When the venue is created
    Then its served price_level resolves to an expensive tier of 3 or 4 from the range
    And its served price_level is not 0
    And its price_range is persisted as currency "BRL" with min 80 and max 200
    And its price_level_source is recorded as "google_range"
