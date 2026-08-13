@wip
Feature: Dormant versus broken targets
  An account that answers but has posted nothing inside the lookback window is
  dormant, not broken. It must be reported as such, must never count as a
  failure, and must never be disabled — while a genuinely blocked or
  non-existent handle keeps being reported exactly as it is today.

  Background:
    Given a crawl target "quietbar" that has never been seeded

  Scenario: Report a target as dormant when its account answers but has nothing in the window
    Given Apify returns an error item for "quietbar" posts with code "no_items"
    And that error item reports no blocked requests
    When the crawl runs for "quietbar"
    Then "quietbar" is reported as dormant

  Scenario: Do not increment the failure counter for a dormant target
    Given the consecutive failure count for "quietbar" is 0
    And Apify returns an error item for "quietbar" posts with code "no_items"
    And that error item reports no blocked requests
    When the crawl runs for "quietbar"
    Then the consecutive failure count for "quietbar" is 0

  Scenario: Do not disable a dormant target
    Given Apify returns an error item for "quietbar" posts with code "no_items"
    And that error item reports no blocked requests
    When the crawl runs for "quietbar"
    Then "quietbar" is still enabled

  Scenario: Keep reporting a blocked target as blocked
    Given Apify returns an error item for "quietbar" posts with code "no_items"
    And that error item reports 11 blocked requests
    When the crawl runs for "quietbar"
    Then the posts stream outcome is recorded as blocked
    And "quietbar" is not reported as dormant

  Scenario: Keep reporting a non-existent handle as permanently failed
    Given Apify returns an error item for "quietbar" posts with code "not_found"
    When the crawl runs for "quietbar"
    Then the posts stream outcome is recorded as a handle-not-found failure
    And "quietbar" is not reported as dormant

  Scenario: Surface dormancy alongside never-seeded on the admin read model
    Given Apify returns an error item for "quietbar" posts with code "no_items"
    And that error item reports no blocked requests
    When the crawl runs for "quietbar"
    And an operator reads the crawl targets from the admin API
    Then "quietbar" reports that its posts stream has never seeded
    And "quietbar" reports that its account is dormant

  Scenario: Use the seed lookback on a target whose cursor is unset
    Given the seed lookback is 12 months
    And the steady-state lookback is 3 months
    When the crawl runs for "quietbar"
    Then Apify is asked for posts newer than 12 months

  Scenario: Use the steady-state lookback once the cursor is set
    Given the seed lookback is 12 months
    And the steady-state lookback is 3 months
    And the posts cursor for "quietbar" is already set
    When the crawl runs for "quietbar"
    Then Apify is not asked for posts newer than 12 months

  Scenario: Honour a per-target seed lookback override
    Given the seed lookback is 12 months
    And "quietbar" overrides its seed lookback to 6 months
    When the crawl runs for "quietbar"
    Then Apify is asked for posts newer than 6 months

  Scenario: Stop reporting dormancy once the target produces a post
    Given "quietbar" is reported as dormant
    And Apify returns 3 posts for "quietbar"
    When the crawl runs for "quietbar"
    Then "quietbar" is not reported as dormant
