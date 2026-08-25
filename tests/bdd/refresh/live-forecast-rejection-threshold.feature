Feature: Live-forecast refresh wipes cache after N consecutive rejections
  Over a live 46.5-hour prod observation window (20 refresh cycles), only
  3.3% of venues ever rejected by BestTime were rejected in exactly one
  cycle; 94.3% went on to a run of 2 or more consecutive rejections and
  82.3% went on to a run of 3 or more. Today, a single clean BestTime
  rejection (status != "OK") immediately deletes any previously-cached good
  live forecast for that venue. The system must instead track consecutive
  rejections per venue and only wipe the cached forecast once a configurable
  threshold of back-to-back rejections is reached, so an isolated,
  likely-transient rejection no longer discards good data that a durably
  unforecastable venue would have lost anyway one cycle later. The sibling
  "venue is currently closed" signal (status "OK" but no live busyness
  available) is a different, unambiguous case and must keep wiping the cache
  immediately every time, unaffected by this threshold.

  Background:
    Given the live-forecast rejection streak threshold is 2 consecutive rejections

  Scenario: A single business rejection does not wipe an already-cached forecast
    Given venue "ven-isolated-reject" exists
    And the live forecast for "ven-isolated-reject" is already cached
    And BestTime answers "ven-isolated-reject" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-isolated-reject" is cached
    And a rejection-below-threshold outcome is recorded for "ven-isolated-reject"

  Scenario: The 2nd consecutive rejection wipes the cached forecast
    Given venue "ven-durable-reject" exists
    And the live forecast for "ven-durable-reject" is already cached
    And BestTime answers "ven-durable-reject" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-durable-reject" is cached
    Given BestTime answers "ven-durable-reject" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-durable-reject" is not cached
    And a cache-deleted outcome is recorded for "ven-durable-reject"

  Scenario: A successful forecast resets an in-progress rejection streak
    Given venue "ven-recovers" exists
    And the live forecast for "ven-recovers" is already cached
    And BestTime answers "ven-recovers" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-recovers" is cached
    Given BestTime answers "ven-recovers" with status "OK" and busyness available
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-recovers" is cached
    Given BestTime answers "ven-recovers" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-recovers" is cached

  Scenario: A closed venue still wipes the cached forecast immediately, unaffected by the threshold
    Given venue "ven-closed" exists
    And the live forecast for "ven-closed" is already cached
    And BestTime answers "ven-closed" with status "OK" and busyness not available
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-closed" is not cached

  Scenario: A transport failure between two rejections does not reset progress toward the threshold
    Given venue "ven-flaky-transport" exists
    And the live forecast for "ven-flaky-transport" is already cached
    And BestTime answers "ven-flaky-transport" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-flaky-transport" is cached
    Given BestTime times out on the first live-forecast call for "ven-flaky-transport"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-flaky-transport" is cached
    Given BestTime answers "ven-flaky-transport" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-flaky-transport" is not cached

  Scenario: A threshold of 1 reproduces the legacy immediate-delete behavior
    Given the live-forecast rejection streak threshold is 1 consecutive rejections
    And venue "ven-legacy" exists
    And the live forecast for "ven-legacy" is already cached
    And BestTime answers "ven-legacy" with status "Error"
    When the live forecast refresh runs against the scripted BestTime transport
    Then the live forecast for "ven-legacy" is not cached
