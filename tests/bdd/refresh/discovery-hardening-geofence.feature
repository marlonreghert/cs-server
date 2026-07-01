@wip
Feature: Discovery/startup hardening and Recife-metro geo-fence eligibility
  As the venue platform
  I must never run pipelines on startup, never trigger venue-filter discovery,
  and serve only venues inside the allowed Recife/Olinda region,
  so the catalog stays clean and the scarce BestTime quota is not spent on
  out-of-region venues.

  Background:
    Given the venue platform is configured with the default Recife/Olinda geo-fence box

  # 1 — No pipeline runs on startup
  Scenario: Startup runs no pipeline even when every on-startup flag is set
    Given every "*_on_startup" flag is set to true
    When the application starts up
    Then no venue discovery, refresh, or enrichment pipeline is executed
    And the server serves the already-cached venues
    And a log states that no pipelines run on startup by design

  Scenario: Scheduled cron and admin triggers still run their pipelines
    When a scheduled refresh job fires
    Then its pipeline executes normally
    And triggering an enabled job from the admin panel executes that pipeline

  # 2 — Discovery is dormant (no reachable trigger)
  Scenario: The venue-catalog discovery job cannot be triggered from the admin panel
    When the admin panel triggers the "venue_catalog" job
    Then the request is rejected as an unknown job
    And no GET /venues/filter call is made to BestTime
    And other admin-triggerable jobs remain available

  Scenario: Discovery keeps no configured discovery points
    Given the discovery points admin config is empty or absent
    Then the venue-filter discovery has no locations to query

  # 3 — Recife-metro geo-fence eligibility
  Scenario: A venue outside the Recife box is excluded from serving
    Given an active venue whose coordinates fall outside the Recife box
    When the serving projection is rebuilt
    Then the venue is absent from the eligible serving set
    And the venue is not counted toward the priority refresh budget
    And the venue row remains in the system of record (not deleted)

  Scenario: A venue inside the Recife box remains eligible
    Given an active venue located in Olinda inside the Recife box
    When the serving projection is rebuilt
    Then the venue is present in the eligible serving set

  Scenario: A venue with no coordinates is not geo-excluded
    Given an active venue that has no stored coordinates
    When eligibility is evaluated
    Then the venue is not excluded by the geo-fence
    And the geo-fence exclusion is reversible and never soft-deletes the venue

  Scenario: Editing the geo-fence box re-includes a venue that now falls inside
    Given a venue previously excluded because it was outside the box
    When an operator widens the geo-fence box to include the venue
    And the serving projection is rebuilt
    Then the venue becomes present in the eligible serving set

  Scenario: An invalid geo-fence update is rejected and leaves the box unchanged
    When an operator submits a geo-fence box with min latitude greater than max latitude
    Then the update is rejected
    And the active geo-fence box is unchanged

  Scenario: Serving-view and code eligibility agree on the geo dimension
    Given a mix of venues inside and outside the Recife box
    When eligibility is computed by the serving view and by the code evaluator
    Then both classify exactly the same venues as eligible
