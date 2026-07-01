Feature: Google-only venue enrichment (add-time, admin-triggered, pending backfill)
  As the venue platform
  I must enrich venues with Google Places metadata (type, hours, reviews,
  business status, rating, price) at add time and via a pending backfill,
  spending no BestTime credit on enrichment,
  so venues are usable immediately without polluting the BestTime quota.

  # Add-time enrichment
  Scenario: A manual add with a place_id is fully Google-enriched inline
    Given an operator adds a venue and selects a Google candidate with a place_id
    When the venue is added
    Then the venue is immediately enriched from Google Places
    And it has a google primary type, opening hours, reviews, business status, rating, and a Google-derived price
    And no BestTime call is made during enrichment

  Scenario: A manual add without a place_id resolves one via Google search
    Given an operator adds a venue with no place_id
    When the venue is added
    Then a Google place_id is resolved via Google search
    And the venue is enriched from Google Places
    And the resolved place_id is persisted for future re-enrichment

  Scenario: Enrichment failure never fails the add
    Given Google returns no match or the details call fails for an added venue
    When the venue is added
    Then the add still succeeds
    And the venue's Google fields remain empty
    And no BestTime price fallback is applied

  # On-demand enrichment (admin panel trigger; cron stays disabled)
  Scenario: The admin-triggered enrichment enriches only venues that need it
    Given the Google enrichment job is triggered from the admin panel
    When it processes the catalog without forcing a refresh
    Then already-enriched venues are skipped
    And no BestTime call is made

  # One-time backfill of pending venues
  Scenario: The backfill enriches only pending venues
    Given a mix of enriched venues and pending venues with no google primary type
    When the pending backfill runs
    Then only the pending venues are enriched
    And already-enriched venues are not reprocessed

  Scenario: The backfill does not re-attempt a no-match venue on re-run
    Given a pending venue that Google has no match for
    When the pending backfill runs
    Then the venue is marked as attempted
    And a second backfill run does not call Google again for that venue

  # Google-only guarantee (backfill path)
  Scenario: A backfilled venue with no Google price ends with no price, not a BestTime tier
    Given a pending venue that carries a stored BestTime price tier but whose Google details carry no price
    When the pending backfill enriches it
    Then the venue's price is empty
    And its price source is not BestTime
