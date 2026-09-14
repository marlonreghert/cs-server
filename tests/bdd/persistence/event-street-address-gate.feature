@wip
Feature: Event street address gate
  As the VibeSense app
  I want an event served only when its venue's location is known at
  street level
  So that a confidently-linked event whose venue's own address is barely
  known never reaches the map with nothing real to show for "where"

  Background:
    Given the events projection is enabled
    And "event_require_street_address" is true
    And the current time is 2026-09-14 18:00 in Recife

  # ── the gate itself ──────────────────────────────────────────────────────

  Scenario: Serve an event whose venue has a street address on file
    Given "Casa Bacurau" is a servable venue in Recife with street "Rua do Sol, 100"
    And an accepted event at "Casa Bacurau" starting 2026-09-14 20:00
    When the events projection runs
    Then an occurrence is projected for that event

  Scenario: Withhold an event whose venue has no street address on file
    Given "Espaço Sem Nome" is a servable venue in Recife with no street on file
    And an accepted event at "Espaço Sem Nome" starting 2026-09-14 20:00
    When the events projection runs
    Then no occurrence is projected for that event

  Scenario: Withhold an event whose venue's street is blank, not merely absent
    Given "Espaço Em Branco" is a servable venue in Recife with street "   "
    And an accepted event at "Espaço Em Branco" starting 2026-09-14 20:00
    When the events projection runs
    Then no occurrence is projected for that event

  # ── the operator rollback ────────────────────────────────────────────────

  Scenario: Serve the same event once an operator disables the gate
    Given "Espaço Sem Nome" is a servable venue in Recife with no street on file
    And an accepted event at "Espaço Sem Nome" starting 2026-09-14 20:00
    When the events projection runs
    Then no occurrence is projected for that event
    When an admin sets "event_require_street_address" to false
    And the events projection runs
    Then an occurrence is projected for that event

  # ── nothing already working may regress ─────────────────────────────────

  Scenario Outline: Every existing exclusion still withholds an event, street or not
    Given "Casa Bacurau" is a servable venue in Recife with street "Rua do Sol, 100"
    And an event at "Casa Bacurau" starting 2026-09-14 20:00 that is <disqualifier>
    When the events projection runs
    Then no occurrence is projected for that event

    Examples:
      | disqualifier                      |
      | still pending review              |
      | rejected                          |
      | superseded by another event       |
      | of post type "menu"               |
      | linked to no venue                |
      | linked to a non-servable venue    |

  Scenario: A street-complete venue's event is unaffected by the new gate
    Given "Casa Bacurau" is a servable venue in Recife with street "Rua do Sol, 100"
    And an accepted event at "Casa Bacurau" starting 2026-09-14 20:00
    And the event has category "techno", price text "R$ 40", ticket info "Grátis até 23:30"
    When the events projection runs
    Then an occurrence is projected for that event
    And the occurrence payload contains the title, description, category, price text and ticket info
