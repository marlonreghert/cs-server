Feature: Recover Apify archive runs that outlive the poll budget
  As the venue platform operator
  I must recover a venue whose Apify actor run is still working when the poll
  budget runs out, and I must be told a timeout apart from a venue that does not
  exist, so that a slow scrape does not silently vanish from the archive and a
  run I already paid for is never bought twice.

  Background:
    Given the media archive is enabled with a configured bucket
    And the archive source is the real Apify Google Maps extractor
    And the venue "v-cuscuz" is in the catalog

  Scenario: A run that finishes inside the poll budget is archived unchanged
    Given the actor run for "v-cuscuz" reaches SUCCEEDED inside the poll budget
    When I archive the venue
    Then the venue must be archived
    And the venue outcome must be reported as "archived"
    And no continuation poll must be attempted
    And the call duration must be observed once

  Scenario: A run that finishes during the continuation window is recovered
    Given the actor run for "v-cuscuz" is still RUNNING when the poll budget ends
    And the actor run reaches SUCCEEDED during the continuation window
    When I archive the venue
    Then the venue must be archived
    And the venue outcome must be reported as "archived"
    And the venue outcome must not be reported as "timeout"
    And the archived photos must be stored under the same run prefix as a first-pass success
    And the log must record that the venue was recovered and how long it took

  Scenario: A run still working past the continuation window is reported as a timeout
    Given the actor run for "v-cuscuz" is still RUNNING when the poll budget ends
    And the actor run never reaches a terminal status
    When I archive the venue
    Then the venue outcome must be reported as "timeout"
    And the venue outcome must not be reported as "no_match"
    And the run summary must count 1 timeout
    And the venue must not be archived

  Scenario: A queued run that never starts is reported as a timeout and does not hang
    Given the actor run for "v-cuscuz" stays READY for the whole continuation window
    When I archive the venue
    Then the venue outcome must be reported as "timeout"
    And the archive run must finish rather than block indefinitely

  Scenario: A genuinely empty result is reported as no_result, not a timeout
    Given the actor run for "v-cuscuz" reaches SUCCEEDED with an empty dataset
    When I archive the venue
    Then the venue outcome must be reported as "no_result"
    And the venue outcome must not be reported as "timeout"

  Scenario: A timed-out venue must never start a second actor run
    Given the actor run for "v-cuscuz" is still RUNNING when the poll budget ends
    When I archive the venue
    Then exactly 1 actor run must have been started for the venue
    And the continuation must poll the run that was already started

  Scenario Outline: The timeout must record the last non-terminal status seen
    Given the actor run for "v-cuscuz" stays <status> for the whole continuation window
    When I archive the venue
    Then the timeout log line must name the last non-terminal status "<status>"
    And the timeout metric must carry the last non-terminal status "<status>"

    Examples:
      | status  |
      | READY   |
      | RUNNING |

  Scenario: A run that fails on Apify's side is not reported as a timeout
    Given the actor run for "v-cuscuz" reaches FAILED inside the poll budget
    When I archive the venue
    Then the venue outcome must not be reported as "timeout"
    And no continuation poll must be attempted

  Scenario: One venue's timeout must not end the run
    Given the venue "v-other" has the search query "Boteco Recife"
    And the actor run for "v-cuscuz" never reaches a terminal status
    And the actor run for "v-other" reaches SUCCEEDED inside the poll budget
    When I archive both venues
    Then the venue "v-other" must be archived
    And the run summary must count 1 timeout
    And the archive run must complete

  Scenario: Credit exhaustion during a continuation still aborts the whole run
    Given the actor run for "v-cuscuz" is still RUNNING when the poll budget ends
    And Apify reports credit exhaustion during the continuation window
    When I archive the venue
    Then the archive run must abort
    And no completion marker must be written
