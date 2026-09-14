@wip
Feature: Admin config PUT accepts a bare boolean body
  Several admin-config keys are boolean flags whose validators require a
  native Python bool (isinstance(value, bool)). The generic
  PUT /admin/config/{key} route must accept a bare JSON true/false body for
  those keys, exactly as the events-venue-night-repair runbook documents
  (`PUT /admin/config/event_dedup_single_night_default_enabled body: true`),
  and must keep accepting a JSON object body for object-typed keys and a JSON
  array body for array-typed keys unchanged.

  Background:
    Given the admin config service is wired with its production validators

  Scenario: Bare true body sets a boolean-typed key
    When the admin PUTs a bare boolean true to the "event_dedup_handle_time_match_enabled" config key
    Then the response status is 200
    And the response value is the boolean true
    And GET /admin/config/event_dedup_handle_time_match_enabled returns the boolean true

  Scenario: Bare false body sets a boolean-typed key
    Given the "event_dedup_handle_time_match_enabled" config key is currently true
    When the admin PUTs a bare boolean false to the "event_dedup_handle_time_match_enabled" config key
    Then the response status is 200
    And the response value is the boolean false
    And GET /admin/config/event_dedup_handle_time_match_enabled returns the boolean false

  Scenario: A dict-typed key still accepts a JSON object body unchanged
    When the admin PUTs a well-formed category map object to the "venue_category_map" config key
    Then the response status is 200
    And the stored venue_category_map value equals the submitted object

  Scenario: A list-typed key still accepts a JSON array body unchanged
    When the admin PUTs a well-formed vibe_modes array to the "vibe_modes" config key
    Then the response status is 200
    And the stored vibe_modes value equals the submitted array

  Scenario: A bare boolean body is rejected for a dict-typed key
    When the admin PUTs a bare boolean true to the "venue_category_map" config key
    Then the response status is 400
    And the stored venue_category_map value is unchanged
