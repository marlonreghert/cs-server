@wip
Feature: Extract the posts archived under a handle
  A post archived under an Instagram handle is unreachable today: extraction can
  only be pointed at venue ids, whose archive prefixes those posts do not share.
  Anything already in S3 must be re-readable without re-crawling, so a fix to
  date resolution or classification can be applied to posts already paid for.

  Background:
    Given the event extraction pipeline is configured for a known venue

  Scenario: Extract the posts archived under a handle
    Given a handle whose posts are archived under that handle
    When extraction runs for that handle
    Then those posts are extracted

  Scenario: Read a handle whose posts were archived under a venue
    Given a handle whose posts are archived under its venue
    When extraction runs for that handle
    Then those posts are extracted

  Scenario: Read a handle whose posts span both archive layouts
    Given a handle with posts archived under both its handle and its venue
    When extraction runs for that handle
    Then every one of those posts is extracted

  Scenario: Process a post archived under two prefixes only once
    Given a post archived under both a handle and a venue
    When extraction runs for that handle
    Then that post is extracted once

  Scenario: Report a handle with nothing archived
    Given a handle with no archived posts
    When extraction runs for that handle
    Then the run reports that nothing was archived for it
    And the run does not fail

  Scenario: Never re-crawl when extracting by handle
    Given a handle whose posts are archived under that handle
    When extraction runs for that handle
    Then no posts are fetched from the crawler

  Scenario: Apply today's date rules to an old post
    Given an archived post whose event was stored under superseded date rules
    When extraction runs for that handle
    Then the event's date follows the current rules

  Scenario: Supersede an event whose corrected date changed its identity
    Given an archived post whose event was stored with a wrong date
    When extraction runs for that handle
    Then the corrected event is live
    And the event with the wrong date is superseded

  Scenario: Keep an operator's edit through a re-extraction
    Given an archived post whose event has an operator-edited field
    When extraction runs for that handle
    Then that field keeps the operator's value

  Scenario: Leave extraction by venue unchanged
    Given a venue whose posts are archived under that venue
    When extraction runs for that venue
    Then those posts are extracted
