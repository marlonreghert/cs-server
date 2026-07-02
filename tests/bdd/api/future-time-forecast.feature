@wip
Feature: Future-time weekly forecast for /v1/venues/nearby
  As the venue-serving API, given a caller that asks for a future day,
  the nearby-venues response must carry that day's weekly forecast so
  downstream consumers can serve future-time venue requests, while callers
  that omit the parameter keep today's behavior unchanged.

  Background:
    Given a venue "central-recife-bar" exists near the requested location
    And the venue has a distinct weekly forecast stored for every day of the week

  Scenario: Omitting target_day_offset returns today's forecast
    When I request nearby venues without a target_day_offset
    Then the venue's weekly_forecast day_int equals today's day index
    And the venue's weekly_forecast day_raw equals today's stored forecast

  Scenario: target_day_offset of zero matches omitting it
    When I request nearby venues with target_day_offset 0
    Then the venue's weekly_forecast day_int equals today's day index

  Scenario: A future target_day_offset returns that day's forecast
    Given today's day index is known
    When I request nearby venues with target_day_offset 3
    Then the venue's weekly_forecast day_int equals today's day index shifted by 3 modulo 7
    And the venue's weekly_forecast day_raw equals the stored forecast for that day

  Scenario: An offset beyond the week wraps around
    When I request nearby venues with target_day_offset 8
    Then the venue's weekly_forecast day_int equals today's day index shifted by 1 modulo 7

  Scenario: A negative target_day_offset is rejected
    When I request nearby venues with target_day_offset -1
    Then the response status is 422

  Scenario: The response shape is unchanged for future days
    When I request nearby venues with target_day_offset 3
    Then the venue's weekly_forecast is a single day object, not a list
    And all other venue fields are present as in a normal nearby response
