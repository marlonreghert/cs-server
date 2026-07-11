Feature: Attach the previous business day's weekly forecast for nighttime serving
  Readers of /v1/venues/nearby must be able to resolve busyness correctly
  between 00:00 and 05:59 local time. Under the BestTime day_raw convention,
  index 0 of a day's array is 6 AM of that day and indices 18-23 are the
  following calendar morning, so the value for an early-morning moment lives
  in the previous day's array. The API must therefore attach the previous
  business day's weekly entry alongside the requested day's entry, so the
  reader can select by the 6 AM anchor. The existing weekly_forecast field
  must not change in shape, day selection, or values.

  Background:
    Given the weekly forecast prev-day attachment flag is enabled
    And a servable venue "club-recife" exists in the Redis geo index

  Scenario: Nearby venues carry the previous business day's weekly entry
    Given the current Recife weekday is Saturday
    And a weekly forecast for day_int 5 with a distinct day_raw array is stored for "club-recife"
    And a weekly forecast for day_int 4 with a distinct day_raw array is stored for "club-recife"
    When a client requests nearby venues around "club-recife" without a day offset
    Then the served venue's "weekly_forecast" must have day_int 5
    And the served venue's "weekly_forecast_prev" must have day_int 4
    And the served venue's "weekly_forecast_prev" day_raw must equal the stored day_int 4 array verbatim

  Scenario: A requested day offset shifts both attached entries together
    Given the current Recife weekday is Saturday
    And weekly forecasts are stored for day_int 5 and day_int 6 for "club-recife"
    When a client requests nearby venues around "club-recife" with target_day_offset 1
    Then the served venue's "weekly_forecast" must have day_int 6
    And the served venue's "weekly_forecast_prev" must have day_int 5

  Scenario: The previous business day wraps across the week boundary
    Given the current Recife weekday is Monday
    And weekly forecasts are stored for day_int 0 and day_int 6 for "club-recife"
    When a client requests nearby venues around "club-recife" without a day offset
    Then the served venue's "weekly_forecast" must have day_int 0
    And the served venue's "weekly_forecast_prev" must have day_int 6

  Scenario: A missing previous day degrades gracefully without affecting the venue
    Given the current Recife weekday is Saturday
    And only a weekly forecast for day_int 5 is stored for "club-recife"
    When a client requests nearby venues around "club-recife" without a day offset
    Then the served venue's "weekly_forecast" must have day_int 5
    And the served venue's "weekly_forecast_prev" must be null
    And the venue must otherwise be served with its full field set

  Scenario: Disabling the flag restores the legacy response shape exactly
    Given the weekly forecast prev-day attachment flag is disabled
    And weekly forecasts are stored for day_int 5 and day_int 4 for "club-recife"
    When a client requests nearby venues around "club-recife" without a day offset
    Then the served venue's "weekly_forecast" must have day_int 5
    And the served venue must not carry a "weekly_forecast_prev" value

  Scenario: Verbose mode carries the same previous-day attachment
    Given the current Recife weekday is Saturday
    And weekly forecasts are stored for day_int 5 and day_int 4 for "club-recife"
    When a client requests nearby venues around "club-recife" in verbose mode
    Then the served venue's "weekly_forecast_prev" must have day_int 4

  Scenario: Disabling the flag changes nothing but the removed key (minified)
    Given the current Recife weekday is Saturday
    And weekly forecasts are stored for day_int 5 and day_int 4 for "club-recife"
    When a client requests nearby venues around "club-recife" with the flag enabled and then disabled
    Then the disabled response equals the enabled response with "weekly_forecast_prev" removed

  Scenario: Disabling the flag changes nothing but the removed key (verbose)
    Given the current Recife weekday is Saturday
    And weekly forecasts are stored for day_int 5 and day_int 4 for "club-recife"
    When a client requests nearby venues around "club-recife" in verbose mode with the flag enabled and then disabled
    Then the disabled response equals the enabled response with "weekly_forecast_prev" removed
