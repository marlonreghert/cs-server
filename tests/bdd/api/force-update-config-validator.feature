Feature: Force-update policy config validation

  The force_update admin config key defines, per platform, the minimum supported
  and minimum recommended app versions plus the store URL and optional messages.
  cs-server validates the policy before persisting it to the system of record and
  mirroring it to the Redis serving key, so a malformed policy can never reach the
  serving path that gates the app.

  Scenario: A valid two-platform policy is accepted and persisted
    Given a force_update policy with valid ios and android version floors
    When an admin writes it to the force_update config key
    Then the write succeeds
    And the policy is stored in the admin config system of record
    And the policy is mirrored to the force_update Redis serving key

  Scenario: A policy with an invalid version string is rejected
    Given a force_update policy whose ios min_supported_version is "2.x"
    When an admin writes it to the force_update config key
    Then the write is rejected as invalid
    And nothing is persisted for the force_update key

  Scenario: A policy whose supported floor exceeds its recommended floor is rejected
    Given a force_update policy where ios min_supported_version is above its min_recommended_version
    When an admin writes it to the force_update config key
    Then the write is rejected as invalid

  Scenario: A policy missing the store URL is rejected
    Given a force_update policy whose android block has no store_url
    When an admin writes it to the force_update config key
    Then the write is rejected as invalid

  Scenario: A policy with a non-https store URL is rejected
    Given a force_update policy whose ios store_url is not https
    When an admin writes it to the force_update config key
    Then the write is rejected as invalid

  Scenario: A policy with an unknown platform key is rejected
    Given a force_update policy that includes a "web" platform block
    When an admin writes it to the force_update config key
    Then the write is rejected as invalid

  Scenario: Deleting the policy clears the record and the mirror
    Given a stored force_update policy
    When an admin deletes the force_update config key
    Then the record is removed from the system of record
    And the force_update Redis serving key is removed
