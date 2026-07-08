Feature: Vibe modes admin config write validation
  The admin config endpoint must reject malformed vibe_modes payloads before
  any write, so the RDS truth and the Redis mirror only ever hold mode configs
  that every reader (vibes_bot serving, mobile rendering) is guaranteed to
  handle. Validation failures must name the offending mode and field and must
  leave the stored config untouched.

  Background:
    Given the admin config service is wired with the vibe_modes validator
    And a well-formed vibe_modes array is currently stored

  Scenario: Well-formed modes array is accepted and round-trips
    When the admin PUTs a vibe_modes array where every mode has a unique id, label, emoji, description, is_default, enabled, busyness_range, sort_strategy, affinity, and a complete filter
    Then the response status is 200
    And the stored vibe_modes value equals the submitted array
    And GET /admin/config/vibe_modes returns the submitted array

  Scenario: Mode missing filter.quality_gates is rejected
    When the admin PUTs a vibe_modes array where mode "role_calmo" has a filter without "quality_gates"
    Then the response status is 400
    And the error detail names mode "role_calmo" and field "filter.quality_gates"
    And the stored vibe_modes value is unchanged

  Scenario: Mode missing a required top-level field is rejected
    When the admin PUTs a vibe_modes array where mode "jantar" has no "busyness_range"
    Then the response status is 400
    And the error detail names mode "jantar" and field "busyness_range"
    And the stored vibe_modes value is unchanged

  Scenario: Unknown extra keys are preserved on a valid payload
    When the admin PUTs a valid vibe_modes array where one mode carries "requires_family_signal" inside its filter and "trajectory_weight" at the top level
    Then the response status is 200
    And the stored mode still contains "requires_family_signal" and "trajectory_weight" verbatim

  Scenario: Top-level object is rejected for vibe_modes
    When the admin PUTs a JSON object instead of an array to the vibe_modes key
    Then the response status is 400
    And the stored vibe_modes value is unchanged

  Scenario: Empty array is rejected
    When the admin PUTs an empty vibe_modes array
    Then the response status is 400
    And the stored vibe_modes value is unchanged

  Scenario: Duplicate mode ids are rejected
    When the admin PUTs a vibe_modes array containing two modes with id "explorar"
    Then the response status is 400
    And the error detail names the duplicated id "explorar"
    And the stored vibe_modes value is unchanged

  Scenario: All-disabled mode list is rejected
    When the admin PUTs a vibe_modes array where every mode has enabled set to false
    Then the response status is 400
    And the stored vibe_modes value is unchanged

  Scenario: More than one default mode is rejected
    When the admin PUTs a vibe_modes array where two modes have is_default set to true
    Then the response status is 400
    And the stored vibe_modes value is unchanged

  Scenario: Invalid busyness_range bounds are rejected
    When the admin PUTs a vibe_modes array where one mode has busyness_range [3, 1]
    Then the response status is 400
    And the stored vibe_modes value is unchanged

  Scenario: Unknown sort_strategy is rejected
    When the admin PUTs a vibe_modes array where one mode has sort_strategy "popularity_desc"
    Then the response status is 400
    And the stored vibe_modes value is unchanged

  Scenario: Malformed quality gate entry is rejected
    When the admin PUTs a vibe_modes array where one quality gate has no "min_rating"
    Then the response status is 400
    And the stored vibe_modes value is unchanged

  Scenario: Non-numeric trajectory_weight is rejected
    When the admin PUTs a vibe_modes array where one mode has a non-numeric "trajectory_weight"
    Then the response status is 400
    And the error detail names field "trajectory_weight"
    And the stored vibe_modes value is unchanged

  Scenario: Non-boolean requires_family_signal is rejected
    When the admin PUTs a vibe_modes array where one mode filter has a non-boolean "requires_family_signal"
    Then the response status is 400
    And the error detail names field "requires_family_signal"
    And the stored vibe_modes value is unchanged

  Scenario: A disabled default mode is rejected
    When the admin PUTs a vibe_modes array where the default mode has enabled set to false
    Then the response status is 400
    And the stored vibe_modes value is unchanged
