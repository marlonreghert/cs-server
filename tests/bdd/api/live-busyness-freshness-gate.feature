Feature: Derive the live-busyness stale window from the refresh cadence
  The nearby-venues API must not present outdated live busyness as current, but it
  must also not flag a venue stale merely because it is between refresh cycles. The
  freshness window is therefore DERIVED from the live refresh cadence: a cached
  value is stale once older than 2x the effective refresh interval
  (admin_config:live_refresh_minutes, else the settings default of 5 minutes),
  floored at 5 minutes. A slower refresh automatically widens the window, so a
  venue refreshed on schedule always serves as live and only a venue that has
  missed a full cycle (a genuinely stalled refresh) is suppressed to forecast.

  Background:
    Given the current time is "2026-07-01 12:00:00" UTC

  Scenario: Default cadence — a 5-minute refresh yields a 10-minute window, serve within it
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 8 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response must include "venue_live_busyness" from the live forecast
    And the serve metric outcome "served" must be incremented for that venue

  Scenario: Default cadence — a value past the 10-minute window is suppressed
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 12 minutes old
    And the venue has a cached weekly forecast for the current day
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null
    And the venue response must still include the "weekly_forecast"
    And the serve metric outcome "suppressed_stale" must be incremented for that venue

  Scenario: A slower refresh widens the window so mid-cycle values stay live
    Given the live refresh interval is set to 15 minutes
    And a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 25 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response must include "venue_live_busyness" from the live forecast
    And the serve metric outcome "served" must be incremented for that venue

  Scenario: A value beyond the widened window is genuinely stale
    Given the live refresh interval is set to 15 minutes
    And a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 40 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null
    And the serve metric outcome "suppressed_stale" must be incremented for that venue

  Scenario: The window is floored so a very short cadence tolerates clock skew
    Given the live refresh interval is set to 1 minutes
    And a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 4 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response must include "venue_live_busyness" from the live forecast

  Scenario: Boundary — a value exactly at the window is stale
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is exactly 10 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null

  Scenario: A value with an unparseable timestamp is suppressed
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is not a parseable timestamp
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null
    And the serve metric outcome "suppressed_unparseable" must be incremented for that venue
