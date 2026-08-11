@wip
Feature: Control add-time Instagram discovery tiers from admin config

  As the operator who owns the budget
  I want the Google-search tier and the LLM judge switchable from admin config
  So that I can start or stop that spend from the panel and have it take effect
  on the next add, without a redeploy or a restart

  Background:
    Given the add-venue endpoint is configured
    And Instagram discovery at add time is enabled
    And the Google-search credential and the judge credential are both present

  Scenario: Fall back to the deploy settings when the key was never set
    Given no "instagram_discovery" admin config is stored
    And the setting for the Google-search tier is enabled
    And the setting for the judge is enabled
    When I add a venue with no Instagram on any free source
    Then the cascade attempts the "google_search" source
    And the judge is consulted

  Scenario: Enable the Google-search tier from admin config alone
    Given the setting for the Google-search tier is disabled
    And "instagram_discovery" admin config sets "google_search_enabled" to true
    When I add a venue with no Instagram on any free source
    Then the cascade attempts the "google_search" source

  Scenario: Let an explicit admin false beat an enabled setting
    Given the setting for the Google-search tier is enabled
    And "instagram_discovery" admin config sets "google_search_enabled" to false
    When I add a venue with no Instagram on any free source
    Then the cascade does not attempt the "google_search" source
    And the venue is still created

  Scenario: Enable the judge from admin config alone
    Given the setting for the judge is disabled
    And "instagram_discovery" admin config sets "judge_enabled" to true
    And the Google-search tier returns a candidate for the venue
    When I add a venue with no Instagram on any free source
    Then the judge is consulted

  Scenario: Let an explicit admin false disable the judge
    Given the setting for the judge is enabled
    And "instagram_discovery" admin config sets "judge_enabled" to false
    And the Google-search tier returns a candidate for the venue
    When I add a venue with no Instagram on any free source
    Then the judge is not consulted
    And the venue is still created

  Scenario: Take effect on the next add without a restart
    Given "instagram_discovery" admin config sets "google_search_enabled" to false
    When I add a venue with no Instagram on any free source
    Then the cascade does not attempt the "google_search" source
    When "instagram_discovery" admin config sets "google_search_enabled" to true
    And I add another venue with no Instagram on any free source
    Then the cascade attempts the "google_search" source

  Scenario: Refuse to spend when the config cannot be read
    Given the admin config read fails
    When I add a venue with no Instagram on any free source
    Then the cascade does not attempt the "google_search" source
    And the judge is not consulted
    And the venue is still created

  Scenario: Never reach the paid Instagram user search through this key
    Given "instagram_discovery" admin config sets "google_search_enabled" to true
    And "instagram_discovery" admin config sets "judge_enabled" to true
    When I add a venue with no Instagram on any free source
    Then the cascade does not attempt the "apify_search" source

  Scenario: Keep the free tiers running whatever the flags say
    Given "instagram_discovery" admin config sets "google_search_enabled" to false
    And "instagram_discovery" admin config sets "judge_enabled" to false
    When I add a venue with no Instagram on any free source
    Then the cascade attempts the "google_website" source
    And the cascade attempts the "archived_gmaps_website" source
    And the cascade attempts the "venue_website" source

  Scenario: Leave the tier absent when its credential is missing
    Given the Google-search credential is not configured
    And "instagram_discovery" admin config sets "google_search_enabled" to true
    When I add a venue with no Instagram on any free source
    Then the cascade does not attempt the "google_search" source
    And the venue is still created

  Scenario: Reject a non-boolean value
    When an operator writes "instagram_discovery" with "google_search_enabled" set to "yes"
    Then the write is rejected
    And the stored "instagram_discovery" config is unchanged

  Scenario: Reject an unknown field
    When an operator writes "instagram_discovery" with an unknown field "apify_search_enabled"
    Then the write is rejected
    And the stored "instagram_discovery" config is unchanged

  Scenario: Accept a valid write and read it back
    When an operator writes "instagram_discovery" with "google_search_enabled" true and "judge_enabled" false
    Then the write succeeds
    And reading "instagram_discovery" returns "google_search_enabled" true and "judge_enabled" false
