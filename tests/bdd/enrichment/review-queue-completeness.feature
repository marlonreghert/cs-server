@wip
Feature: Review queue completeness and venue names
  As the operator of the event pipeline
  I want the review queue to hold every event awaiting my decision, each showing
  its venue's name
  So that an event queued because its date could not be read is actually visible,
  and a linked event does not read as unlinked

  Scenario: Return a venue-post event awaiting review
    Given a venue-post event with the status "pending_review"
    When the review queue is requested
    Then that event is in the queue

  Scenario: Return a venue-post event queued for having no date
    Given a venue-post event with no start time queued for review
    When the review queue is requested
    Then that event is in the queue
    And it carries the reason it is awaiting review

  Scenario: Return a promoter event awaiting a venue decision
    Given a promoter event with no location decision
    When the review queue is requested
    Then that event is in the queue

  Scenario: Return a promoter event confirmed but still lacking a venue
    Given a promoter event with the status "confirmed" and no location decision
    When the review queue is requested
    Then that event is in the queue

  Scenario: Exclude an event an operator has finished with
    Given a confirmed venue-post event whose venue is settled
    When the review queue is requested
    Then that event is not in the queue

  Scenario: Exclude rejected and superseded events
    Given a rejected event and a superseded event
    When the review queue is requested
    Then neither event is in the queue

  Scenario: Return ranked candidates for an event that has them
    Given a promoter event with three candidate venues
    When the review queue is requested
    Then that event carries its three candidates in rank order

  Scenario: Return an empty candidate list for a venue-post event
    Given a venue-post event with the status "pending_review"
    When the review queue is requested
    Then that event carries an empty candidate list
    And the request does not fail

  Scenario: Order the queue oldest first across both kinds
    Given a venue-post event and a promoter event awaiting review with different ages
    When the review queue is requested
    Then the older event is listed first

  Scenario: Carry the venue name on a linked event
    Given a venue-post event linked to a venue named "Métropole Recife"
    When the review queue is requested
    Then that event carries the venue name "Métropole Recife"

  Scenario: Carry a null venue name on an unresolved event
    Given a promoter event with no venue
    When the review queue is requested
    Then that event carries no venue name
    And that event is still returned

  Scenario: Tolerate a dangling venue reference
    Given an event whose venue id matches no venue
    When the review queue is requested
    Then that event carries no venue name
    And that event is still returned

  Scenario: Carry the venue name on the event list and detail too
    Given a venue-post event linked to a venue named "Métropole Recife"
    When the events are listed
    Then that event carries the venue name "Métropole Recife"
    When that event is requested on its own
    Then it carries the venue name "Métropole Recife"
