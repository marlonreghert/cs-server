@wip
Feature: Bounded refresh targets served venues by priority
  As the refresh pipeline
  I want the bounded BestTime refresh to select served (eligible) venues by priority
  So that the scarce monthly budget improves live-data coverage of venues users see

  Background:
    Given a refresh budget of 3 venues
    And the following venues exist:
      | venue_id | lifecycle | eligible | priority |
      | a        | active    | yes      | 0        |
      | b        | active    | yes      | 1        |
      | c        | active    | yes      | 2        |
      | d        | active    | yes      | 3        |
      | e        | active    | no       | 0        |
      | f        | deprecated| no       | 0        |

  Scenario: Only served venues are selected, ordered by priority, up to the budget
    When the bounded refresh selects venues
    Then the selection is "a, b, c"
    And the selection excludes "d"

  Scenario: An active-but-not-eligible venue is never selected
    When the bounded refresh selects venues
    Then the selection excludes "e"

  Scenario: A deprecated venue is never selected
    When the bounded refresh selects venues
    Then the selection excludes "f"

  Scenario: Selection is capped at the refresh budget
    When the bounded refresh selects venues
    Then the selection contains 3 venues

  Scenario: The monthly cap gate is unchanged for selected served venues
    Given the monthly unique-venue cap is reached
    And venue "a" was already touched this month
    And venue "b" was not yet touched this month
    When the bounded refresh runs
    Then a BestTime read is attempted for "a"
    And the BestTime read for "b" is skipped due to the monthly cap

  Scenario: A serving-view read failure skips the cycle instead of refreshing all active venues
    Given the serving view read fails
    When the bounded refresh runs
    Then no venues are refreshed
    And the refresh does not fall back to the full active set
