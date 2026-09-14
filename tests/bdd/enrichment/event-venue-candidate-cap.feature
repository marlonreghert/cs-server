Feature: Cap ranked venue candidates at their real source
  As an operator reading the event-venue review queue
  I want a name-match resolution to keep only its best-ranked venue candidates
  instead of the whole servable catalog
  So that the review queue stays readable, a future advisory LLM call never
  receives a catalog-sized prompt, and no existing auto-link or queued
  decision changes

  A name-match resolution (rung 4 of the resolution ladder) scores every
  servable venue against an event's own location text and has always kept
  every venue that scored above zero — in production, up to 2,661 ranked
  rows for a single event against a 3,532-venue catalog. Nothing in the
  auto-link decision itself ever reads past the top two ranked candidates,
  so capping the stored list at a generous top-K changes what is stored and
  what is shown, never what is decided.

  Background:
    Given the candidate top-K is 20

  # ── the cap, and what it must never touch ──────────────────────────────

  Scenario: Keep only the top-K ranked candidates for a name-match resolution
    Given a venue catalog of 30 venues that all loosely match the location text "Espaco Teste Show"
    When an event's location text is "Espaco Teste Show" with no handle mention
    Then at most 20 ranked venue candidates are stored for that event
    And the stored candidates are ordered best score first

  Scenario: Never drop the winning candidate or its runner-up
    Given the candidate top-K is 2
    And a venue catalog of 30 venues that all loosely match the location text "Espaco Teste Show"
    When an event's location text is "Espaco Teste Show" with no handle mention
    Then exactly 2 ranked venue candidates are stored for that event

  Scenario: Reach the exact same auto-link verdict before and after capping
    Given a venue catalog of 30 venues that all loosely match the location text "Espaco Teste Show"
    And one of those venues is named exactly "Espaco Teste Show"
    When an event's location text is "Espaco Teste Show" with no handle mention
    Then the event auto-links to the venue named exactly "Espaco Teste Show"

  Scenario: Leave a handle-mention resolution untouched by the cap
    Given the candidate top-K is 2
    And the Instagram handle "knownvenue" maps to a venue named "Known Venue"
    When an event's location text is "@knownvenue" with no handle mention
    Then exactly 1 ranked venue candidate is stored for that event

  Scenario: Count a truncation only when the cap actually removes candidates
    Given a venue catalog of 30 venues that all loosely match the location text "Espaco Teste Show"
    When an event's location text is "Espaco Teste Show" with no handle mention
    Then a candidate-truncation is counted

  Scenario: Never count a truncation when the candidate set already fits
    Given a venue catalog of 3 venues that all loosely match the location text "Espaco Teste Show"
    When an event's location text is "Espaco Teste Show" with no handle mention
    Then no candidate-truncation is counted

  # ── the reader this plan exists for ────────────────────────────────────

  Scenario: The review queue returns at most the capped number of candidates
    Given a venue catalog of 30 venues that all loosely match the location text "Espaco Teste Show"
    And an event's location text is "Espaco Teste Show" with no handle mention
    When an operator requests the event review queue
    Then that event's entry lists at most 20 ranked venue candidates

  # ── the one-off repair for rows written before this cap shipped ───────

  Scenario: Report every oversized event without writing anything, before apply
    Given an event already stores 40 ranked candidates, above the configured top-K of 20
    When the candidate-cap backfill runs without apply
    Then the report names that event with its current count 40 and its would-be count 20
    And that event still stores 40 ranked candidates

  Scenario: Shrink an oversized event's stored candidates when applied
    Given an event already stores 40 ranked candidates, above the configured top-K of 20
    When the candidate-cap backfill runs with apply
    Then that event stores at most 20 ranked candidates
    And the event's venue_id, location_resolution, and review_reason are unchanged

  Scenario: Change nothing on a second backfill run
    Given an event already stores 40 ranked candidates, above the configured top-K of 20
    And the candidate-cap backfill has already applied once
    When the candidate-cap backfill runs with apply a second time
    Then no candidate row is inserted, updated, or deleted
