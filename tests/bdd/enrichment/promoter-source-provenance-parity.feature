@wip
Feature: Promoter source provenance parity
  Every stored source row records when its post was posted and what medium it
  was, whichever pipeline read it. A missing value is stored as absent, never
  replaced by the time we happened to crawl it.

  Background:
    Given a promoter crawl target "roundupaccount"

  Scenario: Record a promoter post's upload time on every event it yields
    Given a promoter post uploaded at 2026-08-12 15:26 that yields 3 events
    When the promoter crawl runs
    Then all 3 stored sources record an upload time of 2026-08-12 15:26

  Scenario: Record a promoter post's media type on every event it yields
    Given a promoter post whose media type is "Video" that yields 3 events
    When the promoter crawl runs
    Then all 3 stored sources record their media type as "Video"

  Scenario: Give every event from one post the same provenance values
    Given a promoter post uploaded at 2026-08-12 15:26 whose media type is "Sidecar"
    When the promoter crawl runs
    Then every stored source of that post carries the same upload time
    And every stored source of that post carries the same media type

  Scenario: Store no upload time when the post carries none
    Given a promoter post with no timestamp
    When the promoter crawl runs
    Then the stored sources record no upload time

  Scenario: Store no upload time when the post's timestamp is unparseable
    Given a promoter post whose timestamp is "not-a-date"
    When the promoter crawl runs
    Then the stored sources record no upload time
    And a warning is logged that the timestamp could not be read

  Scenario: Never substitute the crawl time for a missing upload time
    Given a promoter post with no timestamp
    When the promoter crawl runs
    Then the stored sources record no upload time
    And the stored sources' upload time does not equal their first seen time

  Scenario: Keep a post's media type separate from its item type
    Given a promoter post whose media type is "Video" announcing one event
    When the promoter crawl runs
    Then the stored source records its media type as "Video"
    And the stored item records its post type as "event"

  Scenario: Keep recording both fields on the venue path
    Given an archived venue post uploaded at 2026-08-12 15:26 whose media type is "Image"
    When the post is extracted
    Then the stored source records an upload time of 2026-08-12 15:26
    And the stored source records its media type as "Image"
