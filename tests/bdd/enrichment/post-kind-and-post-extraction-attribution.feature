Feature: Say what a post actually is, and attribute it once the model has read it
  Not every post that mentions a weekday and a time announces an event — a
  lunch special reads the same way. The model must say what a post is, and a
  shared handle's venue must be chosen from the model's own reading of the
  post rather than guessed beforehand.

  The model's `kind` answer is what a post's `post_type` is stored as.
  plans/260811_post-items-and-categories.md retires this feature's original
  "a non-event produces no row at all" behaviour: what a post classifies as
  now determines its TYPE, never whether it is persisted at all — see
  tests/bdd/enrichment/post-items-and-categories.feature for the type/
  persistence/queue coverage that replaced the "Classify ... as ..." and
  "A non-event never reaches ..." scenarios this file used to carry. What
  remains here is attribution (§C) and recurrence (§D), which that plan does
  not touch, plus the two classification edge cases (missing/unrecognised
  kind) that are specific to how THIS extraction path reads the model's
  answer.

  Background:
    Given the event extraction pipeline is configured for a known venue

  # --- What a post is ------------------------------------------------------

  Scenario: Classify a DJ night as an event
    Given a post announcing a DJ night on a stated date
    When the post is extracted
    Then the post is recorded as an event

  Scenario: Prefer event when a post is both an event and an offer
    Given a post announcing a show on a stated date with a drinks offer
    When the post is extracted
    Then the post is recorded as an event

  Scenario: Treat a missing kind as an event
    Given a post whose extraction response omits a kind
    When the post is extracted
    Then that post's event is present in the review queue

  Scenario: Treat an unrecognised kind as still needing review like any other
    Given a post whose extraction response names a kind this pipeline does not know
    When the post is extracted
    Then that post's event is present in the review queue

  # Note: "let an operator correct a misclassified post" is now covered by
  # tests/bdd/enrichment/post-items-and-categories.feature's "Let an
  # operator correct a misclassified item" — every post_type is persisted
  # and PATCHable now, so the gap this note used to document no longer
  # exists.

  # --- Attributing a venue after extraction --------------------------------

  Scenario: Attribute a venue from the model's location text
    Given a crawl target whose handle belongs to two venues
    And an extracted post whose location text names one of them
    When the post is attributed
    Then the event is attributed to that venue

  Scenario: Fall back to the caption when the model reported no location
    Given a crawl target whose handle belongs to two venues
    And an extracted post with no location text whose caption names one venue
    When the post is attributed
    Then the event is attributed to that venue

  Scenario: Attribute directly when a handle belongs to one venue
    Given a crawl target whose handle belongs to one venue
    When the post is attributed
    Then the event is attributed to that venue
    And no venue resolution is attempted

  Scenario: Queue an event that neither signal resolves
    Given a crawl target whose handle belongs to two venues
    And an extracted post naming neither venue
    When the post is attributed
    Then the event is attributed to no venue
    And the event awaits a human decision

  Scenario: Archive under the handle before attribution decides
    Given a crawl target whose handle belongs to two venues
    When its scheduled crawl runs
    Then the post's images are archived under the handle

  # --- Recurring programming -----------------------------------------------

  Scenario: Resolve a weekday-range recurrence
    Given an extracted event recurring from one weekday to another
    When its date is resolved
    Then it resolves to the next matching day
    And it is not flagged as missing a date

  Scenario: Resolve a weekday-list recurrence
    Given an extracted event recurring on two named weekdays
    When its date is resolved
    Then it resolves to the next matching day
    And it is not flagged as missing a date

  Scenario: Keep flagging a recurrence nobody can parse
    Given an extracted event whose recurrence phrase cannot be parsed
    When its date is resolved
    Then it has no date
    And it is flagged as missing a date

  Scenario: Never let recurrence resolve a one-off weekday post
    Given an extracted event naming a weekday that is not recurring
    When its date is resolved
    Then the recurrence reading is not applied
