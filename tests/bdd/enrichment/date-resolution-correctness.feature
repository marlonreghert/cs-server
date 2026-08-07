@wip
Feature: Date resolution correctness
  As the operator of the event pipeline
  I want a weekday to corroborate an explicit date, never to replace one
  So that a flyer saying "Sábado • 05/SET" is read as 5 September, and a date we
  cannot read is left visibly blank instead of quietly guessed

  Scenario: Resolve a day with an abbreviated month
    Given a flyer date of "05/SET" on a post from August 2026
    When the event date is resolved
    Then the event starts on 5 September 2026

  Scenario: Resolve an abbreviated month written before the day
    Given a flyer date of "SET 05" on a post from August 2026
    When the event date is resolved
    Then the event starts on 5 September 2026

  Scenario: Resolve an abbreviated month regardless of case and accents
    Given a flyer date of "05/set" on a post from August 2026
    When the event date is resolved
    Then the event starts on 5 September 2026

  Scenario: Read an explicit date rather than the weekday beside it
    Given a flyer date of "Sábado • 05/SET" on a post from August 2026
    When the event date is resolved
    Then the event starts on 5 September 2026
    And the event is not dated from the weekday alone

  Scenario: Refuse to guess a date from a weekday when a day number is present
    Given a flyer date of "sexta 15" on a post from August 2026
    When the event date is resolved
    Then the event has no start date
    And the event is flagged for review

  Scenario: Refuse to guess when an unreadable month leaves a bare day number
    Given a flyer date of "Domingo • 20/DEZEMBRÃO" on a post from January 2026
    When the event date is resolved
    Then the event has no start date
    And the event is flagged for review

  Scenario: Keep resolving a bare weekday with no competing day number
    Given a flyer date of "este sábado" on a post from August 2026
    When the event date is resolved
    Then the event starts on the Saturday after the post

  Scenario: Keep resolving a recurring weekday announcement
    Given a flyer date of "toda quinta" on a post from August 2026
    When the event date is resolved
    Then the event is marked recurring
    And the event starts on the Thursday after the post

  Scenario: Flag a weekday that disagrees with the explicit date
    Given a flyer date whose weekday does not fall on its explicit date
    When the event date is resolved
    Then the event starts on the explicit date
    And the event is flagged with the reason "weekday_mismatch"

  Scenario: Never read a month abbreviation out of ordinary prose
    Given a caption mentioning "set" with no day number beside it
    When the event date is resolved
    Then the event has no start date

  Scenario: Show a failed venue-post extraction in the review queue
    Given a venue-post event whose extraction failed
    When the review queue is requested
    Then that event is in the queue

  Scenario: Show a failed promoter-post extraction in the review queue
    Given a promoter-post event whose extraction failed
    When the review queue is requested
    Then that event is in the queue
