Feature: Hide promoter events
  Promoter-sourced items are out of scope for now, so the admin console shows
  only venue-sourced coverage. Nothing is deleted or re-statused — the filter is
  a runtime flag, and every promoter row stays queryable for the day promoters
  come back.

  Background:
    Given promoter events are hidden

  Scenario: Hide a promoter-sourced item from the events list
    Given an item whose only source is a promoter post
    When an operator reads the events list
    Then that item is not listed

  Scenario: Hide a promoter-sourced item from the review queue
    Given an item whose only source is a promoter post and which needs a decision
    When an operator reads the review queue
    Then that item is not queued

  Scenario: Show a venue-sourced item in both
    Given an item whose only source is a venue post and which needs a decision
    When an operator reads the events list
    Then that item is listed
    When an operator reads the review queue
    Then that item is queued

  Scenario: Show an item that has one promoter source and one venue source
    Given an item with one promoter source and one venue source
    When an operator reads the events list
    Then that item is listed

  Scenario: Show an item with no sources at all
    Given an item with no sources
    When an operator reads the events list
    Then that item is listed

  Scenario: Make the counts agree with the filtered list
    Given 3 venue-sourced items and 7 promoter-sourced items
    When an operator reads the events list
    Then 3 items are listed
    And the reported total is 3

  Scenario: Show promoter items again when the flag is turned off
    Given 3 venue-sourced items and 7 promoter-sourced items
    And promoter events are not hidden
    When an operator reads the events list
    Then 10 items are listed

  Scenario: Report the flag's current value on the admin API
    When an operator reads the admin configuration
    Then it reports that promoter events are hidden

  Scenario: Leave stored promoter rows unchanged
    Given an item whose only source is a promoter post
    When an operator reads the events list
    Then the stored item still exists with its original status
    And the stored item still carries its promoter source
