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
    Given the run includes a venue with 3 photos
    When the archive job runs and reports its outcome
    Then the run status must be "success"
    And the last-success timestamp must advance

  Scenario: A run that loses venues to timeouts is partial, not success
    Given the run includes a venue with 3 photos
    And the run includes a venue whose fetch times out
    When the archive job runs and reports its outcome
    Then the run status must be "partial"
    And the run status must not be "success"
    And the last-success timestamp must not advance

  Scenario: A run where every venue fails is partial, not success
    Given the run includes a venue whose fetch fails
    When the archive job runs and reports its outcome
    Then the run status must be "partial"
    And the last-success timestamp must not advance

  Scenario: A run stopped by credit exhaustion is an error
    Given the run includes a venue with 3 photos
    And the Apify balance runs out mid-run
    When the archive job runs and reports its outcome
    Then the run status must be "error"
    And the last-success timestamp must not advance

  Scenario: A run that skips everything already archived is still a success
    Given every selected venue was already archived by the previous run
    When the archive job runs and reports its outcome
    Then the run status must be "success"
    And no venue must be reported as a failure

  Scenario: A venue with no search query is reported as no_query
    Given the run includes a venue the source cannot address
    When the archive job runs and reports its outcome
    Then the venue outcome must be reported as "no_query"
    And the venue outcome must not be reported as "no_result"
    And the run summary must count 1 no_query

  Scenario: A venue the source cannot find is reported as no_result
    Given the run includes a venue the source cannot find
    When the archive job runs and reports its outcome
    Then the venue outcome must be reported as "no_result"
    And the venue outcome must not be reported as "no_query"
    And the run summary must count 1 no_result

  Scenario Outline: The retired no_match label must never be emitted again
    Given <situation>
    When the archive job runs and reports its outcome
    Then the venue outcome must not be reported as "no_match"

    Examples:
      | situation                                |
      | the run includes a venue the source cannot address |
      | the run includes a venue the source cannot find |
      | the run includes a venue whose fetch times out |
      | the run includes a venue whose fetch fails |

  Scenario: Every venue considered lands in exactly one outcome bucket
    Given the run includes a venue with 3 photos
    And the run includes a venue the source cannot find
    And the run includes a venue whose fetch times out
    When the archive job runs and reports its outcome
    Then the outcome buckets must sum to the number of venues considered
