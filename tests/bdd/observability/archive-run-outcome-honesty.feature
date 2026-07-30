@wip
Feature: Report what an archive run actually did
  As the venue platform operator
  I must be able to tell a clean archive run from one that lost most of its
  venues, and I must be told why a venue produced nothing, so that a run which
  delivered almost nothing cannot be reported as a success and the freshness
  signal I would alert on stays trustworthy.

  Background:
    Given the media archive is enabled with a configured bucket
    And the archive source is the Apify Google Maps extractor

  Scenario: A run where every venue is archived is a success
    Given a venue the extractor can find with 3 photos
    When the photo archive job runs using the Apify source
    Then the run status must be "success"
    And the last-success timestamp must advance

  Scenario: A run that loses venues to timeouts is partial, not success
    Given a venue the extractor can find with 3 photos
    And a venue whose fetch times out
    When the photo archive job runs using the Apify source
    Then the run status must be "partial"
    And the run status must not be "success"
    And the last-success timestamp must not advance

  Scenario: A run where every venue fails is partial, not success
    Given a venue whose fetch fails
    When the photo archive job runs using the Apify source
    Then the run status must be "partial"
    And the last-success timestamp must not advance

  Scenario: A run stopped by credit exhaustion is an error
    Given a venue the extractor can find with 3 photos
    And the Apify account has no credits left
    When the photo archive job runs using the Apify source
    Then the run status must be "error"
    And the last-success timestamp must not advance

  Scenario: A run that skips everything already archived is still a success
    Given every selected venue was already archived by the previous run
    When the photo archive job runs using the Apify source
    Then the run status must be "success"
    And no venue must be reported as a failure

  Scenario: A venue with no search query is reported as no_query
    Given a venue the source cannot address
    When the photo archive job runs using the Apify source
    Then the venue outcome must be reported as "no_query"
    And the venue outcome must not be reported as "no_result"
    And the run summary must count 1 no_query

  Scenario: A venue the source cannot find is reported as no_result
    Given a venue the extractor cannot find
    When the photo archive job runs using the Apify source
    Then the venue outcome must be reported as "no_result"
    And the venue outcome must not be reported as "no_query"
    And the run summary must count 1 no_result

  Scenario Outline: The retired no_match label must never be emitted again
    Given <situation>
    When the photo archive job runs using the Apify source
    Then the venue outcome must not be reported as "no_match"

    Examples:
      | situation                                |
      | a venue the source cannot address        |
      | a venue the extractor cannot find        |
      | a venue whose fetch times out            |
      | a venue whose fetch fails                |

  Scenario: Every venue considered lands in exactly one outcome bucket
    Given a venue the extractor can find with 3 photos
    And a venue the extractor cannot find
    And a venue whose fetch times out
    When the photo archive job runs using the Apify source
    Then the outcome buckets must sum to the number of venues considered
