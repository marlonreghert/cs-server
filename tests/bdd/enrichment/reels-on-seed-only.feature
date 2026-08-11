@wip
Feature: Crawl a target's reels once, on its first run
  A reel is also a grid post, so after the first run the posts stream already
  carries almost every reel — thirty-two reels results bought one new post on a
  real target. But a seed is different: the reels endpoint has its own cap and
  can reach history the posts cap cannot, and that history is otherwise
  unreachable because a cursor only moves forward.

  Background:
    Given the Instagram crawl scheduler is configured

  Scenario: Crawl reels on a target's first run
    Given a crawl target with reels enabled that has never run
    When its scheduled crawl runs
    Then the reels stream is scraped

  Scenario: Skip reels once they have been seeded
    Given a crawl target with reels enabled that has already seeded reels
    When its scheduled crawl runs
    Then the reels stream is not scraped

  Scenario: Never crawl reels for a target with reels disabled
    Given a crawl target with reels disabled that has never run
    When its scheduled crawl runs
    Then the reels stream is not scraped

  Scenario: Seed reels again when the first attempt recorded nothing
    Given a crawl target with reels enabled whose first run recorded no reels cursor
    When its scheduled crawl runs
    Then the reels stream is scraped

  Scenario: Leave the posts stream unaffected
    Given a crawl target with reels enabled that has already seeded reels
    When its scheduled crawl runs
    Then the posts stream is scraped

  Scenario: Record why reels were skipped
    Given a crawl target with reels enabled that has already seeded reels
    When its scheduled crawl runs
    Then the run records that reels were already seeded
