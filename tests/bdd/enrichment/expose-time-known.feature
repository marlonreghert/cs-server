@wip
Feature: Say whether an item's start time was actually read
  Nearly every item starts at midnight Recife, because a date was resolved and
  no clock time was. A consumer cannot tell that from a genuine midnight show by
  looking at the time, so the pipeline must say which it is.

  Background:
    Given the event extraction pipeline is configured for a known venue

  Scenario: Report a known start time as known
    Given a post stating the time an event starts
    When the post is extracted
    Then the item reports its start time as known

  Scenario: Report a date-only item's time as unknown
    Given a post stating only the date an event happens
    When the post is extracted
    Then the item reports its start time as unknown

  Scenario: Report an item predating the flag as unknown
    Given a stored item with no recorded time-known flag
    When the item is served
    Then the item reports its start time as unknown

  Scenario: Report a genuine midnight start as known
    Given a post stating an event starts at midnight
    When the post is extracted
    Then the item reports its start time as known
