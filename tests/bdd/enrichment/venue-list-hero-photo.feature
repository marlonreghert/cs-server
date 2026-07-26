@wip
Feature: Venue list hero photo
  Every servable venue carries a pre-baked hero thumbnail URL that reaches the
  venue list without any Google call at serve time. The durable Google photo
  resource name is stored in RDS; the ephemeral keyless CDN URL is re-resolved
  on a schedule, published into the serving projection with a remaining TTL, and
  degrades to null rather than to a dead image.

  Background:
    Given the hero photo job is enabled
    And the daily Google photo call budget is 3000 calls

  # ── Discovery ────────────────────────────────────────────────────────────

  Scenario: Discovery stores the durable photo name and a resolved URL
    Given a servable venue with a Google place id
    And Google returns two photos for that place
    When the hero photo job discovers missing heroes
    Then a hero photo row is stored for the venue
    And the row holds the first photo's resource name
    And the row holds the author attribution of the first photo
    And the row holds a keyless CDN URL
    And the row state is "ok"

  Scenario: A venue without a Google place id is recorded terminally
    Given a servable venue with no Google place id
    When the hero photo job discovers missing heroes
    Then a hero photo row is stored for the venue with state "no_place_id"
    And no Google call is made for that venue

  Scenario: A venue whose place returns no photos is recorded terminally
    Given a servable venue with a Google place id
    And Google returns no photos for that place
    When the hero photo job discovers missing heroes
    Then a hero photo row is stored for the venue with state "no_photo"

  Scenario: Terminally recorded venues are never re-resolved by the schedule
    Given a hero photo row with state "no_photo"
    And a hero photo row with state "no_place_id"
    When the hero photo job runs again
    Then no Google call is made for either venue
    And both rows keep their state

  # ── Refresh ──────────────────────────────────────────────────────────────

  Scenario: Refreshing a stale row costs exactly one media call
    Given a hero photo row with state "ok" whose URL is older than the refresh interval
    When the hero photo job refreshes stale heroes
    Then exactly one photo media call is made using the stored photo resource name
    And no place details call is made
    And the row's URL and resolution time are updated

  Scenario: A warm on-demand photo cache yields the hero at no Google cost
    Given a hero photo row with state "ok" whose URL is older than the refresh interval
    And the venue has a warm on-demand photo cache entry
    When the hero photo job refreshes stale heroes
    Then the hero URL is derived from the cached entry
    And no Google call is made for that venue

  Scenario: Stale rows are refreshed oldest first
    Given three hero photo rows with state "ok" resolved at different times
    And the refresh limit is 2
    When the hero photo job refreshes stale heroes
    Then the two oldest rows are refreshed
    And the newest row is left untouched

  Scenario: A rotated photo resource name requeues the venue for discovery
    Given a hero photo row with state "ok" whose URL is older than the refresh interval
    And the photo media call returns a 4xx response
    When the hero photo job refreshes stale heroes
    Then the row's failure count is incremented
    And the venue is queued for re-discovery

  Scenario: Three consecutive failures soft-delete the row
    Given a hero photo row whose failure count is 2
    And the photo media call returns a 4xx response
    When the hero photo job refreshes stale heroes
    Then the row is soft-deleted
    And the venue serves no hero photo

  # ── Spend ledger ─────────────────────────────────────────────────────────

  Scenario: An exhausted daily budget stops the run mid-flight
    Given the daily Google photo call budget is 2 calls
    And five servable venues have no hero photo row
    When the hero photo job discovers missing heroes
    Then no more than 2 Google calls are made
    And the run reports that the budget was exceeded
    And the run exits without raising

  Scenario: The ledger also covers the on-demand detail resolve path
    Given the daily Google photo call budget is exhausted
    When an on-demand photo resolve is requested for a venue
    Then no Google call is made
    And the resolve returns an empty photo list

  # ── Projection ───────────────────────────────────────────────────────────

  Scenario: The projector publishes the hero with the remaining TTL
    Given a hero photo row resolved 2 hours ago
    And the hero photo max age is 12 hours
    When the projector rebuilds the serving projection
    Then the hero photo key is written with a TTL of about 10 hours
    And the TTL is not reset to the full max age

  Scenario: The projector deletes a hero aged past the ceiling
    Given a hero photo row resolved 20 hours ago
    And the hero photo max age is 12 hours
    When the projector rebuilds the serving projection
    Then the hero photo key is deleted
    And the venue serves no hero photo

  Scenario: A failing hero stage cannot abort the projection run
    Given three servable venues
    And the hero photo stage fails for the second venue
    When the projector rebuilds the serving projection
    Then the other two venues are still projected
    And the failure is recorded for the second venue

  Scenario: Deleting a venue removes its hero photo key
    Given a venue with a projected hero photo
    When the venue is deleted
    Then the hero photo key is removed

  # ── Serving ──────────────────────────────────────────────────────────────

  Scenario: The nearby response carries the hero photo URL
    Given a venue with a projected hero photo
    When a non-verbose nearby request is served
    Then the venue in the response carries its hero photo URL

  Scenario: A venue with no hero photo serves null rather than failing
    Given a venue with no projected hero photo
    When a non-verbose nearby request is served
    Then the venue in the response carries a null hero photo URL
    And the response succeeds

  Scenario: Serving never calls Google
    Given ten venues of which half have a projected hero photo
    When a non-verbose nearby request is served
    Then no Google call is made
    And five venues carry a hero photo URL and five carry null

  Scenario: The verbose branch is unchanged
    Given a venue with a projected hero photo
    When a verbose nearby request is served
    Then the verbose response shape is unchanged

  Scenario: A Redis outage degrades to no hero photo
    Given the serving projection is unavailable
    When a non-verbose nearby request is served
    Then every venue carries a null hero photo URL
    And the response succeeds

  # ── Regression guard ─────────────────────────────────────────────────────

  Scenario: The retired photos job is still absent
    When the "photos" job is triggered
    Then the response status is 404
