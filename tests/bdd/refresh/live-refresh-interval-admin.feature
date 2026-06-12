Feature: Admin-tunable live forecast refresh interval

  The live_forecast_refresh interval is read from the Redis admin config key
  "admin_config:live_refresh_minutes" by a lightweight watcher job and applied
  to the running scheduler without a restart. Absent or invalid values fall
  back to the settings default and can never stall the refresh.

  Background:
    Given the scheduler is running with live_forecast_refresh at the settings
      default interval of 5 minutes

  Scenario: Setting the admin key reschedules the live refresh
    When the admin config key "live_refresh_minutes" is set to 15
    And the interval watcher runs
    Then the live_forecast_refresh job must be rescheduled to every 15 minutes
    And the live refresh interval gauge must report 15
    And an info log must record the change from 5 to 15

  Scenario: Deleting the admin key reverts to the settings default
    Given the admin config key "live_refresh_minutes" is set to 15 and applied
    When the admin config key "live_refresh_minutes" is deleted
    And the interval watcher runs
    Then the live_forecast_refresh job must be rescheduled to every 5 minutes
    And the live refresh interval gauge must report 5

  Scenario: An unchanged value does not reschedule the job
    Given the admin config key "live_refresh_minutes" is set to 15 and applied
    When the interval watcher runs again with the key still at 15
    Then the live_forecast_refresh job must not be rescheduled

  Scenario Outline: Invalid values keep the current interval
    When the admin config key "live_refresh_minutes" is set to <value>
    And the interval watcher runs
    Then the live_forecast_refresh job must keep its current interval
    And a warning log must record the rejected value

    Examples:
      | value   |
      | 0       |
      | -5      |
      | 121     |
      | "fast"  |

  Scenario: A Redis read failure keeps the current schedule
    Given the admin config Redis read raises an error
    When the interval watcher runs
    Then the live_forecast_refresh job must keep its current interval
    And the watcher error must be counted in the background job metrics
    And the watcher must keep running on its next cycle
