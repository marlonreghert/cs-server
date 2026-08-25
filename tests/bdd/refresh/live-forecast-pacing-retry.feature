Feature: Live-forecast refresh retries transient BestTime failures
  Over a live 44-hour prod observation window, 34.3% of POST /forecasts/live
  calls failed on an unretried 10-second client timeout and 19.1% failed on an
  HTTP 503/504 from BestTime, while only 5.8% of calls produced usable cached
  data. Neither failure mode was ever retried. The system must retry a
  transient timeout or 503/504 on a live-forecast call a bounded number of
  times before giving up, so some fraction of these previously-terminal
  failures become successes instead, without ever retrying a clean BestTime
  business rejection or letting one venue's exhausted retries block the rest
  of the refresh cycle.

  Background:
    Given the live-forecast retry budget is 2 attempts

  Scenario: Cache the live forecast after a transient timeout is retried
    Given venue "ven-timeout-then-ok" exists
    And BestTime times out on the first live-forecast call for "ven-timeout-then-ok"
    And BestTime then answers "ven-timeout-then-ok" with status "OK" and busyness available
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-timeout-then-ok" is cached
    And exactly 2 live-forecast calls were made for "ven-timeout-then-ok"

  Scenario Outline: Cache the live forecast after a transient BestTime error is retried
    Given venue "ven-<code>-then-ok" exists
    And BestTime answers the first live-forecast call for "ven-<code>-then-ok" with HTTP <code>
    And BestTime then answers "ven-<code>-then-ok" with status "OK" and busyness available
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-<code>-then-ok" is cached
    And exactly 2 live-forecast calls were made for "ven-<code>-then-ok"

    Examples:
      | code |
      | 503  |
      | 504  |

  Scenario: Record an error and continue to the next venue when retries are exhausted
    Given venue "ven-always-timeout" exists
    And venue "ven-healthy" exists
    And BestTime times out on every live-forecast call for "ven-always-timeout"
    And BestTime answers "ven-healthy" with status "OK" and busyness available
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-always-timeout" is not cached
    And a live-forecast error is recorded for "ven-always-timeout"
    And exactly 2 live-forecast calls were made for "ven-always-timeout"
    And the live forecast for "ven-healthy" is cached

  Scenario: A BestTime business rejection is never retried
    # Rejection-streak gating (plans/260825_live-forecast-rejection-threshold.md)
    # is orthogonal to this feature's concern (retry, not cache-wipe timing) —
    # pin the threshold to 1 so this scenario keeps proving the un-retried,
    # single-call behavior, not the separate consecutive-rejection count.
    Given the live-forecast rejection streak threshold is 1 consecutive rejections
    And venue "ven-rejected" exists
    And the live forecast for "ven-rejected" is already cached
    And BestTime answers "ven-rejected" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-rejected" is not cached
    And exactly 1 live-forecast call was made for "ven-rejected"
