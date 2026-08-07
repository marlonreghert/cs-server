Feature: One event, many posts
  As the operator of the event pipeline
  I want a countdown campaign to be one event carrying all of its posts
  So that three announcements of the same Saturday are one row I can act on,
  and a later post completes an earlier one instead of duplicating it

  Background:
    Given a venue with archived Instagram posts

  Scenario: Collapse a countdown campaign into one event
    Given three posts announcing the same event on the same date at the same venue
    When the events are extracted
    Then one event exists for that date
    And that event carries three sources

  Scenario: Complete a thin teaser from a later detailed post
    Given a teaser post naming only the event and its date
    And a later post for the same event naming a time and a lineup
    When the events are extracted
    Then the single event carries the date from the teaser
    And it carries the time from the later post
    And it carries the lineup from the later post

  Scenario: Union the lineup across posts
    Given a post naming two performers and a later post naming five for the same event
    When the events are extracted
    Then the single event lists all the distinct performers
    And no performer is listed twice

  Scenario: Flag a contradiction rather than choosing silently
    Given two posts for the same event stating different times
    When the events are extracted
    Then the event keeps the time from the most recent post
    And the merged event is flagged with the reason "sources_disagree"

  Scenario: Never recompute a confirmed event when a new source arrives
    Given a confirmed event with an operator's corrected title
    And a later post announcing the same event
    When the events are extracted
    Then the confirmed event's corrected title survives the merge
    And the new source is attached to that event
    And the divergence is flagged

  Scenario: Field-level protection extends to the merge path, not just re-extraction
    Given a confirmed event whose price an operator corrected
    And a later post for the same event stating a different price and a new description
    When the events are extracted
    Then the operator's price survives the merge
    And the divergence from the confirmed record is flagged
    And the new description is absorbed from the later post

  Scenario: Keep re-extraction of one post idempotent
    Given a post that already produced an event
    When that post is extracted again
    Then the event carries one source
    And no second event is created

  Scenario: Never merge events that have no venue
    Given two posts announcing the same title and date with no venue resolved
    When the events are extracted
    Then two separate events exist

  Scenario: Never merge events that have no date
    Given two posts announcing the same title at the same venue with no date
    When the events are extracted
    Then two separate events exist

  Scenario: Never merge two same-night events with different titles
    Given two posts announcing different events on the same date at the same venue
    When the events are extracted
    Then two separate events exist

  Scenario: Merge titles differing only in case and accents
    Given two posts for the same date and venue titled "NOITE DA PATROA" and "Noite da Patroa"
    When the events are extracted
    Then one event exists for that date

  Scenario: Expose every source on the merged event
    Given three posts announcing the same event on the same date at the same venue
    When the merged event is requested
    Then each source carries its own permalink
    And each source carries its own cover photo key

  Scenario: Keep serving the single-source fields for existing consumers
    Given three posts announcing the same event on the same date at the same venue
    When the merged event is requested
    Then the event still carries a source permalink
    And the event still carries a cover photo key
    And those values come from the most recent source
