@wip
Feature: Event venue targeting
  As the operator of the event pipeline
  I want a free category gate followed by a bounded evidence gate
  So that paid event crawls aim only at venues that plausibly host events, and
  a venue we never examined is never mistaken for one with no events

  Background:
    Given the servable catalog holds venues of several categories
    And the event candidate categories are the shipped defaults

  Scenario: Pass a nightclub and exclude a restaurant on category alone
    Given a venue whose category is "NIGHTCLUB"
    And a venue whose category is "RESTAURANT"
    When event targeting runs
    Then the nightclub passes the category gate
    And the restaurant is recorded with the tier "excluded_category"
    And no external call is made

  Scenario: Promote a restaurant whose vibe profile shows live music
    Given a venue whose category is "RESTAURANT"
    And its vibe profile carries the music format "Banda ao vivo"
    When event targeting runs
    Then that venue passes the category gate
    And its category reason names the vibe signal that promoted it

  Scenario: Honour an admin override that widens the allow-list
    Given the admin config adds "RESTAURANT" to the event candidate categories
    And a venue whose category is "RESTAURANT"
    When event targeting runs
    Then that venue passes the category gate

  Scenario: Fall back to the shipped defaults when the admin config is malformed
    Given the admin config for event candidate categories is malformed
    When event targeting runs
    Then the shipped default allow-list is used
    And the candidate set is not empty
    And a config fallback is counted

  Scenario: Evaluate only the top N candidates by priority
    Given 10 venues pass the category gate
    And the run evaluates at most 3 venues for evidence
    When event targeting runs
    Then the 3 highest priority candidates are evidence-evaluated
    And the remaining 7 keep the tier "category_candidate"
    And the remaining 7 have no evaluation timestamp

  Scenario: Confirm a venue whose flyer evidence clears the threshold
    Given a venue that passed the category gate
    And it has 4 photos classified as "flyer" within the lookback window
    And the run requires 3 posts of evidence
    When event targeting runs
    Then that venue is recorded with the tier "evidence_confirmed"
    And its evidence sample records the flyer count

  Scenario: Reject a venue evaluated with evidence below the threshold
    Given a venue that passed the category gate
    And it has 1 post of event evidence within the lookback window
    And the run requires 3 posts of evidence
    When event targeting runs
    Then that venue is recorded with the tier "evidence_rejected"
    And its evidence sample records what was examined

  Scenario: Distinguish a venue never examined from one rejected
    Given a venue with the tier "category_candidate" and no evaluation timestamp
    And a venue with the tier "evidence_rejected"
    When the targeting summary is requested
    Then the two venues are reported under different tiers
    And only the rejected venue carries an evaluation timestamp

  Scenario: Leave a venue with no Instagram handle unevaluated
    Given a venue that passed the category gate
    And it has no Instagram handle
    When event targeting runs
    Then that venue is recorded with the tier "unevaluated"

  Scenario: Spend nothing on a model during the evidence gate
    Given 5 venues that passed the category gate
    When event targeting runs with the default settings
    Then no model call is made
    And no external API call is made

  Scenario: Resolve the event candidates targeting mode to the confirmed set
    Given 3 venues with the tier "evidence_confirmed"
    And 4 venues with the tier "category_candidate"
    When a run is targeted with the eligibility mode "event_candidates"
    Then exactly the 3 confirmed venues are selected

  Scenario: Apply the venue cap on top of the event candidates mode
    Given 5 venues with the tier "evidence_confirmed"
    And the run caps venues at 2
    When a run is targeted with the eligibility mode "event_candidates"
    Then 2 venues are selected

  Scenario: Skip already evaluated venues unless recompute is requested
    Given a venue already recorded with the tier "evidence_confirmed"
    When event targeting runs without recompute
    Then that venue is not re-evaluated
    When event targeting runs with recompute
    Then that venue is re-evaluated

  Scenario: Report the selection without writing under dry run
    Given 5 venues that passed the category gate
    When event targeting runs as a dry run
    Then the tier changes it would make are reported
    And no venue event profile is written
