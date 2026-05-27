Feature: Add venue to the BestTime account inventory by Brazilian address
  Operators must be able to add a specific venue to our BestTime account
  inventory by submitting its name and address. The system must protect a
  configurable monthly budget of unique new venues so that automated discovery
  never starves the manual add path. The monthly crawler must also sync the
  full BestTime account inventory into Redis at the start of each run so the
  upstream pipeline can serve live busyness for every venue already free to
  query, with no credit cost.

  Background:
    Given the monthly new venue quota is configured to 500
    And the manual add reserve is configured to 10
    And the current calendar month is "2026-05"
    And every add-venue request includes "venue_name", "venue_address", "venue_lat", and "venue_lng" sourced from a Google Places candidate

  Scenario: Add a new venue by address when budget is available
    Given the venue inventory has gained 100 unique new venues in "2026-05"
    And the BestTime account inventory does not contain the submitted address
    When the operator submits a Google Places candidate with venue_name "Bar do Joao", venue_address "Rua das Flores 123, Recife - PE", venue_lat -8.05, and venue_lng -34.88
    Then the response status must be 201
    And the response body must include the returned "venue_id"
    And the response body must include "venue_name", "venue_address", "venue_lat", "venue_lng"
    And the response body must include "source" equal to "besttime_new"
    And the venue must be persisted in the Redis geo index
    And the live forecast for the new venue must be cached when BestTime returns one
    And the weekly forecast for the new venue must be cached when BestTime returns one
    And the monthly new venue counter for "2026-05" must be incremented to 101
    And a metric "add_venue_by_address_total{result=\"created\"}" must be incremented

  Scenario: Normalise BestTime's "venue_lon" response field into our "venue_lng"
    Given BestTime returns a successful "/forecasts" response with the venue coordinate under "venue_lon"
    When the operator submits a valid Google Places candidate
    Then the response body must expose the coordinate as "venue_lng"
    And the persisted Redis venue record must store the coordinate under "venue_lng"

  Scenario: Reject the request when required Google Places fields are missing
    When the operator submits a request body missing "venue_lat" or "venue_lng"
    Then the response status must be 422
    And the BestTime add-venue endpoint must not be called
    And the monthly new venue counter for "2026-05" must not change

  Scenario: Return existing venue without spending the monthly budget when the address is already in the account inventory
    Given the BestTime account inventory already contains a venue matching the submitted name and address
    And the matching venue is present in the Redis geo index
    When the operator submits the same venue_name and venue_address
    Then the response status must be 200
    And the response body must indicate "already_exists" with the existing "venue_id"
    And the BestTime add-venue endpoint must not be called
    And the monthly new venue counter for "2026-05" must not change
    And a metric "add_venue_by_address_total{result=\"already_exists\"}" must be incremented

  Scenario: Reject manual add when the monthly quota is exhausted
    Given the venue inventory has gained 500 unique new venues in "2026-05"
    When the operator submits a new venue_name and venue_address
    Then the response status must be 429
    And the response body must include an explanation that the monthly quota is exhausted
    And the BestTime add-venue endpoint must not be called
    And the monthly new venue counter for "2026-05" must not change
    And a metric "add_venue_by_address_total{result=\"quota_exhausted\"}" must be incremented

  Scenario: Allow manual add to consume the reserved budget when discovery has filled the discovery cap
    Given the venue inventory has gained 490 unique new venues in "2026-05"
    And the BestTime account inventory does not contain the submitted address
    When the operator submits a new venue_name and venue_address
    Then the response status must be 201
    And the monthly new venue counter for "2026-05" must be incremented to 491

  Scenario: Validate the submitted address payload
    When the operator submits an empty venue_name with a valid venue_address
    Then the response status must be 422
    And the BestTime add-venue endpoint must not be called
    And the monthly new venue counter for "2026-05" must not change

  Scenario: Surface BestTime non-recoverable errors clearly without spending the monthly counter
    Given the venue inventory has gained 100 unique new venues in "2026-05"
    And BestTime returns an HTTP 5xx or transport error for the add-venue call
    When the operator submits a valid Google Places candidate
    Then the response status must be 502
    And the response body must explain that BestTime is unavailable
    And the geo fallback must not be attempted
    And the venue must not be persisted in the Redis geo index
    And the monthly new venue counter for "2026-05" must not change
    And a metric "add_venue_by_address_total{result=\"besttime_error\"}" must be incremented

  Scenario: Fall back to a tight-radius geo lookup when BestTime cannot geocode the address
    Given the venue inventory has gained 100 unique new venues in "2026-05"
    And BestTime responds with HTTP 400 or "status=Error" for the "/forecasts" call
    And the BestTime account inventory contains a venue at the submitted coordinate whose name matches the submitted "venue_name" after case folding
    When the operator submits a valid Google Places candidate
    Then cs-server must call "/venues/filter" once with the submitted coordinate and the configured fallback radius
    And the response status must be 200
    And the response body must include "status" equal to "matched_via_geo_fallback"
    And the response body must include the existing "venue_id"
    And the venue must be persisted in the Redis geo index when it was not already there
    And the monthly new venue counter must increment only when the matched venue was new to the Redis geo index
    And a metric "add_venue_by_address_total{result=\"matched_via_geo_fallback\"}" must be incremented

  Scenario: Geo fallback finds no matching venue near the submitted coordinate
    Given the venue inventory has gained 100 unique new venues in "2026-05"
    And BestTime responds with HTTP 400 or "status=Error" for the "/forecasts" call
    And the BestTime account inventory contains no venue at the submitted coordinate whose name matches the submitted "venue_name"
    When the operator submits a valid Google Places candidate
    Then cs-server must call "/venues/filter" once and find no matching venue
    And the response status must be 502
    And the response body must explain that BestTime rejected the address and the geo fallback found no match
    And the venue must not be persisted in the Redis geo index
    And the monthly new venue counter for "2026-05" must not change
    And a metric "add_venue_by_address_total{result=\"besttime_rejected_no_geo_match\"}" must be incremented

  Scenario: Treat an inventory address as already_exists by lookup, before any BestTime call
    Given the BestTime account inventory already contains a venue at the submitted coordinate within the fallback radius
    And the matching venue's case-folded name matches the submitted "venue_name"
    When the operator submits the same Google Places candidate
    Then the response status must be 200
    And the response body must include "status" equal to "already_exists"
    And no BestTime endpoint must be called
    And the monthly new venue counter for "2026-05" must not change

  Scenario: Reject the request when "venue_address" is the inventory-normalised form rather than a Google Places formatted_address
    # Guards against the failure mode observed in the live probe: BestTime
    # rejects its own normalised output when fed back to /forecasts.
    Given the operator's request reuses an address copied from cs-server's inventory list rather than from Google Places
    And BestTime responds with HTTP 400 or "status=Error" for the "/forecasts" call
    When cs-server runs the geo fallback and matches the inventory venue at the submitted coordinate
    Then the response status must be 200
    And the response body must include "status" equal to "matched_via_geo_fallback"
    And the response body must include a hint that vibes_bot should send Google Places "formatted_address" to avoid the geocoder rejection

  Scenario: Discovery refresh must stop short of the manual add reserve
    Given the monthly new venue quota is configured to 500
    And the manual add reserve is configured to 10
    And the venue inventory has gained 480 unique new venues in "2026-05"
    When the discovery refresh job runs
    Then the discovery job must request at most 10 additional unique new venues from BestTime
    And the discovery job must not cause the monthly counter to exceed 490
    And the discovery job must log when it stops short due to the manual add reserve

  Scenario: Increment monthly counter only for venues new to the BestTime account inventory
    Given the discovery refresh receives venues from BestTime that include some venue_ids already present in the BestTime account inventory
    When the discovery refresh processes the response
    Then the monthly new venue counter must increase only by the number of venue_ids that were not part of the BestTime account inventory before this batch
    And venues that were already in the BestTime account inventory must not affect the monthly counter

  Scenario: Reload monthly quota and reserve from admin config on each request
    Given the admin config "venue_monthly_quota" is updated from 500 to 600
    And the admin config "venue_monthly_manual_reserve" is updated from 10 to 25
    When the operator submits a new venue_name and venue_address
    Then the add-venue path must use the updated quota of 600
    And the discovery refresh must use the updated discovery effective cap of 575

  Scenario: Reset the monthly counter when the calendar month rolls over
    Given the current calendar month is "2026-05"
    And the venue inventory has gained 500 unique new venues in "2026-05"
    When the calendar month rolls over to "2026-06"
    And the operator submits a new venue_name and venue_address
    Then the response status must be 201
    And the monthly new venue counter for "2026-06" must be 1
    And the monthly new venue counter for "2026-05" must remain at 500

  Scenario: Sync the full BestTime account inventory into Redis at the start of the monthly crawler
    Given the BestTime account inventory currently contains 1330 venues
    And the Redis geo index currently contains 575 of those venues
    When the monthly crawler runs
    Then the crawler must first list every venue in the BestTime account inventory via the BestTime venues endpoint
    And the crawler must upsert every inventory venue not already in the Redis geo index, using its venue_id, name, address, latitude, and longitude
    And the monthly new venue counter must not be incremented for inventory-sync upserts
    And the inventory-sync step must not call the BestTime add-venue or filter endpoints
    And the inventory-sync step must complete before the discovery refresh step starts
    And the crawler must emit a metric for inventory venues seen, inventory venues newly upserted, and inventory venues skipped

  Scenario: Inventory sync persists venues even when BestTime has no forecast for them yet
    Given a BestTime account inventory venue has "venue_forecasted" false and no foot traffic data
    When the monthly crawler's inventory-sync step processes the venue
    Then the venue must be upserted into the Redis geo index with its id, name, address, latitude, and longitude
    And the absence of forecast data must not block the upsert
    And later live and weekly refresh cycles must include this venue without spending any monthly budget

  Scenario: Inventory sync failure must not abort the monthly crawler
    Given the BestTime venues endpoint returns an error during inventory sync
    When the monthly crawler runs
    Then the crawler must log the inventory-sync failure with enough context to troubleshoot
    And the crawler must continue with the discovery refresh step
    And the discovery refresh must still respect the monthly new venue quota and manual add reserve

  Scenario: BestTime client model accepts both "venue_lng" and "venue_lon" via alias
    # Pinned to the schema-naming inconsistency captured in the live probe:
    # /api/v1/venues emits "venue_lng" while /forecasts emits "venue_lon".
    Given a BestTime response body emits the coordinate under either "venue_lng" or "venue_lon"
    When cs-server parses the body
    Then the resulting venue model must expose the coordinate consistently as "venue_lng"
    And no parsing error must be raised because of the field-name difference

  Scenario: Fresh BestTime venue creation returns a valid response shape that may differ from idempotent re-add
    # Pinned to Probe D in the Pre-Implementation Verification section:
    # the fresh-create response captured once with a real not-in-inventory
    # venue is the canonical fixture for this scenario. Probe D is run
    # exactly once, supervised, and never re-run by automation.
    Given the submitted address resolves to a real venue that is NOT in the BestTime account inventory
    And the venue inventory has gained 100 unique new venues in "2026-05"
    When the operator submits a valid Google Places candidate
    And BestTime responds with the fresh-create payload captured in "tests/fixtures/besttime/forecasts_post_fresh_create_ok.json"
    Then the response status must be 201
    And the response body must include "source" equal to "besttime_new"
    And the venue must be persisted in the Redis geo index with the venue_id BestTime returned
    And the live and weekly forecasts must be cached only when BestTime's fresh-create payload actually contains them
    And the monthly new venue counter for "2026-05" must be incremented to 101
    And the parsed venue model must be structurally identical to the model produced from the captured re-add payload, regardless of whether the fresh-create analysis array is partial or fully populated
