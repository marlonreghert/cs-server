Feature: One dish, one row, one year
  A dish has no end signal — no post ever says the kitchen stopped serving the
  risotto — and no date, so it never merges the way a dated event does. Left
  alone the menu accumulates a row per extraction and presents dishes abandoned
  months ago as current.

  Background:
    Given the event extraction pipeline is configured for a known venue

  # --- One dish, one row ---------------------------------------------------

  Scenario: Keep one row when two posts announce the same dish
    Given two posts from one venue announcing the same dish
    When the posts are extracted
    Then one menu item exists for that dish

  Scenario: Refresh a dish when it is posted again
    Given a stored menu item last seen months ago
    And a new post announcing the same dish
    When the post is extracted
    Then that dish records having been seen just now

  Scenario: Keep two different dishes apart
    Given two posts from one venue announcing different dishes
    When the posts are extracted
    Then two menu items exist

  Scenario: Keep the same dish at two venues apart
    Given two venues each announcing a dish of the same name
    When the posts are extracted
    Then each venue has its own menu item

  Scenario: Leave an unresolved menu item unmerged
    Given a menu item whose venue could not be resolved
    When the post is extracted
    Then that item is not merged into any dish
    And that item awaits a human decision

  # --- Staying current ------------------------------------------------------

  Scenario: Present a recently seen dish as current
    Given a menu item seen this month
    When the menu is read
    Then that dish is presented as current

  Scenario: Stop presenting a long-unseen dish as current
    Given a menu item unseen for longer than the expiry window
    When the menu is read
    Then that dish is not presented as current

  Scenario: Bring a dish back when it is posted again
    Given a menu item unseen for longer than the expiry window
    And a new post announcing that dish
    When the post is extracted
    Then that dish is presented as current

  Scenario: Keep an expired dish queryable
    Given a menu item unseen for longer than the expiry window
    When the menu is read
    Then that dish and its source posts are still readable

  Scenario: Never queue a dish because it expired
    Given a menu item unseen for longer than the expiry window
    When the review queue is listed
    Then that dish is absent from the queue

  Scenario: Honour a changed expiry window
    Given an operator has shortened the configured expiry window
    And a menu item unseen for longer than that window
    When the menu is read
    Then that dish is not presented as current

  # --- Everything else is unaffected ---------------------------------------

  Scenario: Keep events merging on their date
    Given two posts announcing the same event on different days
    When the posts are extracted
    Then two events exist

  Scenario: Keep promotions merging as before
    Given two posts announcing the same promotion on one day
    When the posts are extracted
    Then one promotion exists
