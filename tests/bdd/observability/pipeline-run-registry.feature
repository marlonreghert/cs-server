Feature: Give every pipeline run an identity an operator can select
  As the platform operator
  I must be able to tell one run of a pipeline from another, in metrics and in
  logs, without the run identity growing the monitoring stack without bound,
  so a dashboard can show me a specific run instead of only an aggregate.

  Background:
    Given the pipeline run registry is enabled

  # 1 — Every run is registered
  Scenario: Register a scheduled run
    When the scheduled pipeline "live_forecast" runs to completion
    Then the run is registered for the pipeline "live_forecast"
    And the run is registered with the status "success"
    And the run carries a run id

  Scenario: Register an admin-triggered run the same way
    When the pipeline "instagram" is triggered from the admin panel
    Then the run is registered for the pipeline "instagram"
    And the run is indistinguishable from a scheduled run

  Scenario: Report a failed run without swallowing the failure
    When the scheduled pipeline "google_places" raises during its run
    Then the run is registered with the status "error"
    And the failure still reaches the caller

  Scenario: Show a run under exactly one status at a time
    When the scheduled pipeline "live_forecast" runs to completion
    Then the pipeline has exactly one registered entry for that run

  # 2 — Ordering
  Scenario: Report when each run started
    When the scheduled pipeline "live_forecast" runs to completion
    Then the registered run reports its start time

  Scenario: Mint run ids that sort chronologically
    When three runs of "live_forecast" happen in sequence
    Then sorting the run ids as text puts them in the order they ran

  # 3 — Bounded by construction
  Scenario: Keep only the most recent runs of a pipeline
    Given the registry keeps 3 runs per pipeline
    When 6 runs of "live_forecast" complete
    Then only the 3 most recent runs remain registered
    And the oldest runs are no longer registered

  Scenario: Never let one pipeline evict another's runs
    Given the registry keeps 3 runs per pipeline
    When 6 runs of "live_forecast" complete
    And 1 run of "weekly_forecast" completes
    Then the "weekly_forecast" run is still registered

  # 4 — Log correlation
  Scenario: Stamp every log line emitted during a run
    When the scheduled pipeline "live_forecast" logs during its run
    Then those log lines carry the run id

  Scenario: Leave a pipeline that stamps its own id alone
    When a pipeline logs a line that already carries its run id
    Then the run id appears exactly once in that line

  Scenario: Emit no run id outside a run
    When a log line is emitted outside any pipeline run
    Then that line carries no run id

  # 5 — Instrumentation must never break a pipeline
  Scenario: Continue the run when the registry fails
    Given registering a run fails
    When the scheduled pipeline "live_forecast" runs to completion
    Then the pipeline still completes successfully

  Scenario: Keep working with the registry disabled
    Given the pipeline run registry is disabled
    When the scheduled pipeline "live_forecast" runs to completion
    Then the pipeline still completes successfully
    And no run is registered
