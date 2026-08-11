@wip
Feature: Collapse an unresolved item into the sibling that resolved a venue
  One flyer lists a holiday programme's weeks, another lists its activities, and
  both announce the same workshop on the same day. When only one of them
  resolved a venue, the other must not survive beside it asking a human to link
  something already linked — but a resolved item must never be dragged back to
  having no venue, and an ambiguous case must be left alone.

  Background:
    Given the event extraction pipeline is configured for a known venue

  # --- Collapsing --------------------------------------------------------

  Scenario: Merge an unresolved item into its resolved sibling
    Given two posts from one account announcing the same thing on the same day
    And only one of them resolved a venue
    When the posts are extracted
    Then one item survives

  Scenario: Adopt the sibling's venue
    Given two posts from one account announcing the same thing on the same day
    And only one of them resolved a venue
    When the posts are extracted
    Then the surviving item is attributed to that venue

  Scenario: Leave the resolved item resolved whichever arrives first
    Given two posts from one account announcing the same thing on the same day
    And only one of them resolved a venue
    When the unresolved post is extracted first
    Then the surviving item is attributed to that venue

  # --- Refusing to guess ---------------------------------------------------

  Scenario: Refuse to merge when siblings resolved to different venues
    Given a handle whose posts resolved to two different venues on one day
    And an unresolved post announcing the same thing that day
    When the posts are extracted
    Then the unresolved item keeps no venue
    And the unresolved item awaits a human decision

  Scenario: Refuse to merge an item an operator confirmed
    Given an unresolved item an operator has confirmed
    And a resolved sibling from the same account on the same day
    When the posts are extracted
    Then the confirmed item is unchanged

  Scenario: Refuse to merge when an operator set the venue
    Given an unresolved item whose venue an operator has edited
    And a resolved sibling from the same account on the same day
    When the posts are extracted
    Then the operator's venue is unchanged

  # --- Boundaries ----------------------------------------------------------

  Scenario: Never merge across accounts
    Given two posts from different accounts announcing the same thing that day
    And only one of them resolved a venue
    When the posts are extracted
    Then both items survive

  Scenario: Never merge across dates
    Given two posts from one account announcing the same thing on different days
    And only one of them resolved a venue
    When the posts are extracted
    Then both items survive

  Scenario: Never merge an item with no date
    Given an unresolved item with no date
    And a resolved sibling from the same account
    When the posts are extracted
    Then both items survive

  Scenario: Keep merging resolved items as before
    Given two posts announcing the same thing at the same venue on one day
    When the posts are extracted
    Then one item survives

  # --- What the surviving item says ----------------------------------------

  Scenario: Drop the unresolved reason once a venue is adopted
    Given two posts from one account announcing the same thing on the same day
    And only one of them resolved a venue
    When the posts are extracted
    Then the surviving item no longer reports an unresolved venue

  Scenario: Keep the item's other reasons
    Given two posts from one account announcing the same thing on the same day
    And only one of them resolved a venue
    And the unresolved post's date was a collapsed range
    When the posts are extracted
    Then the surviving item still reports a collapsed date range

  Scenario: Record that the venue came from a sibling
    Given two posts from one account announcing the same thing on the same day
    And only one of them resolved a venue
    When the posts are extracted
    Then the surviving item records that its venue was adopted from a sibling
