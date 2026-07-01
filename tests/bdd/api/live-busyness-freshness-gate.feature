@wip
Feature: Suppress stale live busyness at serve time
  The nearby-venues API must not present an outdated live busyness value as if it
  were current. A cached live forecast is refreshed periodically, but a BestTime
  outage or a stalled refresh job can leave the last value in the cache
  indefinitely. When the underlying live payload is older than a configurable
  freshness window, the API must omit the live busyness value so the downstream
  serving layer falls back to the forecast estimate instead of a stale number.
  Freshness is derived from the live payload's own venue_current_gmttime; the
  window defaults to 1440 minutes and is overridable at runtime via the admin
  config key "live_freshness_max_age_minutes" without a redeploy.

  Background:
    Given the live freshness window default is 1440 minutes
    And the current time is "2026-07-01 12:00:00" UTC

  Scenario: Serve a fresh live busyness value unchanged
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 30 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response must include "venue_live_busyness" from the live forecast
    And the serve metric outcome "served" must be incremented for that venue

  Scenario: Suppress a live busyness value older than the freshness window
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 1500 minutes old
    And the venue has a cached weekly forecast for the current day
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null
    And the venue response must still include the "weekly_forecast"
    And the serve metric outcome "suppressed_stale" must be incremented for that venue

  Scenario: Treat a payload exactly at the window boundary as stale
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is exactly 1440 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null

  Scenario: Suppress a live value whose timestamp cannot be parsed
    Given a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is not a parseable timestamp
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null
    And the serve metric outcome "suppressed_unparseable" must be incremented for that venue

  Scenario: Apply an admin-overridden freshness window within the request
    Given the admin config "live_freshness_max_age_minutes" is set to 15
    And a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 30 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response "venue_live_busyness" must be null

  Scenario: Fall back to the default window when the admin override is invalid
    Given the admin config "live_freshness_max_age_minutes" is set to "not-a-number"
    And a venue has a cached live forecast that is available
    And the live forecast venue_current_gmttime is 30 minutes old
    When the nearby-venues endpoint is queried in minified mode
    Then the venue response must include "venue_live_busyness" from the live forecast
