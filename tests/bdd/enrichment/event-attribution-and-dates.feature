@wip
Feature: Event attribution and dates
  An event must link to the venue its own text names, not to whichever venue the
  surrounding post mentioned first, and a date expression the model read
  correctly must not be lost because a regex did not recognise its shape. The
  model interprets; Python computes.

  Background:
    Given the venues "Taverna Pub" at handle "tavernapubnatal"
    And the venue "Casa do Matuto" at handle "casadomatutonatal"
    And the venue "Sempre Rock Bar" at handle "semprerockbar"

  # ── A. Per-event evidence outranks post-level evidence ──────────────────

  Scenario: Link an event to the venue named in its own text
    Given a promoter post whose caption mentions "@semprerockbar" first
    And an extracted event whose location text is "@tavernapubnatal"
    When the event is attributed
    Then the event links to "Taverna Pub"
    And the link method records that the mention came from the event's own text

  Scenario: Link twenty events from one roundup post to twenty different venues
    Given a promoter roundup post listing 20 events at 20 different venues
    And each event's location text names its own venue handle
    When the events are attributed
    Then each event links to the venue its own location text named
    And no two events link to the same venue

  Scenario: Refuse to link when the caption names several venues and the event names none
    Given a promoter post whose caption mentions "@semprerockbar" and "@tavernapubnatal"
    And an extracted event with no location text
    When the event is attributed
    Then the event links to no venue
    And the event is queued for review with reason "unresolved_venue"
    And the ambiguous-caption refusal is counted

  Scenario: Keep linking when the caption names exactly one venue and the event names none
    Given a promoter post whose caption mentions only "@semprerockbar"
    And an extracted event with no location text
    When the event is attributed
    Then the event links to "Sempre Rock Bar"
    And the link method records that the mention came from the post caption

  Scenario: Never self-link to the promoter's own handle
    Given a promoter post from "@oquetemhojeemnatal" that is itself a known venue handle
    And an extracted event whose location text is "@tavernapubnatal"
    When the event is attributed
    Then the event links to "Taverna Pub"

  Scenario: Prefer the event's own text over an Instagram location tag
    Given a post tagged at "Sempre Rock Bar"
    And an extracted event whose location text is "@tavernapubnatal"
    When the event is attributed
    Then the event links to "Taverna Pub"

  # ── B. Two venues behind one account ────────────────────────────────────

  Scenario: Send a Casa Forte event to the Casa Forte venue
    Given a crawl target "beerdock_recife" mapped to "BeerDock Boa Viagem"
    And a sibling venue "BeerDock Casa Forte" in the Casa Forte neighbourhood
    And an extracted event whose location text is "CASA FORTE"
    When the event is attributed
    Then the event links to "BeerDock Casa Forte"

  Scenario: Keep a Boa Viagem event at the account's own venue
    Given a crawl target "beerdock_recife" mapped to "BeerDock Boa Viagem"
    And a sibling venue "BeerDock Casa Forte" in the Casa Forte neighbourhood
    And an extracted event whose location text is "BOA VIAGEM"
    When the event is attributed
    Then the event links to "BeerDock Boa Viagem"

  Scenario: Queue for review when the branch cannot be determined
    Given a crawl target "beerdock_recife" mapped to "BeerDock Boa Viagem"
    And a sibling venue "BeerDock Casa Forte" in the Casa Forte neighbourhood
    And an extracted event whose location text names neither neighbourhood
    When the event is attributed
    Then the event is queued for review with reason "unresolved_venue"

  Scenario: Never let a neighbourhood drag an event to an unrelated venue
    Given a venue "Unrelated Bar" in the Casa Forte neighbourhood
    And a crawl target "conchittasbar" mapped to "Conchittas Bar"
    And an extracted event whose location text is "CASA FORTE"
    When the event is attributed
    Then the event does not link to "Unrelated Bar"

  # ── C/D. Dates: the model interprets, Python computes ───────────────────

  Scenario: Resolve "É HOJE" to the post's own date
    Given a post published on 2026-08-07
    And an extracted event whose date text is "É HOJE"
    When the date is resolved
    Then the event starts on 2026-08-07

  Scenario: Resolve a range stated with prepositions to its first day
    Given a post published on 2026-08-12
    And an extracted event whose date text is "De 06 a 09 de fevereiro"
    When the date is resolved
    Then the event starts on 2027-02-06
    And the event is flagged as a collapsed range

  Scenario: Resolve a comma-listed range to its first day
    Given a post published on 2026-06-20
    And an extracted event whose date text is "01, 02 e 03 de julho"
    When the date is resolved
    Then the event starts on 2026-07-01
    And the event is flagged as a collapsed range

  Scenario: Keep the current year for a date a few days older than its post
    Given a post published on 2026-08-12
    And an extracted event whose date text is "08/08"
    When the date is resolved
    Then the event starts on 2026-08-08
    And the event is flagged as having an inferred year

  Scenario: Roll the year for a date months older than its post
    Given a post published on 2026-08-12
    And an extracted event whose date text is "06 de fevereiro"
    When the date is resolved
    Then the event starts on 2027-02-06
    And the event is flagged as having an inferred year

  Scenario: Resolve a three-letter month
    Given a post published on 2026-08-12
    And an extracted event whose date text is "05/SET"
    When the date is resolved
    Then the event starts on 2026-09-05

  # 2026-07-02 is genuinely a Thursday. Do not "simplify" this to a date where
  # the weekday and the day number disagree — the resolver corroborates them and
  # would correctly raise a weekday mismatch, which is a different scenario.
  Scenario: Resolve a weekday stated with its day number
    Given a post published on 2026-06-25
    And an extracted event whose date text is "Quinta (02)"
    When the date is resolved
    Then the event starts on 2026-07-02

  Scenario: Flag a weekday that contradicts its day number
    Given a post published on 2026-08-31
    And an extracted event whose date text is "Quinta (02)"
    When the date is resolved
    Then the event is flagged with a weekday mismatch

  Scenario: Reuse a stored interpretation when the same post is extracted again
    Given a post already extracted with date text "É HOJE" resolving to 2026-08-07
    When the same post is extracted again and the model returns a different interpretation
    Then the event still starts on 2026-08-07
    And the source event key is unchanged

  Scenario: Ignore an absolute date supplied by the model
    Given a post published on 2026-08-07
    And an extracted event whose date text is "É HOJE"
    And the model also returns an absolute date of 2027-01-01
    When the date is resolved
    Then the event starts on 2026-08-07

  Scenario: Leave an unreadable date unresolved and queued
    Given a post published on 2026-08-12
    And an extracted event whose date text cannot be interpreted
    When the date is resolved
    Then the event has no start date
    And the event is queued for review with reason "missing_date"

  # ── E. A greeting is not an event ──────────────────────────────────────────

  Scenario: Type a birthday greeting as other, not as an event
    Given a post whose caption reads "Parabéns pelos seus 31 anos! Feliz aniversário!"
    And the post names a venue and a date but no lineup and no time
    When the post is extracted
    Then the item's post type is "other"
    And the item's post type is not "event"

  Scenario: Type a recap of a past night as other
    Given a post whose caption thanks everyone who came last Friday
    And the post names the venue, the date and the acts that played
    When the post is extracted
    Then the item's post type is "other"

  Scenario: Keep typing a genuine announcement as an event
    Given a post announcing a party on Friday with its lineup and door price
    When the post is extracted
    Then the item's post type is "event"

  Scenario: Type a daily food offer as a promotion, not an event
    Given a post announcing "Especial do dia" with a price and no lineup
    When the post is extracted
    Then the item's post type is "promotion"
