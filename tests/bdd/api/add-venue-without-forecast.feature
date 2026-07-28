@wip
Feature: Add a venue BestTime registers but cannot forecast

  An operator must be able to catalog a venue that exists on BestTime's side but
  has no foot-traffic forecast. Today the add fails with 502 and nothing is
  persisted, because the geo fallback searches an endpoint that only returns
  forecasted venues. Membership in the BestTime account inventory — not the
  wording of BestTime's error message — decides whether the venue is real.

  Background:
    Given the monthly new-venue quota has slots available
    And the BestTime account inventory is reachable

  Scenario: Persist a venue that BestTime registered without a forecast
    Given BestTime rejects the create for the submitted venue
    And the submitted venue is present in the BestTime account inventory with "venue_forecasted" false
    When the operator submits the venue name and address
    Then the response status must be 201
    And the response status field must be "created_without_forecast"
    And the response source field must be "besttime_inventory"
    And the venue must be persisted with its forecast flag false
    And the monthly new venue counter must include this venue
    And the venue must be reachable through the nearby venues endpoint

  Scenario: The reconcile must not spend a second create or a filter call
    Given BestTime rejects the create for the submitted venue
    And the submitted venue is present in the BestTime account inventory with "venue_forecasted" false
    When the operator submits the venue name and address
    Then the BestTime add-venue endpoint must have been called exactly once
    And the BestTime venue-filter endpoint must not have been called
    And the only inventory access must be the free account listing

  Scenario: A venue absent from the inventory keeps today's rejection path
    Given BestTime rejects the create for the submitted venue
    And the submitted venue is absent from the BestTime account inventory
    When the operator submits the venue name and address
    Then the monthly slot reserved for this add must be released
    And the geo fallback must run against the submitted coordinate
    And the response must match the outcome the geo fallback produces

  Scenario: A monthly-cap rejection is answered before any reconcile
    Given BestTime rejects the create with its monthly venue cap message
    When the operator submits the venue name and address
    Then the response status must be 429
    And the response must carry BestTime's status and message
    And the account inventory must not be listed

  Scenario: A failing inventory listing degrades to the existing behavior
    Given BestTime rejects the create for the submitted venue
    And the BestTime account listing fails during the reconcile
    When the operator submits the venue name and address
    Then the listing failure must be logged with the submitted venue name
    And the geo fallback must still run
    And the response must be the one the operator would have received before this feature

  Scenario: A short generic name must not link to an unrelated inventory venue
    Given BestTime rejects the create for a submitted venue whose folded name is shorter than the containment floor
    And the account inventory holds a longer-named venue whose folded name contains the submitted one
    When the operator submits the venue name and address
    Then the two venues must not be linked
    And the response must not be a created outcome

  Scenario: Inventory listings are reused across a batch instead of re-read per row
    Given a batch-add job whose rows are rejected by the BestTime create
    And the inventory cache window is configured to a non-zero duration
    When the job processes every row inside that window
    Then the account inventory must be listed exactly once
    And each row must still be reconciled against the listed inventory

  Scenario: The inventory cache can be turned off
    Given the inventory cache window is configured to zero
    And two rejected adds are submitted in immediate succession
    When both reconciles run
    Then the account inventory must be listed once per reconcile

  Scenario: A batch row persisted without a forecast counts as a success
    Given a batch-add job containing a venue BestTime registers without a forecast
    When the job completes
    Then that row's outcome must be "created_without_forecast"
    And the job summary must count the row as a success
    And the job must not stop early because of that row

  Scenario: A recovered timeout takes its forecast flag from the inventory row
    Given the BestTime create times out for the submitted venue
    And the venue is present in the account inventory with "venue_forecasted" false
    When the timeout reconcile completes the add
    Then the persisted venue's forecast flag must be false
