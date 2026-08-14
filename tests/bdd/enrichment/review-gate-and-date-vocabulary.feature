Feature: Queue only the events an operator can actually fix
  As the operator of the event pipeline
  I must see a review queue that holds real extraction failures, and not posts
  whose kind carries no date, cadences that name no weekday, or events whose
  clock time alone went unread, so that the queue stays small enough to trust
  and a phrase as ordinary as "É HOJE" never files a perfectly readable post as
  unreadable.

  # A. Relative dates decorated the way people actually write them
  # `date_text` is the model's verbatim copy of the date expression itself --
  # never the whole caption -- so matching a token inside it is safe.

  Scenario: Resolve "É HOJE" to the post's own date
    Given a post published on 2026-08-07
    And an extracted event whose date text is "É HOJE"
    When the date is resolved
    Then the event starts on 2026-08-07

  Scenario: Resolve a relative token carrying punctuation
    Given a post published on 2026-08-07
    And an extracted event whose date text is "Hoje!"
    When the date is resolved
    Then the event starts on 2026-08-07

  Scenario: Resolve a relative token carrying an emoji
    Given a post published on 2026-08-07
    And an extracted event whose date text is "HOJE 🔥"
    When the date is resolved
    Then the event starts on 2026-08-07

  Scenario: Resolve "É AMANHÃ" to the day after the post
    Given a post published on 2026-08-07
    And an extracted event whose date text is "É AMANHÃ"
    When the date is resolved
    Then the event starts on 2026-08-08

  Scenario: Prefer an explicit date over a relative token in the same text
    Given a post published on 2026-08-07
    And an extracted event whose date text is "hoje, 15/08"
    When the date is resolved
    Then the event starts on 2026-08-15

  # Deferring to the explicit finders must not mean abandoning the token: a
  # clock time contains digits but names no day, so the finders decline and
  # the relative reading is the only one left.
  Scenario: Resolve a relative token stated beside a clock time
    Given a post published on 2026-08-07
    And an extracted event whose date text is "hoje às 22h"
    When the date is resolved
    Then the event starts on 2026-08-07

  Scenario: Resolve a decorated relative token stated beside a clock time
    Given a post published on 2026-08-07
    And an extracted event whose date text is "É HOJE! 23h"
    When the date is resolved
    Then the event starts on 2026-08-07

  # B. Cadences that name no weekday

  Scenario: Resolve a daily cadence to the post's own date
    Given a post published on 2026-08-11
    And an extracted event the model marked recurring with recurrence text "todo dia"
    When the date is resolved
    Then the event starts on 2026-08-11
    And the event is recorded as recurring

  Scenario: Resolve a weekend cadence to the next Saturday
    Given a post published on 2026-08-11
    And an extracted event the model marked recurring with recurrence text "todo fim de semana"
    When the date is resolved
    Then the event starts on 2026-08-15

  # A venue with a standing daily happy hour posts a dated special. The
  # cadence must not eat the date the venue actually wrote down -- and the
  # daily form matches almost every such venue's text, so this collision is
  # common, not hypothetical.
  Scenario: Prefer a stated date over a daily cadence
    Given a post published on 2026-08-07
    And an extracted event stating date text "15/08" alongside the cadence "todo dia"
    When the date is resolved
    Then the event starts on 2026-08-15
    And the event is recorded as recurring

  Scenario: Prefer a stated date over a weekday cadence
    Given a post published on 2026-08-07
    And an extracted event stating date text "15/08" alongside the cadence "toda quinta"
    When the date is resolved
    Then the event starts on 2026-08-15

  Scenario: Fall back to the cadence when no date is stated
    Given a post published on 2026-08-07
    And an extracted event the model marked recurring with recurrence text "toda quinta"
    When the date is resolved
    Then the event starts on 2026-08-13

  Scenario: Leave a cadence with no computable day undated without calling it unreadable
    Given a post published on 2026-08-11
    And an extracted event the model marked recurring with recurrence text "toda semana"
    When the date is resolved
    Then the event resolves to no start date
    And the event is not queued for review with reason "missing_date"

  Scenario: Never let a stored event claim it is recurring while reporting a missing date
    Given a post published on 2026-08-11
    And an extracted event the model marked recurring with recurrence text "toda semana"
    When the event is persisted
    Then no stored event reports both a recurrence and a missing date

  # C. Only an event is required to carry a date

  Scenario: Accept a promotion that states no date
    Given a venue post typed as a promotion with no date anywhere in it
    When the post is extracted
    Then the event is not queued for review
    And the event is accepted

  Scenario: Accept a greeting post that states no date
    Given a venue post typed as other with no date anywhere in it
    When the post is extracted
    Then the event is not queued for review

  Scenario: Still queue an event that states no date
    Given a venue post typed as an event with no date anywhere in it
    When the post is extracted
    Then the event is queued for review with reason "missing_date"

  Scenario: Never record a missing-date reason on a post type that carries no date
    Given a venue post typed as a promotion with no date anywhere in it
    When the post is extracted
    Then the stored review reason does not mention a missing date

  Scenario: Leave a menu's freshness semantics untouched
    Given a venue post typed as a menu with no date anywhere in it
    When the post is extracted
    Then the item's status is not decided by its start date
    And the menu still reports whether it is current from when it was last seen

  # D. An unread clock time is worth counting, not queueing

  Scenario: Accept an event whose flyer named a time that was not read
    Given a post whose flyer was classified as naming a time
    And an extracted event with a resolvable date and no time text
    When the post is extracted
    Then the event is not queued for review
    And the unread time is counted

  Scenario: Report the unread time on the admin API as a flag, not a review reason
    Given an accepted event whose flyer named a time that was not read
    When the event is read from the admin API
    Then the event reports that its time went unread
    And the event's review reason is empty
