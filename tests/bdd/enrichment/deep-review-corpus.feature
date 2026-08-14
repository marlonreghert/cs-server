Feature: Capture a deep review corpus for operator-selected venues
  As an operator
  I want to capture every review inside a recent window for venues I choose
  So that the product has evidence beyond the five reviews Google will return

  Background:
    Given the review window is 180 days
    And the per-venue review cap is 300
    And a monthly review budget is configured

  Scenario: A selected venue captures every review inside the window
    Given a venue with 40 reviews published inside the window
    When the operator runs a deep review crawl for that venue
    Then all 40 reviews are stored for that venue
    And the stored record is not marked truncated

  Scenario: Reviews older than the window are not stored
    Given a venue with 10 reviews inside the window and 90 reviews older than it
    When the operator runs a deep review crawl for that venue
    Then only the 10 in-window reviews are stored

  Scenario: Hitting the per-venue cap is reported, never silent
    Given a venue with 500 reviews published inside the window
    When the operator runs a deep review crawl for that venue
    Then 300 reviews are stored for that venue
    And the stored record is marked truncated
    And the run summary names that venue as truncated

  Scenario: A re-run fetches only reviews newer than the newest stored review
    Given a venue whose deep reviews were captured yesterday
    And 3 new reviews have been published since
    When the operator runs a deep review crawl for that venue again
    Then only 3 reviews are billed
    And the stored review count increases by 3

  Scenario: A re-run never duplicates a review already stored
    Given a venue whose deep reviews were captured yesterday
    And the actor returns reviews that were already captured
    When the operator runs a deep review crawl for that venue again
    Then the stored review count is unchanged
    And the duplicates are counted as duplicate outcomes

  Scenario: Reviews that age out of the window are retained
    Given a venue whose stored reviews include one now older than the window
    When the operator runs a deep review crawl for that venue again
    Then the aged review is still stored

  Scenario: A venue without a Google place id is skipped and reported
    Given a selected venue with no google_place_id
    When the operator runs a deep review crawl
    Then that venue is reported as skipped for a missing place id
    And no paid call is made for that venue

  Scenario: The estimate spends nothing
    Given the operator selects 200 venues
    When the operator requests an estimate for that selection
    Then the estimate reports the worst-case review count and cost
    And the estimate states that the count is an upper bound
    And no actor run is started

  Scenario: The budget is checked before the paid call
    Given the monthly review budget is already exhausted
    When the operator runs a deep review crawl
    Then no actor run is started
    And the run reports that the budget stopped it

  Scenario: A batch is refused when it would leave less than the reserved Apify headroom
    Given the shared Apify account headroom would fall below the reserved minimum after this batch
    When the operator runs a deep review crawl
    Then no actor run is started
    And the run reports that the budget stopped it

  Scenario: An Apify usage lookup failure refuses the batch rather than letting it pass
    Given the Apify account usage lookup fails
    When the operator runs a deep review crawl
    Then no actor run is started
    And the run reports that the budget stopped it

  Scenario: A run that would cross the budget stops at the boundary
    Given the remaining monthly budget covers only the first two of five venues
    When the operator runs a deep review crawl for the five venues
    Then the reviews fetched for the first two venues are stored
    And the run summary names the three venues it did not reach
    And the run outcome is partial

  Scenario: One venue's failure does not abort the run
    Given the actor fails for the second of three selected venues
    When the operator runs a deep review crawl
    Then the first and third venues are stored
    And the run summary names the failed venue

  Scenario: A client-level failure is stored as a failure, not as an empty success
    Given a venue whose actor call fails at the HTTP boundary
    When the operator runs a deep review crawl for that venue
    Then that venue is reported in the run's failed venues
    And no deep review row exists for that venue

  Scenario: Every venue failing is reported honestly, never as a success
    Given the actor fails for all three selected venues
    When the operator runs a deep review crawl
    Then the run summary names all three venues as failed
    And the run outcome is not ok

  Scenario: A venue that fetches nothing and has no prior record gets no stored row
    Given a selected venue with no prior deep review record
    And the actor returns no reviews for that venue
    When the operator runs a deep review crawl for that venue
    Then no deep review row exists for that venue

  Scenario: The existing Google Places review path is untouched
    Given a venue with five reviews from Google Places enrichment
    When the operator runs a deep review crawl for that venue
    Then the google_places reviews payload is unchanged
    And the venue_reviews_v1 projection for that venue is unchanged

  Scenario: The projection carries a bounded slice while RDS keeps everything
    Given a venue with 300 stored deep reviews
    And the projection slice is 40
    When the projector runs
    Then the deep review projection for that venue holds 40 reviews
    And they are the 40 newest
    And the RDS record still holds 300 reviews

  Scenario: A soft-deleted deep review row is removed from the projection
    Given a venue whose deep review row is soft-deleted
    When the projector runs
    Then the deep review key for that venue is absent from Redis

  Scenario: A filter may narrow but never widen an explicit selection
    Given the operator selects three venue ids
    And a filter that matches fifty venues
    When the operator runs a deep review crawl
    Then at most those three venues are crawled
