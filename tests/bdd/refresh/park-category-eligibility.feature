@wip
Feature: PARK category resolution and eligibility for praças and urban parks
  As the venue serving pipeline
  I want plaza, city park, park, and historic landmark venues to resolve to a
  first-class PARK category and pass the eligibility filter
  So that vibe modes can target public open-air social spaces instead of
  hiding them under OTHER

  Scenario: Plaza-typed venue resolves to PARK and is eligible
    Given a venue named "Praça do Arsenal" with Google type "plaza"
    When the venue is evaluated for category and eligibility
    Then the resolved category must be "PARK"
    And the venue must be eligible for serving

  Scenario: Park-typed venue with an ambiguous name keyword is not rejected
    Given a venue named "Parque das Esculturas" with Google type "park"
    When the venue is evaluated for category and eligibility
    Then the resolved category must be "PARK"
    And the venue must be eligible for serving
    And the venue must not be rejected with reason "ineligible_name_keyword"

  Scenario: Garden-typed venue still resolves to OTHER and remains blocked
    Given a venue named "Jardim Botânico do Recife" with Google type "garden"
    When the venue is evaluated for category and eligibility
    Then the resolved category must be "OTHER"
    And the venue must be rejected with reason "ineligible_google_type"

  Scenario: National-park-typed venue remains blocked
    Given a venue named "Parque Nacional do Catimbau" with Google type "national_park"
    When the venue is evaluated for category and eligibility
    Then the resolved category must be "OTHER"
    And the venue must be rejected with reason "ineligible_google_type"

  Scenario: CITY_PARK BestTime-typed venue with no Google type resolves to PARK and is eligible
    Given a venue named "Parque da Jaqueira" with BestTime type "CITY_PARK" and no Google type
    When the venue is evaluated for category and eligibility
    Then the resolved category must be "PARK"
    And the venue must be eligible for serving

  Scenario: Historic-landmark-typed venue resolves to PARK
    Given a venue named "Pátio de São Pedro" with Google type "historical_landmark"
    When the venue is evaluated for category and eligibility
    Then the resolved category must be "PARK"
    And the venue must be eligible for serving

  Scenario: PARK category serves the Ao Ar Livre display tokens
    Given a venue named "Praça do Arsenal" with Google type "plaza"
    When the venue display is resolved
    Then the venue type label must be "Ao Ar Livre"
    And the venue type emoji must be "🌳"
    And the venue type color must be "#16A34A"
