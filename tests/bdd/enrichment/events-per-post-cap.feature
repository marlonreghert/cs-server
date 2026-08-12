@wip
Feature: Events per post cap
  As an operator
  I want to know how many events a post offered and how many output tokens the
  extraction spent
  So that the per-post event cap and the output token ceiling are set from a
  measured distribution instead of a guess, and can be moved independently

  Background:
    Given the per-post event cap is 20
    And the extraction output token ceiling is 13304

  Scenario: Record how many events a post offered when the cap dropped some
    Given a post whose extraction returns 26 events
    When the post is extracted
    Then 20 items are persisted for the post
    And the post records that 26 events were offered
    And the post records that the cap truncated it
    And the offered count is observed for the post

  Scenario: Record an offered count equal to the kept count when the cap did not bite
    Given a post whose extraction returns 7 events
    When the post is extracted
    Then 7 items are persisted for the post
    And the post records that 7 events were offered
    And the post does not record that the cap truncated it

  Scenario: Keep every event of a post that offers exactly the cap
    Given a post whose extraction returns 20 events
    When the post is extracted
    Then 20 items are persisted for the post
    And the post records that 20 events were offered
    And the post does not record that the cap truncated it

  Scenario: Count malformed entries as offered
    Given a post whose extraction returns 22 events of which 3 are malformed
    When the post is extracted
    Then the post records that 22 events were offered
    And the post records 3 malformed events

  Scenario: Persist the offered count on the post's sources
    Given a post whose extraction returns 26 events
    When the post is extracted
    Then every source row for the post carries the offered count
    And an operator can list the posts the cap truncated without reading logs

  Scenario: Raise the kept events without raising the output token ceiling
    Given the per-post event cap is raised to 40
    And the extraction output token ceiling is left at 13304
    And a post whose extraction returns 26 events
    When the post is extracted
    Then 26 items are persisted for the post
    And the extraction call is made with an output token ceiling of 13304

  Scenario: Raise the output token ceiling without raising the kept events
    Given the extraction output token ceiling is raised to 24000
    And the per-post event cap is left at 20
    And a post whose extraction returns 26 events
    When the post is extracted
    Then 20 items are persisted for the post
    And the extraction call is made with an output token ceiling of 24000

  Scenario: Apply the same cap and ceiling on the promoter path
    Given a promoter roundup post whose extraction returns 26 events
    When the promoter post is extracted
    Then the same per-post event cap is applied as on the venue path
    And the same output token ceiling is applied as on the venue path

  Scenario: Discard the whole post when the API cuts the response off
    Given a post whose extraction response is cut off by the output token ceiling
    When the post is extracted
    Then no item is persisted for the post
    And the post is recorded with the truncated outcome
    And the post is not recorded as truncated by the per-post event cap

  Scenario: Report the extraction cost from the reported token counts
    Given a post whose extraction reports 1200 input tokens and 4800 output tokens
    When the post is extracted
    Then the cumulative extraction spend increases by the priced value of those tokens

  Scenario: Report no cost when the API reports no token usage
    Given a post whose extraction reports no token usage
    When the post is extracted
    Then the cumulative extraction spend does not change
