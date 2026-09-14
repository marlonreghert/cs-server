Feature: Merge same-handle posts about the exact same date and time, regardless of title
  As an operator of the event pipeline
  I want several posts from one venue's own Instagram handle, about the exact
  same date and time, to collapse into one listing even when their titles and
  lineups share no distinctive words
  So that a venue that reposts one show three different ways shows one card,
  while a venue that genuinely runs two different programmed events on the
  same day at different times still shows two

  This is a THIRD, independent auto-merge condition alongside title
  containment and shared lineup — neither of those rules is changed, and
  both keep running for every pair this new rule does not reach. It is
  gated off by default; with the flag disabled, production behaviour is
  byte-identical to today's. Both sides must carry a KNOWN time — two posts
  with an unset time never match on this rule alone, v1's deliberately
  narrowed scope (see the plan's Open Questions).

  The Boteco Nem A Pau Juvenal and Casa de Jorge Amado groups below are
  real, live production rows, re-pulled fresh on 2026-09-13 for the plan
  this feature implements
  (plans/260913_candidate-cap-and-handle-time-merge.md) — not hypothetical.

  Background:
    Given the candidate window is 8 hours

  # ── the real positive case: Boteco Nem A Pau Juvenal ───────────────────

  Scenario: Merge three same-handle posts about one show at the exact same date and time
    Given the handle-time-match rule is enabled
    And the handle "boteconemapaujuvenal" posted "Domingo é dia de Boteco!" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And the same handle posted "Show de @alexcarrerasoficial" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And the same handle posted "ALEX CARRERAS E BANDA" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    When the merge pass runs
    Then the three posts collapse into one surviving event
    And the merge is recorded with the handle-time-match reason

  Scenario: Leave the three posts unmerged while the rule is disabled
    Given the handle-time-match rule is disabled
    And the handle "boteconemapaujuvenal" posted "Domingo é dia de Boteco!" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And the same handle posted "Show de @alexcarrerasoficial" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And the same handle posted "ALEX CARRERAS E BANDA" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    When the merge pass runs
    Then all three posts remain as separate events

  # ── the real negative case: Casa de Jorge Amado ────────────────────────

  Scenario: Refuse to merge two same-handle posts on the same day at different known times
    Given the handle-time-match rule is enabled
    And the handle "teatrojorgeamado" posted "PETER PAN" at "Casa de Jorge Amado" starting 2026-09-13 14:00, with a known time and no attribution dispute
    And the same handle posted "CHAPEUZINHO VERMELHO" at "Casa de Jorge Amado" starting 2026-09-13 19:00, with a known time and no attribution dispute
    When the merge pass runs
    Then both posts remain as separate events

  # ── the structural guards found while investigating the real data ──────

  Scenario: Refuse to merge two posts from different handles at the exact same time
    Given the handle-time-match rule is enabled
    And the handle "handle_a" posted "First Show" at "Some Venue" starting 2026-09-13 20:00, with a known time and no attribution dispute
    And the handle "handle_b" posted "Second Show" at "Some Venue" starting 2026-09-13 20:00, with a known time and no attribution dispute
    When the merge pass runs
    Then both posts remain as separate events

  Scenario: Refuse to merge a post that already went through the venue-resolution ladder
    Given the handle-time-match rule is enabled
    And the promoter handle "oquetemhojeemnatal" posted "Gustavo Cocentino" at "Some Roundup Venue" starting 2026-09-12 19:00, with a known time, already linked by a handle mention
    And the same promoter handle posted "Festival CAVE" at "Some Roundup Venue" starting 2026-09-12 20:00, with a known time, already linked by a handle mention
    When the merge pass runs
    Then both posts remain as separate events

  Scenario: Refuse to merge a post whose own text disputes its mapped venue
    Given the handle-time-match rule is enabled
    And the handle "disputed_handle" posted "First Show" at "Some Venue" starting 2026-09-13 21:00, with a known time and no attribution dispute
    And the same handle posted "Second Show" at "Some Venue" starting 2026-09-13 21:00, with a known time and a location-text attribution dispute against the mapped venue
    When the merge pass runs
    Then both posts remain as separate events

  Scenario: Refuse to merge when one side's time is known and the other's is not
    Given the handle-time-match rule is enabled
    And the handle "mixed_time_handle" posted "First Show" at "Some Venue" starting 2026-09-13 21:00, with a known time and no attribution dispute
    And the same handle posted "Second Show" at "Some Venue" on 2026-09-13, with an unknown time and no attribution dispute
    When the merge pass runs
    Then both posts remain as separate events

  Scenario: Refuse to merge when neither side's time is known
    Given the handle-time-match rule is enabled
    And the handle "unknown_time_handle" posted "First Show" at "Some Venue" on 2026-09-13, with an unknown time and no attribution dispute
    And the same handle posted "Second Show" at "Some Venue" on 2026-09-13, with an unknown time and no attribution dispute
    When the merge pass runs
    Then both posts remain as separate events

  Scenario: Never bypass an operator's own title correction
    Given the handle-time-match rule is enabled
    And the handle "boteconemapaujuvenal" posted "Domingo é dia de Boteco!" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And the same handle posted "ALEX CARRERAS E BANDA" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And an operator has edited the title of the most recently posted event
    When the merge pass runs
    Then both posts remain as separate events
    And the pair is recorded as a suggestion instead

  Scenario: Count the handle-time-match merge separately from a title or lineup merge
    Given the handle-time-match rule is enabled
    And the handle "boteconemapaujuvenal" posted "Domingo é dia de Boteco!" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And the same handle posted "Show de @alexcarrerasoficial" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    When the merge pass runs
    Then the merge is counted under its own handle-time-match outcome, not the plain merged outcome

  Scenario: Still merge via title containment when handle-time-match also holds
    Given the handle-time-match rule is enabled
    And the handle "both_signals_handle" posted "Aniversário da Banda X" at "Some Venue" starting 2026-09-13 20:00, with a known time and no attribution dispute
    And the same handle posted "Banda X" at "Some Venue" starting 2026-09-13 20:00, with a known time and no attribution dispute
    When the merge pass runs
    Then the two posts collapse into one surviving event
    And the merge is recorded with both the title and the handle-time-match reasons

  # ── measuring before enabling it for real ──────────────────────────────

  Scenario: The dry-run measurement reports a new handle-time-match pair without writing anything
    Given the handle "boteconemapaujuvenal" posted "Domingo é dia de Boteco!" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    And the same handle posted "ALEX CARRERAS E BANDA" at "Boteco Nem A Pau Juvenal" starting 2026-09-13 18:00, with a known time and no attribution dispute
    When the dedup measurement script runs with handle-time-match forced on
    Then the report lists that pair as a new auto pair
    And no event row is changed
