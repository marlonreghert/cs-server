Feature: Backfill source provenance
  Every archived post recorded when it was posted and what medium it was. Copy
  those facts onto the stored source rows that predate the columns, without
  inventing a value and without overwriting one already there.

  Background:
    Given the provenance backfill script is available

  Scenario: Fill both fields from an archived manifest record
    Given a stored source with no upload time and no media type
    And its archived manifest records an upload time of 2026-08-12 15:26 and media type "Video"
    When the backfill runs with apply
    Then the stored source records an upload time of 2026-08-12 15:26
    And the stored source records its media type as "Video"

  Scenario: Leave a row that already has values untouched
    Given a stored source that already records an upload time and a media type
    When the backfill runs with apply
    Then that source's upload time is unchanged
    And that source's media type is unchanged

  Scenario: Leave a row whose manifest has no upload time with a null upload time
    Given a stored source with no upload time
    And its archived manifest records no upload time
    When the backfill runs with apply
    Then the stored source still records no upload time

  Scenario: Never substitute the crawl time for a missing upload time
    Given a stored source with no upload time
    And its archived manifest records no upload time
    When the backfill runs with apply
    Then the stored source's upload time does not equal its first seen time

  Scenario: Report a row whose manifest record cannot be found
    Given a stored source with no upload time
    And no archived manifest record matches it
    When the backfill runs with apply
    Then the stored source still records no upload time
    And the report counts it as unmatched

  Scenario: Report every change and write nothing without apply
    Given several stored sources with no upload time
    When the backfill runs without apply
    Then no source is changed
    And the report counts what it would have filled

  Scenario: Change nothing on a second apply
    Given the backfill has already been applied
    When the backfill runs with apply again
    Then no source is changed

  Scenario: Continue past an unreadable manifest
    Given two stored sources and the first one's manifest cannot be read
    When the backfill runs with apply
    Then the second source is still filled
    And the report counts the unreadable manifest

  Scenario: Write nothing to the archive
    Given a stored source with no upload time
    When the backfill runs with apply
    Then no object is written to the archive bucket
