@wip
Feature: Crawl error visibility
  A crawl that failed must be reported as a failure, not as an account with
  nothing to say. Apify returns a structured error item in an otherwise empty
  dataset; the crawler must read it, tell a permanent handle problem apart from
  a transient block, refuse to bill it, and let it reach the failure counter.

  Background:
    Given a crawl target "somebar" that has never been seeded

  Scenario: Report a blocked scrape as a failure, not as an empty account
    Given Apify returns an error item for "somebar" posts with code "no_items"
    And that error item reports 11 blocked requests
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as blocked
    And the outcome is not recorded as empty
    And the run logs the Apify error code and the blocked-request count

  Scenario: Report a non-existent handle as permanently failed
    Given Apify returns an error item for "somebar" posts with code "not_found"
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as a handle-not-found failure
    And the target records why it will no longer be retried on a schedule
    And a warning is logged naming the handle

  Scenario: Keep treating a genuinely empty stream as empty
    Given Apify returns an error item for "somebar" reels with code "no_items"
    And that error item reports no blocked requests
    When the crawl runs for "somebar"
    Then the reels stream outcome is recorded as empty
    And the consecutive failure count for "somebar" is 0

  Scenario: Treat an unrecognised Apify error code as a transient failure
    Given Apify returns an error item for "somebar" posts with code "some_new_code"
    When the crawl runs for "somebar"
    Then the posts stream outcome is recorded as a failure
    And the outcome is not recorded as empty

  Scenario: Increment the failure counter when a fetch is blocked
    Given the consecutive failure count for "somebar" is 2
    And Apify returns an error item for "somebar" posts with code "no_items"
    And that error item reports 11 blocked requests
    When the crawl runs for "somebar"
    Then the consecutive failure count for "somebar" is 3

  Scenario: Reset the failure counter after a stream genuinely succeeds
    Given the consecutive failure count for "somebar" is 3
    And Apify returns 5 posts for "somebar"
    When the crawl runs for "somebar"
    Then the consecutive failure count for "somebar" is 0

  Scenario: Refuse to bill an error item as a crawl result
    Given the monthly result budget has 100 results remaining
    And Apify returns an error item for "somebar" posts with code "no_items"
    When the crawl runs for "somebar"
    Then the monthly result budget still has 100 results remaining
    And the target's last run records 0 results
    And the target's last run cost is 0

  Scenario: Count only the real posts when a dataset mixes an error item with posts
    Given Apify returns 3 posts and 1 error item for "somebar" posts
    When the crawl runs for "somebar"
    Then the target's last run records 3 results
    And 3 posts are archived for "somebar"

  Scenario: Leave the cursor unadvanced when a stream fails
    Given Apify returns an error item for "somebar" posts with code "no_items"
    And that error item reports 11 blocked requests
    When the crawl runs for "somebar"
    Then the posts cursor for "somebar" is still unset

  Scenario: Surface the last failure kind on the admin crawl-target read model
    Given Apify returns an error item for "somebar" posts with code "not_found"
    When the crawl runs for "somebar"
    And an operator reads the crawl targets from the admin API
    Then "somebar" reports its last failure kind
    And "somebar" reports when that failure was observed

  Scenario: Persist a source post's media type
    Given Apify returns 1 post for "somebar" whose media type is "Video"
    When the crawl runs for "somebar"
    And an event is extracted from that post
    Then the stored source records its media type as "Video"

  Scenario: Record that a post was truncated by the per-post event cap
    Given the per-post event cap is 20
    And a post for "somebar" from which the model returns 25 events
    When the post is extracted
    Then 20 events are stored for that post
    And the stored sources record that the post was truncated by the cap

  Scenario: Do not mark a post as cap-truncated when it fits
    Given the per-post event cap is 20
    And a post for "somebar" from which the model returns 4 events
    When the post is extracted
    Then 4 events are stored for that post
    And the stored sources do not record a cap truncation
