@wip
Feature: A tombstone says what replaced it
  As the operator of the event pipeline
  I must be able to see which event replaced a superseded one, so that a row
  retired because a later extraction read its date better is explainable from
  the data instead of reading, in the console, as a duplicate nobody merged.

  # A re-extraction supersede: the SAME post read twice, resolving a different
  # date the second time, so its computed identity moved and the old row was
  # retired. Distinct from a dedup merge, which folds DIFFERENT posts together
  # and already records its survivor.

  Scenario: Record the replacement when one successor carries the same title
    Given a stored event superseded by a re-extraction of its own post
    And the post's new events include exactly one with the same title
    When the post is reconciled
    Then the superseded event records that event as its replacement

  Scenario: Leave the link empty when no new event carries that title
    Given a stored event superseded by a re-extraction of its own post
    And the post's new events carry no matching title
    When the post is reconciled
    Then the superseded event records no replacement
    And the unlinked supersede is counted

  Scenario: Leave the link empty when several new events share that title
    Given a stored event superseded by a re-extraction of its own post
    And two of the post's new events carry the same title
    When the post is reconciled
    Then the superseded event records no replacement
    And the unlinked supersede is counted

  Scenario: Never link across posts
    Given a stored event superseded by a re-extraction of its own post
    And a different post has an event with the same title
    When the post is reconciled
    Then the superseded event does not record that event as its replacement

  Scenario: Count a linked supersede distinctly from an unlinked one
    Given a post whose re-extraction supersedes one linkable and one ambiguous event
    When the post is reconciled
    Then the linked and unlinked supersedes are counted separately

  Scenario: Keep the merge path's own replacement recording unchanged
    Given two events merged by title similarity
    When the merge is applied
    Then the absorbed event still records the surviving event as its replacement

  Scenario: Report the replacement on the admin API
    Given a superseded event that records a replacement
    When the event is read from the admin API
    Then the event reports which event replaced it

  Scenario: Report no replacement for a row that has none
    Given a superseded event that records no replacement
    When the event is read from the admin API
    Then the event reports no replacement

  # The back-fill over rows superseded before this was recorded.

  Scenario: Back-fill an orphan whose successor is unambiguous
    Given a superseded event with no recorded replacement
    And exactly one live event from the same post carries the same title
    When the back-fill runs with apply
    Then the superseded event records that event as its replacement

  Scenario: Leave an ambiguous orphan untouched and name it
    Given a superseded event with no recorded replacement
    And two live events from the same post carry the same title
    When the back-fill runs with apply
    Then the superseded event still records no replacement
    And the report names it as ambiguous

  Scenario: Never back-fill a link across posts
    Given a superseded event with no recorded replacement
    And the only same-titled live event comes from a different post
    When the back-fill runs with apply
    Then the superseded event still records no replacement

  Scenario: Write nothing without apply
    Given a superseded event with no recorded replacement
    And exactly one live event from the same post carries the same title
    When the back-fill runs without apply
    Then the superseded event still records no replacement
    And the report says it would have been linked

  Scenario: Change nothing on a second back-fill
    Given a back-fill has already linked every unambiguous orphan
    When the back-fill runs with apply again
    Then no event's replacement changes
