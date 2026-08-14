Feature: Venue-add job tracking on RDS, not Redis
  As the VibeSense platform
  Single-add and batch-add venue jobs (AddVenueJobService, BatchAddService) today
  persist only to Redis with a TTL: a crash inside the owning process is handled
  in-process, but if the whole process is replaced (container restart, redeploy)
  mid-job, the last-written Redis document is left however it was — typically
  "running" — forever, with no reconciliation and no durable history once the
  TTL lapses. Job tracking must move onto the RDS system of record
  (admin.venue_add_job_run), survive indefinitely for audit purposes, and any
  row still "running" from a process that no longer exists must be
  deterministically reconciled to "interrupted" on the next startup — without
  ever resuming or re-running the underlying add.

  # Plan: plans/260814_venue-add-job-rds-tracking.md.
  # Incident: 2026-08-14, job_id=2d07cf4450ea466ea6a7b8b95fc11211 stuck at
  # "running" after an unrelated deploy recycled the cs-server container
  # mid-batch.

  Background:
    Given the RDS system-of-record is enabled
    And an empty RDS

  Scenario: A single-add job's terminal result is readable from the RDS store
    Given a single-add job is started for "Boate Fantasy" at "Coroa do Meio, Aracaju - SE"
    When the add completes with a "created" outcome
    Then polling the job returns status "done" with the "created" result
    And no Redis key was written for this job

  Scenario: A batch-add job's per-row results and summary are readable from the RDS store
    Given a batch-add job is started for 3 venues
    When all 3 rows finish processing
    Then polling the job returns status "done" with a summary count of 3
    And the job's results list has 3 entries
    And no Redis key was written for this job

  Scenario: Recent jobs are listed newest-first directly from the RDS store
    Given 3 single-add jobs have completed, started at different times
    When the recent-jobs list is requested
    Then the jobs are returned newest-started-first
    And each "done" job is annotated with its outcome

  Scenario: A job left running by a prior process is reconciled to interrupted on startup
    Given a batch-add job row exists with status "running" from a previous process
    When cs-server starts up
    Then that job's status becomes "interrupted"
    And its stopped_reason explains the process restarted while it was running
    And its finished_at is set
    And the underlying add is not re-run

  Scenario: A startup with no running jobs reconciles nothing
    Given no job row has status "running"
    When cs-server starts up
    Then zero jobs are reconciled
    And startup completes without delay

  Scenario: An in-process crash during a run still reaches failed, unchanged from today
    Given a single-add job is started for "Kingston 085" at "Praia de Iracema, Fortaleza - CE"
    When the add handler raises an exception before completing
    Then polling the job returns status "failed" with the error message
    And the process is not restarted

  Scenario: A persistence failure during a run does not crash the job
    Given a batch-add job is started for 2 venues
    When the RDS store is unavailable for one row's save
    Then the batch-add job continues processing the remaining rows
    And a job-persistence-failure metric is recorded
