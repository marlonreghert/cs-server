@wip
Feature: Erasing a user removes every trace of their engagement data
  Apple requires an in-app account deletion that actually deletes. Given a raw
  user id, cs-server must erase that user's rows from the system of record and
  remove them from the engagement projections it owns — including their
  membership in every hot-likes set, which is keyed by venue and so cannot be
  found from Redis alone. The call must be safe to retry and must never touch
  another user or any venue.

  Background:
    Given the venues "Bar Alfa" and "Bar Beta" exist and are servable
    And the user "user-a" has favorited "Bar Alfa" and "Bar Beta"
    And the user "user-a" has hot-liked "Bar Alfa"
    And the user "user-a" has recorded app sessions
    And the user "user-b" has favorited "Bar Alfa"
    And the user "user-b" has hot-liked "Bar Alfa"

  Scenario: A user is fully erased from the system of record and the projections
    When the engagement data for "user-a" is deleted
    Then no favorites rows remain for "user-a"
    And no hot-like event rows remain for "user-a"
    And no app session rows remain for "user-a"
    And the favorites projection for "user-a" is absent
    And "user-a" is not a member of the hot-likes set for "Bar Alfa"

  Scenario: The hot-like count drops by exactly one and other members survive
    When the engagement data for "user-a" is deleted
    Then the hot-likes count for "Bar Alfa" decreases by 1
    And "user-b" is still a member of the hot-likes set for "Bar Alfa"

  Scenario: A user hot-liking one venue across several days is removed once
    Given the user "user-a" has hot-liked "Bar Beta" on 3 different days
    When the engagement data for "user-a" is deleted
    Then no hot-like event rows remain for "user-a"
    And "user-a" is not a member of the hot-likes set for "Bar Beta"
    And the hot-likes count for "Bar Beta" decreases by 1

  Scenario: Deleting the same user twice succeeds and reports nothing removed
    When the engagement data for "user-a" is deleted
    And the engagement data for "user-a" is deleted again
    Then the second deletion succeeds
    And the second deletion reports zero removals

  Scenario: Deleting a user who never existed succeeds
    When the engagement data for "ghost-user" is deleted
    Then the deletion succeeds
    And the deletion reports zero removals

  Scenario: Another user's data is completely unaffected
    When the engagement data for "user-a" is deleted
    Then the favorites rows for "user-b" are unchanged
    And the hot-like event rows for "user-b" are unchanged
    And the favorites projection for "user-b" is unchanged

  Scenario: Erasure hard-deletes favorites rather than soft-deleting them
    When the engagement data for "user-a" is deleted
    Then no favorites row bearing the pseudonym for "user-a" survives in any state

  Scenario: No venue data is touched by a user deletion
    When the engagement data for "user-a" is deleted
    Then the venues "Bar Alfa" and "Bar Beta" are still servable
    And the stored venue rows and enrichment records are unchanged

  Scenario Outline: An invalid user id is rejected and deletes nothing
    When the engagement data for <user_id> is deleted
    Then the request is rejected as invalid
    And the favorites rows for "user-b" are unchanged

    Examples:
      | user_id  |
      | ""       |
      | missing  |

  Scenario: A projection failure after the row purge is reported and converges on retry
    Given the engagement projection write will fail
    When the engagement data for "user-a" is deleted
    Then the deletion reports failure
    When the engagement projection write recovers
    And the engagement data for "user-a" is deleted again
    Then no favorites rows remain for "user-a"
    And "user-a" is not a member of the hot-likes set for "Bar Alfa"

  Scenario: A deletion never logs the raw user id
    When the engagement data for "user-a" is deleted
    Then no emitted log record contains the raw id "user-a"
