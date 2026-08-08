Feature: Capture ticket references and attractions from event flyers
  An Instagram flyer states how to buy a ticket and what is playing — which
  acts, on which stage, in which musical styles. The event row must keep both
  instead of discarding them, must accumulate attractions across the posts
  announcing one event, and must never degrade what an operator has decided.

  Background:
    Given the event extraction pipeline is configured for a known venue

  # --- Ticket references -------------------------------------------------

  Scenario: Capture a ticket reference that is not a link
    Given a post whose caption offers tickets without giving a URL
    When the post is extracted
    Then the event records the ticket reference verbatim
    And the event has no ticket URL

  Scenario: Keep a ticket URL and a text reference side by side
    Given a post whose caption gives both a ticketing link and a purchase note
    When the post is extracted
    Then the event records the ticketing link
    And the event records the purchase note verbatim
    And neither value suppresses the other

  Scenario: Leave the ticket reference empty when the caption says nothing about buying
    Given a post whose caption never mentions tickets, entry or price
    When the post is extracted
    Then the event has no ticket reference

  # --- Attractions -------------------------------------------------------

  Scenario: Capture two stages from one flyer
    Given a post whose caption announces two dance floors with different acts
    When the post is extracted
    Then each attraction is tagged with the stage it plays
    And the acts announced for one stage are not attributed to the other

  Scenario: Distinguish a live act from a DJ set in the same event
    Given a post whose caption announces a live show alongside DJ sets
    When the post is extracted
    Then the live act is recorded as a live attraction
    And each DJ is recorded as a DJ attraction

  Scenario: Constrain musical styles to the product vocabulary
    Given a post whose caption names one known musical style and one unknown one
    When the post is extracted
    Then the attraction carries the known style
    And the unknown style is not stored

  Scenario: Populate the lineup from the attractions
    Given a post whose caption announces several named acts
    When the post is extracted
    Then the event lineup lists exactly the attractions' names in the order announced

  Scenario: Skip one malformed attraction and keep its siblings
    Given an extraction response in which one attraction entry is malformed
    When the post is extracted
    Then the well-formed attractions are persisted
    And the malformed attraction is skipped and counted

  Scenario: Persist nothing partial when the response truncates
    Given an extraction response that is truncated before it completes
    When the post is extracted
    Then no event is stored for that post
    And the truncation is counted

  Scenario: Leave both fields empty for a flyer that states neither
    Given a post whose caption announces an event with no acts and no ticket information
    When the post is extracted
    Then the event has no ticket reference
    And the event has no attractions

  # --- Merging across the posts that announce one event -------------------

  Scenario: Accumulate a later post's attractions onto the same event
    Given an event already announced by a teaser naming two acts
    When a later post announcing the same event names three further acts
    Then the event carries all five acts
    And no act announced by the teaser is lost

  Scenario: Fill in a stage a teaser did not state
    Given an event already announced by a teaser naming an act with no stage
    When a later post announcing the same event places that act on a named stage
    Then the event records that act once, on the named stage

  Scenario: Keep the same act on two stages apart
    Given an event already announced by a post placing an act on one stage
    When a later post announcing the same event places that act on a second stage
    Then the event records that act once per stage

  # --- Operator protection ------------------------------------------------

  Scenario: Keep an operator's ticket reference when a later post contradicts it
    Given an event whose ticket reference an operator has edited
    When a later post announcing the same event states a different ticket reference
    Then the event keeps the operator's ticket reference
    And the event is flagged as diverging from the confirmed record

  Scenario: Never overwrite a known ticket reference with an absent one
    Given an event that already records a ticket reference
    When a later post announcing the same event states no ticket reference
    Then the event keeps the ticket reference it already had
    And the event is not flagged
