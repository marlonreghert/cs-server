@refresh
Feature: Priority-bounded BestTime refresh within the monthly unique-venue cap
  BestTime caps the account at a fixed number of unique venues interacted with
  per calendar month (currently 500), counting every live/forecast read. The
  system must spend that scarce allowance on a prioritized subset instead of
  reading every active venue, must keep venue discovery off, and must never ask
  BestTime for more unique venues than the monthly cap.

  Background:
    Given the monthly venue quota is 500 and the manual reserve is 10
    And the refresh budget X is therefore 490

  Scenario: Live refresh selects only the top-X active venues by priority
    Given 600 active venues exist with assorted priorities 0 through 5
    When the live forecast refresh runs
    Then it must request live forecasts for at most 490 distinct venues
    And it must select them ordered by priority ascending
    And no venue outside that selected set must be requested from BestTime

  Scenario: Weekly refresh reuses the same selected set as live refresh
    Given the live forecast refresh has selected its top-X venues
    When the weekly forecast refresh runs
    Then it must request weekly forecasts for the same venue set as live refresh
    And the union of venues touched by live and weekly refresh must not exceed 490 distinct venues

  Scenario: Selection tie-break is deterministic when priorities are equal
    Given multiple active venues share priority 0
    When the refresh selection is computed
    Then ties must be broken by reviews descending then rating descending
    And the selection must be stable across repeated runs

  Scenario: Venue discovery is disabled
    Given discovery is disabled by configuration
    When the scheduler starts
    Then the venue catalog discovery job must not be scheduled
    And a manual discovery trigger must be rejected as disabled
    And no BestTime venue-filter discovery call must be made

  Scenario: The monthly ledger refuses reads beyond the cap
    Given 500 distinct venues have already been touched this calendar month
    When any refresh requests a live forecast for a new venue id
    Then the request must be refused before calling BestTime
    And a skipped-by-cap metric must be incremented

  Scenario: Repeated reads of an already-touched venue do not consume new budget
    Given a venue was already touched this calendar month
    When the live refresh requests that same venue again
    Then it must be allowed without increasing the monthly unique-venue count

  Scenario: Add-venue geo fallback uses a 50m clutter radius
    Given BestTime rejects a manual add and the geo fallback runs
    When a candidate venue lies more than 50 meters from the requested point
    Then it must not be matched
    And the effective fallback radius must be 50 meters
