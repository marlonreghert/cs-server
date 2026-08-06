@wip
Feature: Venue-post multi-event
  As the operator of the event pipeline
  I want a venue's own post to be able to announce several events
  So that a venue advertising two nights in one post does not silently lose
  one of them, and an operator's decisions survive the model changing its mind

  Background:
    Given an event-candidate venue with an Instagram handle
    And its posts are archived with their captions and flyer images

  Scenario: Extract every event a venue post announces
    Given a venue post whose flyer announces three events
    When venue event extraction runs
    Then three events are persisted for that post
    And each event carries its own title

  Scenario: Attribute every event in a venue post to the posting venue
    Given a venue post whose flyer announces three events
    When venue event extraction runs
    Then all three events are attributed to the posting venue

  Scenario: Resolve each event's date independently
    Given a venue post announcing one event on a date and one on a later date
    When venue event extraction runs
    Then the two events carry different start dates
    And both dates are resolved against the post timestamp

  Scenario: Leave a single-event venue post producing exactly one event
    Given a venue post whose flyer announces a single party
    When venue event extraction runs
    Then exactly one event is persisted for that post

  Scenario: Produce no duplicates when a re-extraction reorders the events
    Given a venue post that already produced three events
    When venue event extraction runs again and returns them in a different order
    Then three events exist for that post
    And no duplicate venue event is created

  Scenario: Preserve a confirmed venue event across a reordered re-extraction
    Given a venue post that already produced three events
    And the operator confirmed one of them with a corrected title
    When venue event extraction runs again and returns them in a different order
    Then the confirmed venue event's corrected title is unchanged

  Scenario: Flag a divergence when a confirmed event's title no longer matches
    Given a confirmed venue event
    When venue event extraction runs again and returns a different title
    Then the confirmed venue event is flagged as diverging from the model
    And its status is still confirmed

  Scenario: Flag a divergence when a confirmed event's date no longer matches
    Given a confirmed venue event
    When venue event extraction runs again and returns a different date
    Then the confirmed venue event is flagged as diverging from the model
    And its status is still confirmed

  Scenario: Flag a divergence on a confirmed promoter event too
    Given a confirmed promoter event
    When the multi-event extraction runs again and returns a different title
    Then the confirmed promoter event is flagged as diverging from the model
    And its status is still confirmed

  Scenario: Supersede a venue event a later run no longer finds
    Given a venue post that already produced three events
    When venue event extraction runs again and returns only two of them
    Then the missing venue event has the status "superseded"
    And the missing venue event is not deleted

  Scenario: Never supersede a confirmed venue event that disappears
    Given a venue post that already produced a confirmed event
    When venue event extraction runs again and no longer returns that event
    Then the confirmed venue event keeps its status

  Scenario: Never supersede a manually linked venue event that disappears
    Given a venue post that already produced a manually linked event
    When venue event extraction runs again and no longer returns that event
    Then that venue event keeps its manual link

  Scenario: Persist nothing partial when a venue post's response is truncated
    Given a venue post whose extraction response is cut off mid-list
    When venue event extraction runs
    Then the post is counted with the venue outcome "truncated"
    And no venue event is persisted for that post

  Scenario: Skip one malformed event and keep its siblings
    Given a venue post whose extraction returns three events and one is malformed
    When venue event extraction runs
    Then two events are persisted for that post
    And the malformed venue event is counted

  Scenario: Record how many events a venue post produced
    Given a venue post whose flyer announces three events
    When venue event extraction runs
    Then the events-per-post measurement records three for that venue post

  Scenario: Keep a location without re-attributing the event
    Given a venue post whose flyer names a location
    When venue event extraction runs
    Then the event records that location text
    And the event is still attributed to the posting venue
