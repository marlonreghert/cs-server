@wip
Feature: Admin venue-type breakdown and accent-folded add-venue geo short-circuit
  Operators must be able to read the catalog's venue-type breakdown, and a
  re-add of a venue whose submitted name differs from the cataloged name only
  by accents or punctuation must short-circuit on the free local geo index
  instead of spending a paid BestTime create on a venue we already have.

  # ── GET /admin/venue-type-breakdown ──────────────────────────────────────

  Scenario: Venue-type breakdown returns per-type counts
    Given the catalog contains 2 active venues with BestTime type "BAR"
    And the catalog contains 1 active venue with BestTime type "RESTAURANT"
    And both "BAR" venues have Google primary type "bar"
    And the "RESTAURANT" venue has no Google vibe attributes
    When the operator requests the admin venue-type breakdown
    Then the response status must be 200
    And the response field "total_venues" must be 3
    And the response field "with_google_type" must be 2
    And the "besttime_types" map must count 2 for "BAR" and 1 for "RESTAURANT"
    And the "google_places_types" map must count 2 for "bar"

  Scenario: Venue-type breakdown buckets venues without a BestTime type as unknown
    Given the catalog contains 1 active venue with no BestTime type
    When the operator requests the admin venue-type breakdown
    Then the response status must be 200
    And the "besttime_types" map must count 1 for "unknown"

  Scenario: Venue-type breakdown reports service unavailable before the container is initialized
    Given the application container is not initialized
    When the operator requests the admin venue-type breakdown
    Then the response status must be 503

  # ── POST /admin/venues/by-address: accent-folded geo short-circuit ───────

  Scenario: An accented re-add short-circuits on the local geo index without a BestTime call
    Given an active venue named "Laca Pina" is cataloged in the Redis geo index near latitude -8.05 and longitude -34.88
    And the monthly new venue counter for the current month is 100
    When the operator submits an add-venue request named "LAÇA, Pina" at latitude -8.05 and longitude -34.88
    Then the response status must be 200
    And the response body must identify the already-cataloged venue
    And no BestTime add-venue call must be made
    And the monthly new venue counter for the current month must remain 100
    And a metric "add_venue_by_address_total{result=\"already_exists\"}" must be incremented

  Scenario: A deprecated venue is still skipped by the accent-folded geo lookup
    Given a venue named "Laca Pina" near latitude -8.05 and longitude -34.88 was deprecated by a geo-link undo
    When the operator submits an add-venue request named "LAÇA, Pina" at latitude -8.05 and longitude -34.88
    Then the geo lookup must not short-circuit on the deprecated venue
    And the request must fall through to the BestTime create path

  Scenario: A genuinely different nearby name still misses the geo short-circuit
    Given an active venue named "Bar do Joao" is cataloged in the Redis geo index near latitude -8.05 and longitude -34.88
    When the operator submits an add-venue request named "Restaurante Maria" at latitude -8.05 and longitude -34.88
    Then the geo lookup must not short-circuit
    And the request must fall through to the BestTime create path
