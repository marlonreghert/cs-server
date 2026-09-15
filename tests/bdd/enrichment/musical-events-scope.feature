Feature: Musical events scope gate — non-musical posts skip agentic cost and never surface
  As an operator paying for per-event OpenAI/agentic calls and serving a
  nightlife/venue busyness product
  I want a post's own category to decide, before any model call fires, whether
  the display-title and venue-advisor passes ever run for it, and to decide,
  independently, whether the serving projection ever selects it
  So that a non-musical post (a school-holiday kids' activity program, a
  workshop, a comedy night) never spends a model call and never reaches the
  app, while a musical, off-vocabulary, or uncategorized post keeps behaving
  exactly as it does today

  See plans/260914_musical-events-scope.md. The deny-list defaults unlisted,
  off-vocabulary and null categories to eligible — the vocabulary is already
  proven stale on contact with real production data (90 distinct raw category
  values against a 17-value seed), so an allow-list would silently suppress
  agentic help for genuine, off-vocabulary music categories. The two gates are
  independent: a post can be excluded from the model passes without being
  excluded from serving, and vice versa, because the serving question ("is
  this venue already resolved and otherwise selectable") and the model-pass
  question ("does this row still need agentic help at all") are unrelated.

  Background:
    Given the event non-music category deny-list is at its default

  # ── the agentic passes skip a non-music row before any model call ──────────

  Scenario: A deny-listed-category post never reaches the display-title model call
    Given a touched event whose category is "kids / family"
    And that event has two source titles and no obvious canonical pick
    When the display-title pass runs for the touched events
    Then no display-title model call is charged for that event
    And that event's recorded outcome is "skipped_non_music"

  Scenario: A deny-listed-category post never reaches the venue-advisor model call
    Given a touched event whose category is "workshop"
    And that event is queued for venue resolution with candidate venues
    When the venue-advisor pass runs for the touched events
    Then no venue-advisor model call is charged for that event
    And that event's recorded outcome is "skipped_non_music"

  Scenario Outline: A post whose category is not deny-listed keeps reaching both model calls
    Given a touched event whose category is "<category>"
    And that event has two source titles and no obvious canonical pick
    And that event is queued for venue resolution with candidate venues
    When the display-title pass and the venue-advisor pass run for the touched events
    Then a display-title model call is charged for that event
    And a venue-advisor model call is charged for that event

    Examples:
      | category           |
      | rock                |
      | reggae               |
      |                      |

  # ── the serving projection excludes a deny-listed category row ─────────────

  Scenario: A deny-listed-category event with a deterministically resolved venue never appears in the serving projection
    Given an event whose category is "kids / family"
    And that event has a resolved venue, an accepted status, no superseding row, and a current start time
    When the serving projection selects events
    Then that event is not selected

  Scenario Outline: An event whose category is not deny-listed is unaffected by the category check
    Given an event whose category is "<category>"
    And that event has a resolved venue, an accepted status, no superseding row, and a current start time
    When the serving projection selects events
    Then that event is selected

    Examples:
      | category |
      | rock      |
      | reggae     |
      |            |

  # ── admin config is live, not a deploy ──────────────────────────────────────

  Scenario: Editing the non-music category deny-list changes projection eligibility without a deploy
    Given an event whose category is "quiz / trivia"
    And that event has a resolved venue, an accepted status, no superseding row, and a current start time
    And "quiz / trivia" is not on the event non-music category deny-list
    When the serving projection selects events
    Then that event is selected
    When an operator adds "quiz / trivia" to the event non-music category deny-list
    And the serving projection selects events again
    Then that event is not selected

  # ── excluded rows stay visible to the operator, never deleted ──────────────

  Scenario: A deny-listed-category event stays visible in the admin console after exclusion
    Given an event whose category is "kids / family" and is excluded from the serving projection
    When an operator lists events in the admin console
    Then that event still appears in the admin console listing

  # ── the venue-link-audit reviewer is unaffected ─────────────────────────────

  Scenario: The venue-link-audit reviewer makes the same model calls regardless of any event's category
    Given a flagged handle-venue pair whose corroborating events are all deny-listed-category events
    When the reviewer runs over the audit's flagged pairs
    Then a model call is still charged for that pair
