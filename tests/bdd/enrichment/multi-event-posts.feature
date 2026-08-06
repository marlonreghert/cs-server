Feature: Multi-event posts
  As the operator of the event pipeline
  I want one Instagram post to be able to announce several events
  So that a city-listings roundup does not silently collapse into its first
  event, and an operator's decisions survive the model changing its mind

  Background:
    Given an active promoter account
    And the catalog holds the venues named in its posts

  Scenario: Extract every event a roundup caption announces
    Given a post whose caption announces three events at three different venues
    When the multi-event extraction runs
    Then three events are persisted for that post
    And each event carries its own title

  Scenario: Resolve each event in a post to its own venue
    Given a post whose caption announces three events at three different venues
    When the multi-event extraction runs
    Then the three events are linked to three different venues

  Scenario: Resolve each event's date independently
    Given a post announcing one event today and one event on a later date
    When the multi-event extraction runs
    Then the two events carry different start dates
    And both dates are resolved against the post timestamp

  Scenario: Leave a single-party flyer producing exactly one event
    Given a post whose flyer announces a single party
    When the multi-event extraction runs
    Then exactly one event is persisted for that post

  Scenario: Produce no duplicates when a re-extraction reorders the events
    Given a post that already produced three events
    When the multi-event extraction runs again and the model returns them in a different order
    Then three events exist for that post
    And no duplicate event is created

  Scenario: Preserve a confirmed event across a reordered re-extraction
    Given a post that already produced three events
    And the operator confirmed one of them with a corrected title
    When the multi-event extraction runs again and the model returns them in a different order
    Then the confirmed event's corrected title is unchanged
    And it is still attached to the same event

  Scenario: Preserve a manually linked event across re-extraction
    Given a post whose event an operator linked manually
    When the multi-event extraction runs again over that post
    Then the manually linked event stays linked to the venue the operator chose

  Scenario: Supersede an event a later extraction no longer finds
    Given a post that already produced three events
    When the multi-event extraction runs again and returns only two of them
    Then the missing event has the status "superseded"
    And the missing event is not deleted

  Scenario: Never supersede a confirmed event that disappears
    Given a post that already produced a confirmed event
    When the multi-event extraction runs again and no longer returns that event
    Then the confirmed event keeps its status

  Scenario: Never supersede a manually linked event that disappears
    Given a post that already produced a manually linked event
    When the multi-event extraction runs again and no longer returns that event
    Then that event keeps its manual link

  Scenario: Leave an event with no location text unresolved
    Given a post announcing two events where only the first names a venue
    When the multi-event extraction runs
    Then the first event is linked to that venue
    And the second event is unresolved
    And the second event does not inherit the first event's venue

  Scenario: Persist nothing partial when the response is truncated
    Given a post whose extraction response is cut off mid-list
    When the multi-event extraction runs
    Then the post is counted with the outcome "truncated"
    And no event is persisted for that post
    And the raw response is kept

  Scenario: Skip one malformed event and keep its siblings
    Given a post whose extraction returns three events and one is malformed
    When the multi-event extraction runs
    Then two events are persisted for that post
    And the malformed event is counted

  Scenario: Record how many events each post produced
    Given a post whose caption announces three events at three different venues
    When the multi-event extraction runs
    Then the events-per-post measurement records three for that post
