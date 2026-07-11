Feature: Projection and persistence integrity
  The Redis projection must converge to the RDS system of record in both
  directions every cycle: one bad row must never abort a run, deletions in
  RDS must remove the matching Redis keys, venue removal must cover every
  per-venue key family, and the engagement write path must be idempotent
  under its own retry contract and must refuse to run with an unset
  pseudonymization key.

  Background:
    Given servable venues "venue-a" and "venue-b" exist in RDS

  Scenario: A corrupt enrichment payload isolates to its venue
    Given "venue-a" has a vibe-profile enrichment row whose payload fails model validation
    And "venue-b" has valid enrichment rows
    When a projection run executes
    Then "venue-b" must be fully projected to Redis
    And the run summary must report at least one error naming "venue-a"
    And the reconcile removal pass must still execute in the same run

  Scenario: An RDS-deleted live forecast disappears from Redis on the next cycle
    Given "venue-a" has a live forecast projected in Redis
    And the live forecast row for "venue-a" is deleted in RDS
    When a projection run executes
    Then the Redis live forecast key for "venue-a" must not exist
    And "venue-a" must remain served in the Redis geo index

  Scenario: A soft-deleted enrichment row deletes its Redis key while the venue keeps serving
    Given "venue-a" has an Instagram enrichment projected in Redis
    And the Instagram enrichment row for "venue-a" is soft-deleted in RDS
    When a projection run executes
    Then the Redis Instagram key for "venue-a" must not exist
    And the other enrichment keys for "venue-a" must remain present

  Scenario: A soft-deleted weekly day is removed without touching other days
    Given "venue-a" has weekly forecasts projected for day_int 4 and day_int 5
    And the weekly forecast row for "venue-a" day_int 4 is soft-deleted in RDS
    When a projection run executes
    Then the Redis weekly forecast key for "venue-a" day_int 4 must not exist
    And the Redis weekly forecast key for "venue-a" day_int 5 must remain present

  Scenario: Venue deletion removes every per-venue key family
    Given "venue-a" has projected keys for its venue record, live forecast, weekly forecasts, vibe attributes, photos, fresh photos, IG posts, reviews, opening hours, and vibe profile
    When "venue-a" is removed from serving
    Then no Redis key for "venue-a" must remain in any per-venue key family

  Scenario: A retried hot-like write persists exactly one event row
    Given a hot-like write for user "u1" and "venue-a" has committed its RDS event row
    And the same write failed after the RDS commit and was retried per the router contract
    When the retried hot-like write completes
    Then exactly one hot-like event row must exist for "u1" and "venue-a" in the current business period
    And the retried request must succeed

  Scenario: Engagement writes refuse an empty pseudonymization key
    Given the engagement pseudonymization key is configured empty
    And engagement persistence is enabled
    When the service starts
    Then startup must fail with a clear error naming the pseudonymization key setting
