Feature: User activity tracking and counts
  As the system of record
  I want to record each authenticated app session pseudonymized in RDS
  and expose distinct-user counts
  So that the admin dashboard can report real usage instead of only favorites

  Background:
    Given a clean engagement activity store

  Scenario: Recording a session makes the user appear in counts
    When a session is recorded for user "uid-alice"
    And the admin requests user activity counts
    Then total_users is 1
    And active_1d is 1

  Scenario: Repeat sessions for the same user on the same day count once
    When a session is recorded for user "uid-alice"
    And a session is recorded for user "uid-alice"
    And the admin requests user activity counts
    Then total_users is 1
    And active_1d is 1

  Scenario: Distinct users on the same day are all counted
    When a session is recorded for user "uid-alice"
    And a session is recorded for user "uid-bob"
    And the admin requests user activity counts
    Then total_users is 2
    And active_1d is 2

  Scenario: Active windows reflect when each user was last active
    Given user "uid-alice" had a session 10 days ago
    And user "uid-bob" had a session today
    When the admin requests user activity counts
    Then active_1d is 1
    And active_7d is 1
    And active_30d is 2
    And total_users is 2

  Scenario: The stored identifier is pseudonymized, never the raw user id
    When a session is recorded for user "uid-alice"
    Then the engagement activity store holds no row equal to "uid-alice"
    And it holds one pseudonymized activity row for today

  Scenario: Recording a session with a missing user id is rejected
    When a session is recorded with no user id
    Then the response status is 422
