@wip
Feature: Instagram event extraction
  As the operator of the event pipeline
  I want archived posts of event-candidate venues turned into structured events
  So that what a flyer announces becomes a record an operator can check, and a
  date that cannot be determined is never invented

  Background:
    Given a venue with the tier "evidence_confirmed"
    And its Instagram posts are archived with their captions and flyer images

  Scenario: Extract an event from a flyer post
    Given a post whose flyer announces a party with a title, a date and a time
    When event extraction runs
    Then an event is stored for that venue
    And the event carries the title from the flyer
    And the event carries a start time
    And the event records the permalink of the post it came from

  Scenario: Skip a post that is neither a flyer nor caption-matched
    Given a post whose image is a plate of food
    And its caption carries no event marker
    When event extraction runs
    Then no model call is made for that post
    And that post is counted with the outcome "not_event_like"

  Scenario: Resolve a relative date against the post timestamp
    Given a post published on a Thursday whose caption says "amanhã"
    And the extraction runs three weeks after that post
    When event extraction runs
    Then the event starts on the Friday after the post was published
    And the event does not start on the day after the run

  Scenario: Resolve a date with no year to the next occurrence after the post
    Given a post published in July 2026 whose flyer says "15/08"
    When event extraction runs
    Then the event starts on 15 August 2026

  Scenario: Resolve a date with no year across a year boundary
    Given a post published in December 2026 whose flyer says "15/08"
    When event extraction runs
    Then the event starts on 15 August 2027

  Scenario: Read a numeric date day-first
    Given a post whose flyer says "05/08"
    When event extraction runs
    Then the event starts on 5 August
    And the event does not start on 5 May

  Scenario: Store no date rather than a guessed one
    Given a post whose flyer announces an event with no determinable date
    When event extraction runs
    Then the event is stored with no start time
    And the event has the status "pending_review"
    And the review reason names the missing date

  Scenario: Represent a recurring announcement as recurring
    Given a post published on a Monday whose caption says "toda quinta"
    When event extraction runs
    Then the event is marked recurring
    And the event records the recurrence as stated
    And the event starts on the Thursday of that week

  Scenario: Store an event time in the venue's timezone
    Given a post whose flyer says the event starts at "22h"
    When event extraction runs
    Then the event starts at 22:00 America/Recife

  Scenario: Update in place when the same post is extracted again
    Given a post already extracted into an event
    When event extraction runs again over that post
    Then exactly one event exists for that post
    And its last seen timestamp is updated

  Scenario: Preserve an operator's confirmed event on re-extraction
    Given an event an operator has confirmed with a corrected title
    When event extraction runs again over its post
    Then the corrected title is unchanged
    And the divergence between the model and the operator is flagged

  Scenario: Record an extraction failure without losing the post
    Given a post that qualifies for extraction
    And the model returns unparseable output for it
    When event extraction runs
    Then that post is counted with the outcome "extraction_failed"
    And the raw model output is kept
    And the run continues to the next post

  Scenario: Queue a low-confidence extraction instead of accepting it
    Given a post whose extraction confidence is below the minimum
    When event extraction runs
    Then the event has the status "pending_review"
    And the review reason names the low confidence

  Scenario: Report qualifying posts without spending under dry run
    Given 10 archived posts of which 4 qualify
    When event extraction runs as a dry run
    Then 4 posts are reported as qualifying
    And no model call is made
    And no event is written

  Scenario: Confirm and reject events through the admin API
    Given an event with the status "pending_review"
    When the operator confirms it
    Then its status is "confirmed"
    When the operator rejects another pending event
    Then that event's status is "rejected"

  Scenario: Leave the served venue response unchanged
    Given events exist for a venue
    When the venue is served through the public API
    Then the response is unchanged from before events existed
