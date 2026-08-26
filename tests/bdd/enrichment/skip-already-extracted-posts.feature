Feature: Skip posts event extraction has already successfully extracted
  As the operator of the event pipeline
  I want the scheduled extraction run to skip the OpenAI vision call for a
  post it has already turned into an event
  So that the per-venue cap is spent on genuinely new posts instead of paying
  to re-read the same archived posts, run after run, forever

  Background:
    Given an event-candidate venue with an Instagram handle
    And its Instagram posts are archived with their captions and flyer images

  Scenario: A post already successfully extracted is not sent to the model again
    Given a post already extracted into an event
    When event extraction runs
    Then the model is not called a second time for that post
    And that post is counted with the outcome "skipped_seen"

  Scenario: A post whose previous extraction failed is retried
    Given a post whose only prior extraction attempt failed
    And the model now returns a valid response for it
    When event extraction runs
    Then that post is counted with the outcome "accepted"

  Scenario: The cap is spent on unprocessed posts, not consumed by already-extracted ones
    Given two already-extracted posts newer than a new, unprocessed post
    And the per-venue post cap is 1
    When event extraction runs
    Then the new post is extracted
    And 1 posts are reported as qualifying

  Scenario: A first-ever run for a venue is unaffected
    Given a post that has never been extracted before
    When event extraction runs
    Then an event is stored for that venue
    And no post is counted with the outcome "skipped_seen"

  Scenario: Deliberate re-extraction still calls the model for an already-extracted post
    Given a post already extracted into an event
    And the model returns a fresh response for it
    When extraction runs for that handle
    Then the model is called again for that post

  Scenario: A skipped post still keeps its menu item's freshness current
    Given a stored menu item last seen months ago, from a post seen again this run
    When event extraction runs
    Then no model call is made for that post
    And that dish records having been seen just now
