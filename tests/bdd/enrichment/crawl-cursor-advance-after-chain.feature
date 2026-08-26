@wip
Feature: Advance the crawl cursor only after the chain that follows it completes
  A scheduled Instagram crawl fetches and bills posts, then archives,
  classifies, and extracts them (the "chain"). The cursor must only advance
  once that chain has actually finished, so a crash or an unexpected failure
  partway through the chain leaves the already-billed posts safely re-
  fetchable on the next run instead of silently and permanently lost.

  Background:
    Given the Instagram crawl scheduler is configured

  Scenario: A chain failure leaves the cursor unchanged
    Given a crawl target with a known cursor that will return new posts
    When its scheduled crawl runs but the chain fails
    Then the target's cursor is unchanged

  Scenario: A chain failure still records the run's billing
    Given a crawl target with a known cursor that will return new posts
    When its scheduled crawl runs but the chain fails
    Then the run's billing is still recorded

  Scenario: A chain failure counts toward the target's failure total
    Given a crawl target with a known cursor that will return new posts
    When its scheduled crawl runs but the chain fails
    Then the target's consecutive-failure count increases

  Scenario: A successful chain advances the cursor exactly as before
    Given a crawl target with a known cursor that will return new posts
    When its scheduled crawl runs
    Then the target's cursor advances to the newest returned post's time
    And the new posts are archived
    And event extraction runs for them
