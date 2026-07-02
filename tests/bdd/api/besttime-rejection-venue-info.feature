@wip
Feature: BestTime rejections with partial venue info take the rejection path
  As the venue platform
  I must treat a BestTime create rejection whose venue info carries no venue id
  as a rejection — surfacing BestTime's message and trying the geo fallback —
  so operators see why a venue cannot be added instead of a fake parse error.

  Scenario: A rejection with idless venue info surfaces BestTime's message
    Given BestTime rejects a create with an explanatory message and a venue info block without a venue id
    And no nearby venue matches in the geo fallback
    When an operator adds the venue by name and address
    Then the add fails as a rejection, not as an unparseable response
    And the error response carries BestTime's message
    And the geo fallback was attempted
    And the reserved quota slot is released

  Scenario: The same rejection with a nearby match completes via geo fallback
    Given BestTime rejects a create with a venue info block without a venue id
    And a nearby venue matches in the geo fallback
    When an operator adds the venue by name and address
    Then the add completes as matched via geo fallback

  Scenario: A body with no usable status still classifies as a bad response
    Given BestTime replies with a body that has no usable status or venue info
    When an operator adds the venue
    Then the add fails with the bad-response error
