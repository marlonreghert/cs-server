@wip
Feature: The cascade scores without a working probe
  As the operator of the Instagram handle pipeline
  I want candidates judged on the evidence that can actually be collected
  So that an unreachable verification step does not reject every venue

  Background:
    Given the venue is named "Bar do Cuscuz"
    And the venue's own Google listing links "https://instagram.com/bardocuscuzrecife"

  Scenario: Accept a handle from the venue's own listing when the probe is blocked
    Given Instagram refuses to answer existence checks
    When the cascade discovers the handle
    Then the cascade accepts the handle
    And the stored record has the status "found"

  Scenario: Accept the handle when existence is merely unknown
    Given the existence check could not be completed
    When the cascade discovers the handle
    Then the cascade accepts the handle

  Scenario: Reject the handle when the profile is confirmed absent
    Given the existence check confirms the profile does not exist
    When the cascade discovers the handle
    Then the cascade does not accept the handle

  Scenario: Hold the full bar when the probe works
    Given the existence check confirms the profile exists
    When the cascade discovers the handle
    Then the cascade accepts the handle
    And the acceptance bar was not lowered

  Scenario: Compare the venue name against the handle when no display name exists
    Given Instagram refuses to answer existence checks
    When the cascade discovers the handle
    Then the recorded name similarity is above 0.5

  Scenario: Prefer a real display name over the handle
    Given the existence check returns the display name "Bar do Cuscuz"
    When the cascade discovers the handle
    Then the recorded name similarity is above 0.9

  Scenario: Record that existence could not be checked
    Given Instagram refuses to answer existence checks
    When the cascade discovers the handle
    Then the stored record shows the acceptance bar was lowered

  Scenario: Keep an unverified paid-search candidate below the bar
    Given the venue's own Google listing links nothing
    And the paid search proposes the handle "algumbarqualquer"
    And Instagram refuses to answer existence checks
    When the cascade discovers the handle
    Then the cascade does not accept the handle
