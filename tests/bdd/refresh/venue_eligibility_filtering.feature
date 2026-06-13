Feature: Admin-tunable eligibility configuration
  Operators read and tune the eligibility block-list (admin.eligibility_rule)
  without a redeploy. Tuning changes which venues the serving view
  (serving.eligible_venue) returns on the next projection — it never soft-deletes
  or changes a venue's lifecycle. The serving-view effect of a rule change (both
  directions) is covered by eligibility-serving-view.feature; this feature covers
  the admin read/validate surface that stays after the destructive sweep + the
  serve-time eligibility filter were retired.

  Background:
    Given the eligibility filter uses the default blocked types, blocked Google types, and blocked name keywords

  Scenario: Operators read the active eligibility configuration
    When an operator requests the eligibility configuration
    Then the response returns the active blocked types, blocked Google types, and blocked name keywords

  Scenario: Invalid eligibility configuration is rejected and the active filter is unchanged
    When an operator submits an eligibility configuration with a non-list blocked-types value
    Then the update is rejected with a validation error
    And the active eligibility configuration is unchanged
