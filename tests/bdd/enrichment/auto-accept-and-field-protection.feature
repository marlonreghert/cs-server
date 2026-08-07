Feature: Auto-accept clean events, protect fields not rows
  As the operator of the event pipeline
  I want events with nothing wrong to need no approval, and my own edits to be
  the only thing a later post cannot overwrite
  So that the review queue is a work list I can empty, and correcting a title
  never freezes the date, the lineup and everything else against better data

  Scenario: Accept an extraction with nothing wrong
    Given an extraction with no review reason, a resolved date, a linked venue
      and confidence above the floor
    When the event is persisted
    Then the event has the status "accepted"
    And the event is not in the review queue

  Scenario: Keep a flagged event awaiting a human
    Given an extraction carrying a review reason
    When the event is persisted
    Then the event has the status "pending_review"
    And the event is in the review queue

  Scenario: Keep an event with no resolved date awaiting a human
    Given an extraction with no start date
    When the event is persisted
    Then the event has the status "pending_review"

  Scenario: Keep an event with no venue awaiting a human
    Given an extraction with no venue
    When the event is persisted
    Then the event has the status "pending_review"

  Scenario: Keep a low-confidence event awaiting a human
    Given an extraction below the confidence floor
    When the event is persisted
    Then the event has the status "pending_review"

  Scenario: Record only the fields an operator patched
    Given an accepted event
    When the operator patches only its title
    Then the operator-edited fields record the title
    And they do not record any other field

  Scenario: Accumulate edited fields across successive patches
    Given an accepted event whose title the operator already patched
    When the operator patches only its price
    Then the operator-edited fields record both the title and the price

  Scenario: Update a field the operator never edited
    Given an event whose title the operator patched
    And a later post stating a different price
    When the event is re-extracted
    Then the price is updated from the later post

  Scenario: Keep an operator-edited field when a later post contradicts it
    Given an event whose title the operator patched
    And a later post stating a different title
    When the event is re-extracted
    Then the operator's title is unchanged
    And the event is flagged as diverging from the operator's record

  Scenario: Never replace a known value with a null
    Given an event carrying a price
    And a later post stating no price
    When the event is re-extracted
    Then the event still carries its price

  Scenario: Union the lineup even on an operator-edited event
    Given an event whose title the operator patched and which lists two performers
    And a later post naming a third performer
    When the event is re-extracted
    Then the event lists all three performers

  Scenario: Supersede an auto-accepted event a later run no longer finds
    Given an accepted event
    When a later run no longer returns it
    Then the event has the status "superseded"

  Scenario: Never supersede a human-confirmed event that disappears
    Given a human-confirmed event
    When a later run no longer returns it
    Then the event keeps the status "confirmed"

  Scenario: Never supersede a manually linked event that disappears
    Given an event an operator manually linked to a venue
    When a later run no longer returns it
    Then the event keeps its manual link

  Scenario: Protect every field of a legacy confirmed row with unknown edits
    Given a confirmed event with no record of which fields were edited
    And a later post stating a different title and a different price
    When the event is re-extracted
    Then neither the title nor the price is changed
