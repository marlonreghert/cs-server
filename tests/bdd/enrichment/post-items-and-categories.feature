Feature: Store what a post actually announced, and what kind of thing it is
  An event sells an experience; a promotion sells a price; a menu item is a
  dish. Calling all three an event makes every query and every reviewer read
  something the data does not say. Each item a post yields keeps its own type,
  and a category in words a human recognises.

  Background:
    Given the event extraction pipeline is configured for a known venue

  # --- The type is kept -----------------------------------------------------

  Scenario: Store a promotion as a promotion
    Given a post announcing a discount on a fixed weekday
    When the post is extracted
    Then the stored item is a promotion

  Scenario: Store a menu item as a menu item
    Given a post announcing a dish available on weekdays
    When the post is extracted
    Then the stored item is a menu item

  Scenario: Store an event as an event
    Given a post announcing live music on a stated date
    When the post is extracted
    Then the stored item is an event

  Scenario: Store a type this pipeline does not recognise
    Given a post whose extraction names a type this pipeline does not know
    When the post is extracted
    Then the stored item keeps that type exactly as given

  Scenario: Let an operator correct a misclassified item
    Given a stored item recorded as a menu item
    When an operator changes its type to an event
    Then the stored item is an event
    And that correction survives the next extraction of the same post

  # --- The queue still shows decisions, not types ---------------------------

  Scenario: Auto-accept a clean menu item
    Given a post announcing a dish at a resolved venue
    When the post is extracted
    Then that item is accepted
    And that item is absent from the review queue

  Scenario: Queue an item of any type that needs a decision
    Given a post announcing a discount at an unresolved venue
    When the post is extracted
    Then that item awaits a human decision

  # --- Categories ------------------------------------------------------------

  Scenario: Record the category the model returned
    Given a post announcing a samba night
    When the post is extracted
    Then the stored item's category is samba

  Scenario: Match a category that differs only in case
    Given a post whose extraction returns a category in different case
    When the post is extracted
    Then the stored item's category uses the known spelling

  Scenario: Keep a category the vocabulary does not contain
    Given a post whose extraction returns a category nobody listed
    When the post is extracted
    Then the stored item keeps that category
    And the run counts it as outside the vocabulary

  Scenario: Read the vocabulary from configuration
    Given an operator has added a category to the configured vocabulary
    When a post using that category is extracted
    Then the stored item's category uses the configured spelling
