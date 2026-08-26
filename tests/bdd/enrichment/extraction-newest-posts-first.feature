Feature: Extraction processes the newest archived posts first
  As the operator of the event pipeline
  I want the per-venue post cap to spend on the NEWEST archived posts
  So that a venue's crawl keeps surfacing fresh flyers instead of being
  permanently stuck on whichever posts happened to archive first

  Background:
    Given an event-candidate venue with an Instagram handle
    And its Instagram posts are archived with their captions and flyer images

  Scenario: Cap keeps the newest posts, never the oldest, regardless of archive order
    Given three qualifying posts archived out of chronological order: an oldest, a middle, and a newest
    And the per-venue post cap is 2
    When event extraction runs
    Then the newest post is extracted
    And the middle post is extracted
    And the oldest post is not extracted

  Scenario: The cap still bounds how many posts are processed
    Given three qualifying posts archived out of chronological order: an oldest, a middle, and a newest
    And the per-venue post cap is 2
    When event extraction runs
    Then 2 posts are reported as qualifying

  Scenario: A post with no usable timestamp is dropped before a dated post, never crashes
    Given a qualifying post with no usable timestamp alongside two qualifying dated posts
    And the per-venue post cap is 2
    When event extraction runs
    Then event extraction completes without error
    And the post with no usable timestamp is not extracted
    And both dated posts are extracted

  Scenario: A post with no usable timestamp is still processed when the cap allows it
    Given a qualifying post with no usable timestamp alongside two qualifying dated posts
    And the per-venue post cap is 10
    When event extraction runs
    Then event extraction completes without error
    And the post with no usable timestamp is extracted
    And both dated posts are extracted

  Scenario: A venue with fewer posts than the cap is unaffected
    Given three qualifying posts archived out of chronological order: an oldest, a middle, and a newest
    And the per-venue post cap is 10
    When event extraction runs
    Then the newest post is extracted
    And the middle post is extracted
    And the oldest post is extracted

  Scenario: Re-extracting by handle also keeps the newest posts, never the oldest
    Given a shared handle with three qualifying posts archived out of chronological order: an oldest, a middle, and a newest
    And the per-venue post cap is 2
    When extraction runs for that handle
    Then the newest post is extracted
    And the middle post is extracted
    And the oldest post is not extracted
