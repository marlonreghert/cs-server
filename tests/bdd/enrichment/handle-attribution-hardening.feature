@wip
Feature: Handle attribution hardening
  An @handle is an exact identifier: it either resolves to a venue we carry or
  it does not. Its characters must never be fuzzy-matched against a different
  venue's name, and an event naming a handle we do not carry must say so.

  Background:
    Given the venue "Seu Chico Botequim" at handle "seuchicobotequim"
    And the venue "Sempre Rock Bar" at handle "semprerockbar"
    And the venue "Maria Café" with no Instagram handle
    And the venue "Espaço Tucano" with no Instagram handle

  # ── an unknown handle is evidence, not a guess ─────────────────────────────

  Scenario: Refuse to link an event whose location text names an unknown handle
    Given an extracted event whose location text is "@mahalilacafe"
    When the event is attributed
    Then the event links to no venue

  Scenario: Record venue-not-in-catalog on that event
    Given an extracted event whose location text is "@mahalilacafe"
    When the event is attributed
    Then the stored event is queued for review with reason "venue_not_in_catalog"

  Scenario: Never link "@mahalilacafe" to "Maria Café"
    Given an extracted event whose location text is "@mahalilacafe"
    When the event is attributed
    Then the event does not link to "Maria Café"

  Scenario: Never link "@espaco.muta" to "Espaço Tucano"
    Given an extracted event whose location text is "@espaco.muta"
    When the event is attributed
    Then the event does not link to "Espaço Tucano"

  Scenario: Keep unresolved-venue for an event that names nothing at all
    Given an extracted event with no location text
    And its post caption names no known venue
    When the event is attributed
    Then the stored event is queued for review with reason "unresolved_venue"
    And the stored event is not queued for review with reason "venue_not_in_catalog"

  # ── genuine matching still works ───────────────────────────────────────────

  Scenario: Keep linking an event whose location text is a known handle
    Given an extracted event whose location text is "@seuchicobotequim"
    When the event is attributed
    Then the event links to "Seu Chico Botequim"

  Scenario: Keep linking an event whose location text is a real venue name
    Given an extracted event whose location text is "Sempre Rock Bar"
    When the event is attributed
    Then the event links to "Sempre Rock Bar"

  Scenario: Match on the place name left after a handle is stripped
    Given the venue "Ô Bar Restaurante - Ponta Negra" with no Instagram handle
    And an extracted event whose location text is "@obarpraia Ponta Negra"
    When the event is attributed
    Then the event links to "Ô Bar Restaurante - Ponta Negra"

  Scenario: Do not name-match when only a handle remains
    Given an extracted event whose location text is "@espaco.muta"
    When the event is attributed
    Then no name match is attempted for that event

  # ── nothing already working may regress ────────────────────────────────────

  Scenario: Keep the per-event location text ahead of a caption mention
    Given a promoter post whose caption mentions "@semprerockbar" first
    And an extracted event whose location text is "@seuchicobotequim"
    When the event is attributed
    Then the event links to "Seu Chico Botequim"

  Scenario: Keep the neighbourhood rung working for a multi-branch account
    Given a crawl target "beerdock_recife" mapped to "BeerDock Boa Viagem"
    And a sibling venue "BeerDock Casa Forte" in the Casa Forte neighbourhood
    And an extracted event whose location text is "CASA FORTE"
    When the event is attributed
    Then the event links to "BeerDock Casa Forte"

  Scenario: Keep every correct link from the production run
    Given the production events of the 2026-08-13 promoter crawl
    When the events are attributed
    Then "@seuchicobotequim" links to "Seu Chico Botequim"
    And "@semprerockbar" links to "Sempre Rock Bar"
    And "@tavernapubnatal" links to "Taverna Pub Medieval Bar & Avalon Events"
    And "@obarpontanegra" links to "Ô Bar Restaurante - Ponta Negra"
    And "@bar54_" links to "Bar 54"
