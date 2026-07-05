Feature: Server-side batch venue-add runs a list and summarizes every outcome
  As the venue platform
  I must add a whole curated list in one server-side job — running each row
  through the same add flow as the single endpoint and persisting a pollable
  summary — so a bulk campaign costs one request plus polling, deterministically.

  Scenario: A batch job adds every row and reports a per-outcome summary
    Given the current calendar month is "2026-05"
    And BestTime accepts every add and returns a created venue
    When the operator submits a batch of 2 venues with coordinates
    Then the batch endpoint returns 202 with a job id
    And polling the job eventually reports status "done"
    And the job summary reports 2 created and 2 processed

  Scenario: Polling an unknown batch job returns not found
    When the operator polls a batch job id that does not exist
    Then the batch poll returns 404
