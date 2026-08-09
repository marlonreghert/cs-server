Feature: Crawl Instagram targets on a schedule, incrementally
  Each crawl target is crawled on its own schedule and resumes from the newest
  post it already saw, so an unchanged target costs nothing and a busy one is
  still bounded. Reels are crawled alongside posts, because some venues
  announce their events nowhere else.

  Background:
    Given the Instagram crawl scheduler is configured

  # --- Where a crawl starts ------------------------------------------------

  Scenario: Seed a brand-new target from the default lookback
    Given a crawl target that has never been crawled
    When its scheduled crawl runs
    Then the scrape is bounded to the default three-month lookback
    And the scrape is also bounded by the target's seed result cap

  Scenario: A seed uses the seed cap even when a steady-state cap is also set
    Given a crawl target with a steady-state cap but no cursor
    When its scheduled crawl runs
    Then the scrape is bounded by the seed cap, not the steady-state cap

  Scenario: A target's own seed cap overrides the default
    Given a crawl target with its own seed cap and no cursor
    When its scheduled crawl runs
    Then the scrape is bounded by that target's own seed cap

  Scenario: Resume an existing target from its cursor
    Given a crawl target whose newest seen post is known
    When its scheduled crawl runs
    Then the scrape is bounded to that post's time minus the configured overlap
    And no post older than that bound is requested

  Scenario: Use a target's own lookback when one is configured
    Given a crawl target with its own initial lookback and no cursor
    When its scheduled crawl runs
    Then the scrape is bounded to that target's lookback

  # --- How the cursor moves ------------------------------------------------

  Scenario: Advance the cursor to the newest post returned
    Given a crawl target that returns several posts of differing ages
    When the crawl succeeds
    Then the target's cursor is the newest returned post's time
    And the cursor is stored as UTC

  Scenario: Leave the cursor untouched when a crawl fails
    Given a crawl target with a known cursor
    When its crawl fails partway through
    Then the target's cursor is unchanged

  Scenario: Leave the cursor untouched when a crawl returns nothing
    Given a crawl target with a known cursor
    When its crawl returns no posts
    Then the target's cursor is unchanged
    And nothing is archived or extracted

  # --- Paying once per handle ----------------------------------------------

  Scenario: Crawl a shared handle only once
    Given two venues that share one Instagram handle
    And a single crawl target for that handle
    When the scheduled crawl runs
    Then the handle is scraped exactly once

  # --- Reels ---------------------------------------------------------------

  Scenario: Crawl reels as a separate run
    Given a crawl target with reels enabled
    When its scheduled crawl runs
    Then posts and reels are scraped as two separate runs

  Scenario: Track reels and posts with independent cursors
    Given a crawl target with reels enabled and both cursors set
    When a crawl returns new posts but no new reels
    Then the posts cursor advances
    And the reels cursor is unchanged

  Scenario: Skip reels for a target that has them disabled
    Given a crawl target with reels disabled
    When its scheduled crawl runs
    Then only posts are scraped

  # --- Pinned posts --------------------------------------------------------

  Scenario: Drop a pinned post older than the requested bound
    Given a crawl target whose profile pins an old post
    When its scheduled crawl runs
    Then the pinned post is not archived
    And the pinned post is counted as dropped

  Scenario: Never let a pinned post move the cursor
    Given a crawl whose only returned post is an old pinned one
    When the crawl completes
    Then the target's cursor is unchanged

  # --- Spending ceilings ---------------------------------------------------

  Scenario: Refuse to crawl once the monthly result budget is exhausted
    Given the monthly result budget is exhausted
    When a scheduled crawl is due
    Then no scrape is requested at all
    And the refusal is recorded

  Scenario: Apply the steady-state result cap alongside the date bound
    Given a crawl target with a known cursor and a steady-state result cap
    When its scheduled crawl runs
    Then the scrape carries both the date bound and the cap

  Scenario: The remaining monthly budget caps an oversized request
    Given the monthly result budget has less remaining than the target's cap
    When its scheduled crawl runs
    Then the scrape is capped to the remaining budget, not the full cap

  Scenario: Stop the whole cycle when Apify reports credit exhaustion
    Given several crawl targets are due
    When the first target's scrape reports credit exhaustion
    Then no further target is scraped in that cycle

  # --- Which targets run ---------------------------------------------------

  Scenario: Skip a disabled target
    Given a crawl target that is disabled
    When its schedule fires
    Then no scrape is requested for it

  Scenario: Skip a target that keeps failing
    Given a crawl target past its consecutive-failure threshold
    When its schedule fires
    Then no scrape is requested for it

  Scenario: Run each target on its own schedule
    Given one target scheduled for weekend nights and another scheduled monthly
    When only the weekend schedule fires
    Then only the weekend target is scraped

  # --- Chaining ------------------------------------------------------------

  Scenario: Chain archiving and extraction after a crawl that found posts
    Given a crawl target that returns new posts
    When the crawl succeeds
    Then the new posts are archived
    And event extraction runs for them
