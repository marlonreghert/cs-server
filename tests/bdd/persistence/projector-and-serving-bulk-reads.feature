@wip
Feature: Projector and serving bulk reads preserve outputs with bounded round-trips
  The Redis projection rebuild and the nearby-venues serving path must produce
  exactly the outputs they produce today while reading their inputs in bulk:
  the projector must not query RDS per venue, the nearby endpoint must not GET
  Redis per venue, and blocking admin work must never stall the event loop.

  Background:
    Given an RDS state with 3 servable venues carrying every enrichment family, weekly forecasts for all 7 days, and a live forecast
    And 1 servable venue with no Google opening hours and no live forecast
    And 1 active venue excluded from the serving view by the eligibility block-list
    And 1 venue whose vibe-attributes enrichment is soft-deleted

  # ── Projector: equivalence + bounded queries ─────────────────────────────

  Scenario: A bulk-read rebuild writes the same projection as the per-venue rebuild
    When the Redis projection rebuild runs
    Then every servable venue must be projected with the same Redis keys and serialized values as the RDS state dictates
    And the soft-deleted enrichment must not be projected
    And the ineligible venue must be removed from the serving projection
    And the rebuild summary must report the same venue, enrichment, live, removed, and error counts as before the change

  Scenario: Projector RDS queries do not grow with the venue count
    When the Redis projection rebuild runs
    Then the number of RDS queries issued must not exceed 12
    And the number of RDS queries must be the same regardless of how many servable venues exist

  Scenario: One bad venue row still skips only that venue
    Given one servable venue whose RDS row cannot be parsed
    When the Redis projection rebuild runs
    Then the rebuild summary must count 1 error
    And every other servable venue must still be projected

  # ── Nearby serving: equivalence + bounded round-trips ────────────────────

  Scenario: The nearby response body is unchanged by pipelined reads
    Given the serving projection has been rebuilt
    When a client requests nearby venues around the seeded coordinates
    Then the minified response body must be byte-identical to the response produced by per-key reads
    And the venue without Google hours must carry hours derived from its BestTime weekly forecast with hours source "besttime"

  Scenario: A stale live value is still suppressed after pipelining
    Given the serving projection holds a live forecast older than the freshness window for one venue
    When a client requests nearby venues around the seeded coordinates
    Then that venue must be served without a live busyness value
    And the metric "venue_serve_live_busyness_total" outcome "suppressed_stale" must be incremented

  Scenario: Nearby Redis round-trips are bounded
    When a client requests nearby venues around the seeded coordinates
    Then the number of Redis round-trips for the request must not grow with the number of venues returned

  # ── Event-loop responsiveness ─────────────────────────────────────────────

  Scenario: Health stays responsive while the admin inventory listing runs
    Given an admin venue-inventory listing is executing against a slow store
    When a client requests the health endpoint during the listing
    Then the health endpoint must respond without waiting for the listing to finish
