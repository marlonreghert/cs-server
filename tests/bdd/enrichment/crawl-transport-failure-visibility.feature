@wip
Feature: Crawl transport failure visibility
  A crawl that fails at the HTTP layer must be reported as a failure, exactly as
  a dataset error item already is. A timed-out call returns no posts, and that
  must never be recorded as an account with nothing to say.

  Background:
    Given a crawl target "somebar" that has never been seeded

  # ── transport failures are failures ────────────────────────────────────────

  Scenario: Report a timed-out fetch as a failure, not as an empty account
    Given the Apify call for "somebar" posts times out
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as a failure
    And the outcome is not recorded as empty

  Scenario: Report an HTTP error from Apify as a failure
    Given the Apify call for "somebar" posts fails with an HTTP error
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as a failure

  Scenario: Report a connection error as a failure
    Given the Apify call for "somebar" posts fails to connect
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as a failure

  Scenario: Increment the failure counter when a fetch times out
    Given the consecutive failure count for "somebar" is 1
    And the Apify call for "somebar" posts times out
    When the crawl runs for "somebar"
    Then the consecutive failure count for "somebar" is 2

  Scenario: Leave the cursor unadvanced when a fetch times out
    Given the Apify call for "somebar" posts times out
    When the crawl runs for "somebar"
    Then the posts cursor for "somebar" is still unset

  Scenario: Record the failure kind on the target when a fetch times out
    Given the Apify call for "somebar" posts times out
    When the crawl runs for "somebar"
    And an operator reads the crawl targets from the admin API
    Then "somebar" reports its last failure kind
    And "somebar" reports when that failure was observed

  # ── a genuine empty is still a genuine empty ───────────────────────────────

  Scenario: Keep treating a genuinely empty dataset as empty
    Given Apify returns no posts and no error item for "somebar"
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as empty
    And the consecutive failure count for "somebar" is 0

  Scenario: Keep the dataset error-item classification behaving as it does today
    Given Apify returns an error item for "somebar" posts with code "not_found"
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as a handle-not-found failure

  Scenario: Keep classifying a blocked scrape as blocked
    Given Apify returns an error item for "somebar" posts with code "no_items"
    And that error item reports 11 blocked requests
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as blocked

  # ── the log must name the handle ───────────────────────────────────────────

  Scenario: Name the handle in the log line for a timed-out fetch
    Given the Apify call for "somebar" posts times out
    When the crawl runs for "somebar"
    Then the timeout is logged naming the handle "somebar"
    And the timeout is logged naming the stream "posts"

  # ── a stream that has never worked is louder than one that failed today ────

  Scenario: Surface a stream that has never successfully seeded
    Given "somebar" has run before and its posts cursor is still unset
    When an operator reads the crawl targets from the admin API
    Then "somebar" reports that its posts stream has never seeded

  Scenario: Do not report a target that has never run as never-seeded
    Given "somebar" has never run
    When an operator reads the crawl targets from the admin API
    Then "somebar" does not report that its posts stream has never seeded

  Scenario: Stop reporting never-seeded once the stream succeeds
    Given "somebar" has run before and its posts cursor is still unset
    And Apify returns 5 posts for "somebar"
    When the crawl runs for "somebar"
    And an operator reads the crawl targets from the admin API
    Then "somebar" does not report that its posts stream has never seeded
