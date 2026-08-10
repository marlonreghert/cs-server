Feature: Process each crawled post once, and attribute it to one venue
  A reel is also a grid post, so the posts and reels streams return overlapping
  items and the same post must not be archived, classified and extracted twice.
  A handle shared by several venues must have each post attributed to the venue
  its own content names, never to all of them and never by ordering.

  Background:
    Given the Instagram crawl scheduler is configured

  # --- Overlapping streams -------------------------------------------------

  Scenario: Process a post returned by both streams only once
    Given a crawl target with reels enabled
    And one post is returned by both the posts and the reels stream
    When its scheduled crawl runs
    Then that post is archived once
    And that post is classified once
    And that post is extracted once

  Scenario: Keep both cursors advancing when the streams overlap entirely
    Given a crawl target with reels enabled
    And every returned reel is also returned as a post
    When its scheduled crawl runs
    Then the posts cursor advances to the newest post
    And the reels cursor advances to the newest reel

  Scenario: Count what the reels stream added beyond the posts stream
    Given a crawl target with reels enabled
    And some reels were already returned by the posts stream
    When its scheduled crawl runs
    Then the run records how many reels results were new
    And the run records how many were already seen as posts

  # --- The reels default ---------------------------------------------------

  Scenario: Crawl only posts for a newly created target
    Given a crawl target created without choosing whether to crawl reels
    When its scheduled crawl runs
    Then only the posts stream is scraped

  Scenario: Keep crawling reels for a target that already had them enabled
    Given an existing crawl target with reels enabled
    When its scheduled crawl runs
    Then both streams are scraped

  # --- Attributing a shared handle -----------------------------------------

  Scenario: Attribute a post to the only venue behind a handle
    Given a crawl target whose handle belongs to one venue
    When its scheduled crawl runs
    Then every post is attributed to that venue

  Scenario: Attribute a post to the branch its caption names
    Given a crawl target whose handle belongs to two venues in different areas
    And a post whose caption names one of those areas
    When its scheduled crawl runs
    Then the event is attributed to the venue in that area
    And it is not attributed to the other venue

  Scenario: Queue an event whose post names no venue
    Given a crawl target whose handle belongs to two venues
    And a post whose caption names neither of them
    When its scheduled crawl runs
    Then the event is attributed to no venue
    And the event awaits a human decision

  Scenario: Never attribute one post to several venues
    Given a crawl target whose handle belongs to two venues
    When its scheduled crawl runs
    Then no post produces an event for more than one venue

  Scenario: Archive a shared handle's post once
    Given a crawl target whose handle belongs to two venues
    When its scheduled crawl runs
    Then each post's images are archived once, not once per venue

  Scenario: Never guess a venue below the confidence floor
    Given a crawl target whose handle belongs to two venues
    And a post whose caption weakly suggests one of them
    When its scheduled crawl runs
    Then the event is attributed to no venue
    And the event awaits a human decision
