Feature: Say when a date was guessed, say why an event is queued, and report both paths alike
  A date earlier than the post that announced it is rolled forward a year. That
  is a guess, and a guess must not arrive wearing high confidence — least of all
  when its own siblings, from the same flyer, resolved to a different year. An
  event queued for a human must say what the human is being asked to decide. And
  the shared-handle crawl path must report what the single-venue path reports.

  Background:
    Given the event extraction pipeline is configured for a known venue

  # --- A guessed year is a guess --------------------------------------------

  Scenario: Flag an event whose year was inferred
    Given a post announcing a date earlier in the year than the post itself
    When the post is extracted
    Then the event is flagged as having an inferred year

  Scenario: Keep an inferred year out of auto-accept
    Given a post announcing a date earlier in the year than the post itself
    When the post is extracted
    Then the event is not auto-accepted

  Scenario: Leave a future-dated event alone
    Given a post announcing a date later than the post itself
    When the post is extracted
    Then the event is not flagged as having an inferred year

  # --- Siblings from one flyer agree ----------------------------------------

  Scenario: Follow the year the sibling dates resolved to
    Given one post announcing four weekly dates, two of them already past
    When the post is extracted
    Then every event resolves to the same year
    And no event is dated a year later than its siblings

  Scenario: Keep one post's dates from influencing another's
    Given two unrelated posts each announcing one date
    When the posts are extracted
    Then each event resolves from its own post only

  Scenario: Refuse to break a tie between sibling years
    Given one post whose dates split evenly between two years
    When the post is extracted
    Then the events are flagged as having an inferred year

  # --- A range starts on its first day --------------------------------------

  Scenario: Start a multi-day event on its first day
    Given a post announcing an event across three consecutive days
    When the post is extracted
    Then the event starts on the first of those days

  Scenario: Say that a range was collapsed
    Given a post announcing an event across three consecutive days
    When the post is extracted
    Then the event is flagged as having a collapsed date range

  Scenario: Leave a single-date event alone
    Given a post announcing an event on one date
    When the post is extracted
    Then the event is not flagged as having a collapsed date range

  # --- Every queued event says why ------------------------------------------

  Scenario: State that the venue could not be resolved
    Given a crawl target whose handle belongs to two venues
    And a post naming neither venue
    When its scheduled crawl runs
    Then the event states that its venue could not be resolved

  Scenario: Never queue an event with no reason at all
    Given any post that produces a queued event
    When the post is extracted
    Then that event states a reason

  # --- Both paths report alike ----------------------------------------------

  Scenario: Queue an unresolved venue post from a shared handle
    Given a crawl target whose handle belongs to two venues
    And a post naming neither venue
    When its scheduled crawl runs
    And the shared-handle review queue is listed
    Then that event is present in the queue

  Scenario: Label a venue's own post as a venue post
    Given a crawl target whose handle belongs to two venues
    When its scheduled crawl runs
    Then each event records that it came from a venue post

  Scenario: Count the kind split on the shared-handle path
    Given a crawl target whose handle belongs to two venues
    And one of its posts is a menu item
    When its scheduled crawl runs
    Then the extraction is counted as a menu item

  Scenario: Count attribution outcomes on the shared-handle path
    Given a crawl target whose handle belongs to two venues
    And one post naming a venue and one naming neither
    When its scheduled crawl runs
    Then one attribution is counted as resolved
    And one attribution is counted as ambiguous
