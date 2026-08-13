@wip
Feature: Add venue as a pollable background job
  The synchronous add-venue call can legitimately run for minutes once BestTime
  pacing, the BestTime create timeout, Google enrichment, and Instagram
  discovery are all accounted for, and no fixed HTTP timeout can safely bound
  that. Operators must be able to start an add and poll for its outcome
  instead of holding one HTTP request open for the whole duration, without
  losing any of the existing outcome detail the synchronous endpoint reports.
  Operators must also be able to see recent add attempts and their outcomes —
  including a failure's reason — without having kept a poll open on them.

  Background:
    Given the monthly new venue quota is configured to 500
    And the manual add reserve is configured to 10
    And the current calendar month is "2026-05"
    And every add-venue request includes "venue_name", "venue_address", "venue_lat", and "venue_lng" sourced from a Google Places candidate

  Scenario: Starting an add job returns immediately with a job id
    Given the BestTime account inventory does not contain the submitted address
    And BestTime's response to the create call is delayed
    When the operator starts an add job for venue_name "Bar do Joao", venue_address "Rua das Flores 123, Recife - PE", venue_lat -8.05, and venue_lng -34.88
    Then the start response status must be 202
    And the start response body must include a "job_id"
    And the start response body must indicate status "running"
    And the start response must not wait for the BestTime create to resolve

  Scenario: Polling a job before it finishes reports it is still running
    Given the BestTime account inventory does not contain the submitted address
    And BestTime's response to the create call is delayed
    And the operator has started an add job for a new venue
    When the operator polls the job before BestTime responds
    Then the poll response status must be 200
    And the poll response body must indicate status "running"
    And the poll response body must not yet include a "result"

  Scenario: Polling a finished job reports the same outcome the synchronous add would have
    Given the BestTime account inventory does not contain the submitted address
    And the operator has started an add job for venue_name "Bar do Joao", venue_address "Rua das Flores 123, Recife - PE", venue_lat -8.05, and venue_lng -34.88
    When the job finishes and the operator polls it
    Then the poll response status must be 200
    And the poll response body must indicate status "done"
    And the poll response "http_status" must be 201
    And the poll response "result" must include the returned "venue_id"
    And the poll response "result" must include "source" equal to "besttime_new"
    And a metric "add_venue_by_address_total{result=\"created\"}" must be incremented

  Scenario: Polling a finished job that recovered from a BestTime timeout still reports the recovery
    Given a venue create that exceeds the BestTime timeout
    And the venue appears in the BestTime account inventory
    And the operator has started an add job for a new venue
    When the job finishes and the operator polls it
    Then the poll response "http_status" must be 201
    And the poll response "result" must indicate "recovered_from_timeout" is true

  Scenario: Polling a finished job that matched via geo fallback reports the same decision detail
    Given the BestTime account inventory already contains a venue matching the submitted coordinates but not the submitted address
    And the operator has started an add job for a new venue
    When the job finishes and the operator polls it
    Then the poll response "http_status" must be 200
    And the poll response "result" must indicate "matched_via_geo_fallback" with "newly_linked" true

  Scenario: Polling a finished job that failed reports BestTime's own message
    Given BestTime rejects a venue create with an explanatory message
    And the operator has started an add job for a new venue
    When the job finishes and the operator polls it
    Then the poll response "http_status" must be 502
    And the poll response "result" must include the BestTime rejection message

  Scenario: Polling an unknown job id
    When the operator polls a job id that was never started
    Then the poll response status must be 404

  Scenario: Two add jobs for different venues started back to back both complete
    Given the BestTime account inventory does not contain either submitted address
    When the operator starts an add job for venue_name "Bar do Joao", venue_address "Rua das Flores 123, Recife - PE", venue_lat -8.05, and venue_lng -34.88
    And the operator starts a second add job for venue_name "Laca Burguer", venue_address "Av. Conselheiro Aguiar 123, Recife - PE", venue_lat -8.119, and venue_lng -34.904
    Then both jobs must finish with status "done"
    And neither job's start response must indicate it was refused as already running

  Scenario: An add job started while a batch-add job is running still completes
    Given a batch-add job is already running
    When the operator starts an add job for a new venue
    Then the add job's start response status must be 202
    And the add job must finish with status "done"

  Scenario: A just-started job appears in the recent-jobs list as running
    Given the operator has started an add job for a new venue
    When the operator lists recent add jobs
    Then the recent-jobs response status must be 200
    And the recent-jobs list must include that job with status "running"

  Scenario: A finished created job appears in the recent-jobs list with its outcome
    Given the BestTime account inventory does not contain the submitted address
    And the operator has started an add job for venue_name "Bar do Joao", venue_address "Rua das Flores 123, Recife - PE", venue_lat -8.05, and venue_lng -34.88
    And the job has finished
    When the operator lists recent add jobs
    Then the recent-jobs list must include that job with status "done" and outcome "created"

  Scenario: A job BestTime rejected appears in the recent-jobs list with the rejection message as its reason
    Given BestTime rejects a venue create with an explanatory message
    And the operator has started an add job for a new venue
    And the job has finished
    When the operator lists recent add jobs
    Then the recent-jobs list must include that job showing the BestTime rejection message as its failure reason

  Scenario: A crashed job appears in the recent-jobs list as failed with its error
    Given the add job's runner crashes unexpectedly
    And the operator has started an add job for a new venue
    And the job has finished
    When the operator lists recent add jobs
    Then the recent-jobs list must include that job with status "failed" and its error text

  Scenario: The recent-jobs list never exceeds its cap
    Given more add jobs have been started than the recent-jobs cap allows
    When the operator lists recent add jobs
    Then the recent-jobs list must contain at most the capped number of entries
    And the recent-jobs list must show the most recently started jobs, not the oldest
