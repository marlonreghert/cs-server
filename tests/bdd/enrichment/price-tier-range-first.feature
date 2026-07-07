Feature: Price tier prefers the objective range over the coarse Google enum
  As the price pipeline
  I must derive the served price tier from the objective Google priceRange first,
  fall back to the priceLevel enum only when no usable range exists and then to
  BestTime, tune the range thresholds to the local market so venues spread across
  tiers instead of piling at one tier, and backfill existing venues so the
  correction reaches the serving projection. The served tier stays an integer 1 to
  4 or null and is never 0.

  Background:
    Given enrichment derives the served price tier in the order
      | rank | source        |
      | 1    | google_range  |
      | 2    | google_enum   |
      | 3    | besttime      |
      | 4    | null          |
    And the served price tier is an integer 1 to 4 or null
    And the served price tier is never 0

  Scenario: When both Google signals are present the objective range wins over the enum
    Given a price-relevant venue whose Google details carry both a priceLevel enum of PRICE_LEVEL_MODERATE and a priceRange of BRL 80 to 160
    When the venue is enriched
    Then its served price_level is derived from the range, not from the enum
    And its price_level_source is recorded as "google_range"
    And its price_range is persisted as currency "BRL" with min 80 and max 160

  Scenario: Tuned thresholds give a cheaper venue a lower tier than a pricier one
    Given a price-relevant venue with a priceRange of BRL 40 to 120 and no priceLevel enum
    And a price-relevant venue with a priceRange of BRL 80 to 160 and no priceLevel enum
    When both venues are enriched
    Then the venue priced BRL 40 to 120 has a strictly lower served price_level than the venue priced BRL 80 to 160
    And both venues have a price_level_source of "google_range"

  Scenario: A venue with an enum but no usable range is tiered from the enum
    Given a price-relevant venue whose Google details carry a priceLevel enum of PRICE_LEVEL_MODERATE
    And the venue has no usable Google priceRange
    When the venue is enriched
    Then its served price_level is derived from the enum
    And its price_level_source is recorded as "google_enum"

  Scenario: A venue with no Google price signal falls back to BestTime
    Given a price-relevant venue with no Google price signal
    And the venue has a BestTime price tier of 2
    When the venue is enriched
    Then its served price_level is tier 2 from BestTime
    And its price_level_source is recorded as "besttime"

  Scenario: A free or unpriceable venue resolves to unknown
    Given a price-relevant venue whose Google priceLevel enum is PRICE_LEVEL_FREE and which has no usable priceRange
    When the venue is enriched
    Then its served price_level is null
    And its price_level_source is null

  Scenario: An enum-less range with an unbounded upper bound buckets from the lower bound
    Given a price-relevant venue whose Google priceRange has a startPrice of BRL 90 and no endPrice
    And the venue has no usable Google priceLevel enum
    When the venue is enriched
    Then its served price_level is derived from the range lower bound
    And its price_level_source is recorded as "google_range"
    And its price_range is persisted as currency "BRL" with min 90 and a null max

  Scenario: Backfilling existing venues recomputes the tier range-first
    Given existing venues each stored with a priceLevel enum of PRICE_LEVEL_MODERATE, a priceRange of BRL 80 to 160, and a price_level_source of "google_enum"
    When the price-tier backfill is applied
    Then each such venue's price_level is recomputed from the range
    And each such venue's price_level_source becomes "google_range"

  Scenario: The price-tier backfill is idempotent
    Given the price-tier backfill has already been applied
    When the price-tier backfill is applied again
    Then no venue's price_level or price_level_source changes

  Scenario: The backfill leaves signal-less venues unknown
    Given an existing venue with no priceLevel enum, no priceRange, and no BestTime price
    When the price-tier backfill is applied
    Then its served price_level remains null
    And its price_level_source remains null
