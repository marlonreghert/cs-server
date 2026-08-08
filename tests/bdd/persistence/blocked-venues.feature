@wip
Feature: Per-user venue blocking is mutually exclusive with favorites
  As the engagement system of record, cs-server must let a user block a venue
  so it stops appearing in their feed, and unblock it later. Blocking and
  favoriting the same venue are mutually exclusive: blocking a venue removes
  any existing favorite for that user atomically, and unblocking never
  restores it. Blocks persist in RDS as the source of truth and project to
  Redis immediately, the same way favorites already do.

  Background:
    Given the venues "Bar Alfa" and "Bar Beta" exist and are servable

  Scenario: Blocking a venue that is not favorited persists the block and projects it
    When user "user-a" blocks venue "Bar Alfa" through the engagement API
    Then RDS holds an active block for user "user-a" on "Bar Alfa"
    And Redis holds the block so vibes_bot can read it
    And the block response reports that no favorite was removed

  Scenario: Blocking a favorited venue atomically removes the favorite
    Given the user "user-a" has favorited "Bar Alfa"
    When user "user-a" blocks venue "Bar Alfa" through the engagement API
    Then RDS holds an active block for user "user-a" on "Bar Alfa"
    And RDS no longer holds an active favorite for "user-a" on "Bar Alfa"
    And the favorites projection for "user-a" no longer includes "Bar Alfa"
    And the block response reports that a favorite was removed

  Scenario: Unblocking a venue never restores a favorite that was removed
    Given the user "user-a" has favorited "Bar Alfa"
    And user "user-a" blocks venue "Bar Alfa" through the engagement API
    When user "user-a" unblocks venue "Bar Alfa" through the engagement API
    Then RDS no longer holds an active block for user "user-a" on "Bar Alfa"
    And RDS still does not hold an active favorite for "user-a" on "Bar Alfa"

  Scenario: Unblocking a venue that was never blocked succeeds and changes nothing
    When user "user-a" unblocks venue "Bar Alfa" through the engagement API
    Then RDS does not hold an active block for user "user-a" on "Bar Alfa"

  Scenario: Blocking the same venue twice is idempotent
    Given user "user-a" blocks venue "Bar Alfa" through the engagement API
    When user "user-a" blocks venue "Bar Alfa" through the engagement API again
    Then RDS holds exactly one block row for user "user-a" on "Bar Alfa"
    And the block is still active

  Scenario: Blocking one venue does not affect another user's blocks or favorites
    Given the user "user-b" has favorited "Bar Alfa"
    When user "user-a" blocks venue "Bar Alfa" through the engagement API
    Then RDS still holds an active favorite for "user-b" on "Bar Alfa"
    And RDS does not hold an active block for user "user-b" on "Bar Alfa"

  Scenario: Erasing a user's engagement data purges their blocked-venue rows
    Given the user "user-a" has favorited "Bar Alfa"
    And user "user-a" blocks venue "Bar Alfa" through the engagement API
    And the user "user-b" has favorited "Bar Beta"
    And user "user-b" blocks venue "Bar Beta" through the engagement API
    When the engagement data for "user-a" is deleted
    Then no blocked-venue rows remain for "user-a"
    And the blocked-venues projection for "user-a" is absent
    And the deletion reports the number of blocked venues removed
    And RDS still holds an active block for user "user-b" on "Bar Beta"

  Scenario Outline: Blocking or unblocking with a missing id is rejected and changes nothing
    When user <user_id> blocks venue <venue_id> through the engagement API
    Then the request is rejected as invalid
    And RDS does not hold an active block for user "user-a" on "Bar Alfa"

    Examples:
      | user_id  | venue_id    |
      | ""       | "Bar Alfa"  |
      | "user-a" | ""          |
