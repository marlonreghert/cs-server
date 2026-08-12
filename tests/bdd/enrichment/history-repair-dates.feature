@wip
Feature: History repair — dates
  Re-resolve every stored date expression with the current resolver, using only
  data already in RDS. No model call, no Apify. A repaired date moves the row's
  content identity with it, and an operator's own answer is never overwritten.

  Background:
    Given the date repair script is available
    And every source carries the post's own upload timestamp

  # ── repairing what the improved resolver can now read ──────────────────────

  Scenario: Repair a row whose date text the resolver can now read
    Given a stored item with date text "É HOJE" and no start date
    And its post was uploaded on 2026-08-07
    When the repair runs with apply
    Then the item starts on 2026-08-07

  Scenario: Correct a row whose year was rolled forward wrongly
    Given a stored item with date text "08/08" starting on 2027-08-09
    And its post was uploaded on 2026-08-12
    When the repair runs with apply
    Then the item starts on 2026-08-08

  Scenario: Correct a range that was resolved to its last day
    Given a stored item with date text "De 06 a 09 de fevereiro" starting on 2027-02-09
    When the repair runs with apply
    Then the item starts on 2027-02-06
    And the item is flagged as a collapsed range

  Scenario: Leave a row whose date text is empty undated and queued
    Given a stored item with no date text and no start date
    When the repair runs with apply
    Then the item still has no start date
    And the item is still queued for review with reason "missing_date"

  Scenario: Leave a row with no stored anchor untouched
    Given a stored item with date text "É HOJE" whose source has no upload timestamp
    When the repair runs with apply
    Then the item still has no start date
    And the report records it as skipped for a missing anchor

  # ── the operator always wins ───────────────────────────────────────────────

  Scenario: Never change a date an operator edited
    Given a stored item whose start date was edited by an operator
    When the repair runs with apply
    Then the item's start date is unchanged
    And the report records it as skipped for an operator edit

  Scenario: Never change a confirmed row and report the disagreement
    Given a confirmed item whose stored date the resolver now disagrees with
    When the repair runs with apply
    Then the item's start date is unchanged
    And the report lists it as a confirmed conflict

  Scenario: Skip a superseded row
    Given a superseded item whose date the resolver can now read
    When the repair runs with apply
    Then the item's start date is unchanged

  # ── review reasons move in both directions ─────────────────────────────────

  Scenario: Drop the missing date reason when the date resolves
    Given a stored item queued for review with reason "missing_date"
    And its date text can now be resolved
    When the repair runs with apply
    Then the item is no longer queued for "missing_date"

  Scenario: Keep a row queued for a different reason when its date resolves
    Given a stored item queued for review with reasons "missing_date" and "unresolved_venue"
    And its date text can now be resolved
    When the repair runs with apply
    Then the item is no longer queued for "missing_date"
    And the item is still queued for "unresolved_venue"

  Scenario: Add the inferred year reason when the repair had to guess
    Given a stored item whose date text states no year
    And resolving it requires rolling the year forward
    When the repair runs with apply
    Then the item is queued for review with reason "year_inferred"

  # ── identity moves with the date ───────────────────────────────────────────

  Scenario: Rewrite the source event key together with the date
    Given a stored item whose date the repair will change
    When the repair runs with apply
    Then the source event key equals the key a fresh extraction would compute
    And the key and the start date were written in one transaction

  Scenario: Hand a key collision to the merge layer instead of failing
    Given two sources of one post whose repaired dates give them the same content identity
    When the repair runs with apply
    Then the repair does not raise
    And the pair is reported as a merge candidate

  # ── the dry run is the measurement ─────────────────────────────────────────

  Scenario: Report every change and write nothing without apply
    Given several stored items whose dates the repair would change
    When the repair runs without apply
    Then no item's start date changes
    And the report lists each item's stored date and resolved date

  Scenario: Group the report by date text shape
    Given stored items with several different date text shapes
    When the repair runs without apply
    Then the report groups the changes by date text shape

  Scenario: Change nothing on a second apply
    Given the repair has already been applied
    When the repair runs with apply again
    Then no item changes
