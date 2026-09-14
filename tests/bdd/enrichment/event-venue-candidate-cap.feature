@wip
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
    Given the event extraction pipeline is configured for a known venue
    And the venue catalog carries 30 venues whose names loosely resemble the event's own location text
    And the candidate top-K is 20

  # ── the cap, and what it must never touch ──────────────────────────────

  Scenario: Keep only the top-K ranked candidates for a name-match resolution
    When an event's location text scores above zero against every venue in the catalog
    Then at most 20 ranked venue candidates are stored for that event
    And the stored candidates are still ordered best score first

  Scenario: Never drop the winning candidate or its runner-up
    Given the candidate top-K is 2
    When an event's location text scores above zero against every venue in the catalog
    Then the stored candidates still include the best-scored venue and the second-best-scored venue

  Scenario: Reach the exact same auto-link verdict before and after capping
    Given a location text whose best-scored venue clears the confidence floor and the margin over its runner-up
    When the event is resolved with the candidate top-K applied
    Then the event auto-links to the same venue it would have without any cap

  Scenario: Reach the exact same queued verdict before and after capping
    Given a location text whose best-scored venue clears the confidence floor but not the margin over its runner-up
    When the event is resolved with the candidate top-K applied
    Then the event is queued for review with the same top two candidates it would have without any cap

  Scenario: Leave a handle-mention resolution untouched by the cap
    When an event's own location text names a known venue by its Instagram handle
    Then exactly one ranked candidate is stored for that event
    And the cap never applies to it

  Scenario: Leave a bounded neighbourhood-match candidate set untouched by the cap
    Given a handle with two same-brand sibling venues
    When an event's location text matches one sibling's neighbourhood
    Then the stored candidates are exactly the sibling venues the neighbourhood rung considered
    And the cap never applies to them

  Scenario: Count a truncation only when the cap actually removes candidates
    When an event's location text scores above zero against every venue in the catalog
    Then a candidate-truncation is counted for that event

  Scenario: Never count a truncation when the candidate set already fits
    Given the venue catalog carries only 3 venues whose names loosely resemble the event's own location text
    When the event is resolved
    Then no candidate-truncation is counted for that event

  # ── the two readers this plan exists for ───────────────────────────────

  Scenario: The review queue returns at most the capped number of candidates
    Given an event whose uncapped resolution would have scored the whole catalog
    When an operator requests the event review queue
    Then that event's entry lists at most 20 ranked venue candidates

  # ── the one-off repair for rows written before this cap shipped ───────

  Scenario: Report every oversized event without writing anything, before apply
    Given an event already stores more ranked candidates than the configured top-K
    When the candidate-cap backfill runs without apply
    Then the report names that event and its current and would-be candidate counts
    And the stored candidates for that event are unchanged

  Scenario: Shrink an oversized event's stored candidates when applied
    Given an event already stores more ranked candidates than the configured top-K
    When the candidate-cap backfill runs with apply
    Then that event stores at most the configured top-K candidates
    And the event's venue_id, location_resolution, and review_reason are unchanged

  Scenario: Change nothing on a second backfill run
    Given the candidate-cap backfill has already applied once
    When the candidate-cap backfill runs with apply a second time
    Then no candidate row is inserted, updated, or deleted
