@wip
Feature: Show an operator the duplicate and refusal backlog they cannot see today
  A refused merge pair records nothing at all — no row, no queue entry, nothing
  an operator can act on — so a backlog of thousands of refusals accumulated in
  production without anyone noticing, next to a venue-night duplicate population
  nobody could count. This feature gives both a standing number and a report
  that names the venues and the accounts behind them, so the fix can be judged
  on a before and after rather than on hope.

  Reading the backlog never changes a row: it is a report over what is already
  stored.

  Background:
    Given the event extraction pipeline is configured for a known venue
    And the venue catalog carries "Club Metrópole" and "BeerDock Boa Viagem"

  # ── the duplicate population ──────────────────────────────────────────────

  Scenario: Report every venue-night holding more than one live event
    Given three stored events at "Club Metrópole" on one Saturday
    And one stored event at "BeerDock Boa Viagem" on that Saturday
    When the dedup backlog is read
    Then the backlog reports one venue-night group
    And the group names "Club Metrópole" and that Saturday
    And the group lists the three titles

  Scenario: Report the excess row count for the whole corpus
    Given three stored events at "Club Metrópole" on one Saturday
    And two stored events at "BeerDock Boa Viagem" on one Sunday
    When the dedup backlog is read
    Then the backlog reports two venue-night groups
    And the backlog reports three excess rows

  Scenario: Report an empty backlog when no venue-night holds more than one event
    Given one stored event at "Club Metrópole" on one Saturday
    And one stored event at "BeerDock Boa Viagem" on one Sunday
    When the dedup backlog is read
    Then the backlog reports no venue-night groups
    And the backlog reports no excess rows

  Scenario: Exclude superseded events from every backlog measure
    Given two stored events at "Club Metrópole" on one Saturday
    And one of them has been superseded by a merge
    When the dedup backlog is read
    Then the backlog reports no venue-night groups

  Scenario: Exclude non-event post types from every backlog measure
    Given one stored event and one stored menu item at "Club Metrópole" on one Saturday
    When the dedup backlog is read
    Then the backlog reports no venue-night groups

  Scenario: Exclude events with no venue from every backlog measure
    Given two stored events on one Saturday with no venue resolved
    When the dedup backlog is read
    Then the backlog reports no venue-night groups

  # ── the refusals nobody can see today ─────────────────────────────────────

  Scenario: Report refused same-night pairs split by refusal reason
    Given two stored events at "Club Metrópole" on one Saturday whose titles share no distinctive word
    And two stored events at "BeerDock Boa Viagem" on one Sunday whose titles are entirely generic
    When the dedup backlog is read
    Then the backlog reports one pair refused as disjoint
    And the backlog reports one pair refused for having no distinctive tokens

  Scenario: Report pending merge suggestions awaiting an operator
    Given two stored events at "Sempre Rock Bar" on one day whose titles differ only in the speaker's name
    And the merge pass has run for that venue
    When the dedup backlog is read
    Then the backlog reports one pending merge suggestion

  # ── the venue-acquisition backlog ─────────────────────────────────────────

  Scenario: Report the accounts whose events name a location the catalog does not carry
    Given two stored events at "BeerDock Boa Viagem" whose recorded location text is "@beerdock.madalena"
    When the dedup backlog is read
    Then the backlog reports "beerdock_recife" as an account with disputed attributions
    And the backlog names "@beerdock.madalena" and the two events it would recover

  Scenario: Report an account's distinct location texts separately
    Given two stored events at "BeerDock Boa Viagem" whose recorded location text is "CASA FORTE"
    And one stored event at "BeerDock Boa Viagem" whose recorded location text is "MADALENA"
    When the dedup backlog is read
    Then the backlog reports two distinct location texts for "beerdock_recife"
    And each location text carries its own row count

  # ── the standing number ───────────────────────────────────────────────────

  Scenario: Publish the backlog measures as gauges after an extraction run
    Given three stored events at "Club Metrópole" on one Saturday
    When an extraction run completes
    Then the venue-night group gauge reports one
    And the excess row gauge reports two

  Scenario: Show the backlog shrinking after a merge absorbs a cluster
    Given "Club Metrópole" runs one night rather than a programme
    And three stored events at "Club Metrópole" on one Saturday
    And the excess row gauge has been recorded
    When the dedup sweep runs with apply for that single-night venue
    And an extraction run completes
    Then the excess row gauge reports fewer rows than before the sweep

  # ── the operator-facing report ────────────────────────────────────────────

  Scenario: Serve the backlog report to an operator
    Given three stored events at "Club Metrópole" on one Saturday
    When an operator reads the dedup backlog report
    Then the response lists the venue-night group with its titles
    And the response lists the refusal counts
    And the response lists the accounts with disputed attributions

  Scenario: Page the backlog report
    Given twelve venue-nights each holding two stored events
    When an operator reads the dedup backlog report limited to five groups
    Then the response lists five groups
    And the response reports twelve groups in total

  Scenario: Change no stored row by reading the backlog
    Given three stored events at "Club Metrópole" on one Saturday
    When an operator reads the dedup backlog report
    Then every stored event is unchanged
    And no merge suggestion is recorded
