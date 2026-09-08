@wip
Feature: Non-events stay out of the events serving projection, and its recurrence, category and end-time values are serving-correct

  As the system of record for the events serving projection
  I must never project a recurring venue service offering as an event,
  and I must serve a casing-normalised recurrence phrase, a category
  canonicalised against the live admin vocabulary, and an end time that
  belongs to the occurrence it is attached to,
  so that the app renders one spelling per recurrence, one spelling per
  genre, an end time that is never before its own start, and nothing that
  is merely the venue being open on its ordinary days.

  # Every scenario below drives the REAL projector and asserts on what the
  # projector itself wrote. No scenario may hand-seed `events_index_v1:*` or
  # `event_occurrence_v1:*` — the sibling feature file records why: a
  # hand-seeded index goes green while production stays broken.
  #
  # No scenario may reach OpenAI, S3, Apify or Instagram. The extraction
  # prompt's amended `kind` precedence is asserted by pytest (that both
  # prompts carry the shared constant) and by a resampled live eval outside
  # this suite; here it is expressed only as its observable outcome — a row
  # typed `menu` or `promotion` never reaches the projection.
  #
  # The plan's §0 operator decision simplifies that prompt rule and accepts
  # that it also suppresses genuine food-anchored events. Nothing in THIS
  # file implements the rule, so nothing here changes because of it — but
  # two scenarios below carry its consequence: the projection must never
  # filter on `category` (or the operator's re-admission would be deleted at
  # the projection layer), and an operator PATCHing `post_type` back to
  # "event" must restore the occurrences (that PATCH is the whole mechanism
  # by which §0's concession is reversible).
  #
  # The `ends_at` scenarios below split on whether an occurrence's
  # `starts_at` was RE-DERIVED from a weekday pattern (`occurrence_id` is
  # `<event_id>_<date>`) or served on the announcement's own stored
  # `starts_at` (`occurrence_id` is the bare `event_id`). They deliberately
  # do NOT split on `is_recurring`: a recurring row whose recurrence prose
  # this repo cannot parse takes the SECOND shape, and treating it as the
  # first would delete a genuine multi-day end.

  Background:
    Given a geo-fence city "recife" is configured
    And the events projection horizon is 21 days
    And the current Recife local time is 2026-09-07 12:00

  # ── The non-event class ────────────────────────────────────────────────

  Scenario: A standing service offering is never selected for the projection
    Given an accepted event row for venue "Cachaçaria Tradição" titled "Buffet de Terça a Domingo"
    And that row has post_type "menu"
    And that row is recurring with recurrence text "Terça a Domingo"
    And that row was last seen 3 days ago
    When the events projection runs
    Then no occurrence key exists for that event
    And the city events index for "recife" holds no occurrence of that event
    And the venue events index for "Cachaçaria Tradição" holds no occurrence of that event

  Scenario: A standing price offering is never selected for the projection
    Given an accepted event row for venue "Downtown Beer Garden" titled "QUINTA É DIA DE HAPPY HOUR"
    And that row has post_type "promotion"
    And that row is recurring with recurrence text "Toda quinta"
    And that row was last seen 3 days ago
    When the events projection runs
    Then no occurrence key exists for that event
    And the city events index for "recife" holds no occurrence of that event

  Scenario: A genuine recurring night is still projected
    Given an accepted event row for venue "Downtown Beer Garden" titled "Sambinha Downtown"
    And that row has post_type "event"
    And that row is recurring with recurrence text "TODOS OS DOMINGOS"
    And that row was last seen 3 days ago
    When the events projection runs
    Then the city events index for "recife" holds at least one occurrence of that event

  Scenario: A food-anchored row typed as an event is still projected
    Given an accepted event row for venue "Cachaçaria Tradição" titled "Feijoada com samba ao vivo"
    And that row has post_type "event"
    And that row has category "food festival"
    And that row is recurring with recurrence text "todos os sábados"
    And that row was last seen 3 days ago
    When the events projection runs
    Then the city events index for "recife" holds at least one occurrence of that event
    # The projection must never filter on category. Under the plan's §0 the
    # extraction rule now SUPPRESSES food-anchored captions, so the only way
    # a row like this exists is that an operator typed it `event` by hand --
    # which makes this scenario stronger, not weaker. `food festival` and
    # `tasting` are first-class entries in this repo's own shipped
    # DEFAULT_CATEGORY_VOCABULARY, and a category blocklist here would
    # delete the operator's own correction and make §0's trade irreversible.

  Scenario: An operator re-admitting a suppressed row re-projects every occurrence
    Given a recurring event row titled "Feijoada com samba ao vivo" with post_type "menu"
    And the events projection has already run
    And no occurrence key exists for that event
    When an operator sets that event's post_type to "event"
    And the events projection runs
    Then the city events index for "recife" holds at least one occurrence of that event
    And an occurrence key exists for that event
    # The inverse of the reclassification scenario above, and the mechanical
    # proof that §0's food-boundary concession is reversible per row with no
    # migration, no re-extraction and no deploy.

  Scenario: A recurring class with a paid enrolment is still projected
    Given an accepted event row for venue "Sala de Reboco" titled "Aula de FORRÓ"
    And that row has post_type "event"
    And that row has category "workshop"
    And that row is recurring with recurrence text "Toda QUARTA"
    And that row was last seen 3 days ago
    When the events projection runs
    Then the city events index for "recife" holds at least one occurrence of that event

  Scenario: An operator reclassification deprojects every occurrence of the event
    Given an accepted recurring event row titled "Buffet de Terça a Domingo" with post_type "event"
    And the events projection has already run
    And the city events index for "recife" holds 6 occurrences of that event
    When an operator sets that event's post_type to "menu"
    And the events projection runs
    Then no occurrence key exists for that event
    And the city events index for "recife" holds no occurrence of that event
    And the venue events index for that event's venue holds no occurrence of it
    And the event row still exists in the system of record with post_type "menu"

  Scenario: A reclassified event is not resurrected by a later projection cycle
    Given an accepted recurring event row whose post_type an operator set to "menu"
    And the events projection has already run
    When the events projection runs again
    Then no occurrence key exists for that event
    And the city events index for "recife" holds no occurrence of that event

  # ── recurrence_text: casing only ───────────────────────────────────────

  Scenario Outline: A recurrence phrase is projected in normalised sentence case
    Given an accepted recurring event row with recurrence text "<stored>"
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event carries recurrence text "<projected>"

    Examples: the two case-variant pairs measured in production
      | stored           | projected        |
      | Toda QUARTA      | Toda quarta      |
      | toda quarta      | Toda quarta      |
      | TODOS OS SÁBADOS | Todos os sábados |
      | todos os sábados | Todos os sábados |
      | TODOS OS DOMINGOS| Todos os domingos|
      | Terça a Domingo  | Terça a domingo  |

  Scenario: A recurrence phrase that needs no change is projected verbatim
    Given an accepted recurring event row with recurrence text "Toda sexta, às 21 horas"
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event carries recurrence text "Toda sexta, às 21 horas"

  Scenario: Two recurrence phrases that differ by wording stay distinct
    Given an accepted recurring event row "A" with recurrence text "Aos domingos"
    And an accepted recurring event row "B" with recurrence text "TODOS OS DOMINGOS"
    And both rows were last seen 3 days ago
    When the events projection runs
    Then event "A" is projected with recurrence text "Aos domingos"
    And event "B" is projected with recurrence text "Todos os domingos"
    And the two projected recurrence texts are different

  Scenario: The system of record keeps the verbatim extracted recurrence phrase
    Given an accepted recurring event row with recurrence text "Toda QUARTA"
    And that row was last seen 3 days ago
    When the events projection runs
    Then the event row in the system of record still has recurrence text "Toda QUARTA"

  Scenario: A row with no recurrence phrase projects a null recurrence text
    Given an accepted non-recurring event row with no recurrence text
    When the events projection runs
    Then every projected occurrence of that event carries a null recurrence text

  # ── category: canonicalised against the live admin vocabulary ──────────

  Scenario: A category is canonicalised against the live admin vocabulary
    Given the admin post category vocabulary is configured as "Forró, Party, Live Music"
    And an accepted event row with category "forró"
    When the events projection runs
    Then every projected occurrence of that event carries category "Forró"

  Scenario: With no admin vocabulary configured the shipped default spelling is projected
    Given no admin post category vocabulary is configured
    And an accepted event row with category "FORRÓ"
    When the events projection runs
    Then every projected occurrence of that event carries category "forró"

  Scenario: A category already spelled as the vocabulary spells it is reported unchanged
    Given the admin post category vocabulary is configured as "Forró, Party, Live Music"
    And an accepted event row with category "Forró"
    When the events projection runs
    Then every projected occurrence of that event carries category "Forró"
    And the category outcome counter records outcome "unchanged"
    And it does not record outcome "canonicalized" for that row
    # `canonicalized` means the vocabulary REWROTE the stored spelling;
    # `unchanged` means the stored spelling and the vocabulary already
    # agreed. Declaring the label without ever reaching it asserts nothing,
    # and this plan's observability posture is that a zero-filled label's
    # presence is the evidence.

  Scenario: An off-vocabulary category is projected unchanged, never dropped
    Given the admin post category vocabulary is configured as "Forró, Party, Live Music"
    And an accepted event row with category "brega"
    When the events projection runs
    Then every projected occurrence of that event carries category "brega"

  Scenario: An unreadable vocabulary config does not fail the projection cycle
    Given the admin post category vocabulary read fails
    And an accepted event row with category "forró"
    When the events projection runs
    Then every projected occurrence of that event is still written
    And every projected occurrence of that event carries category "forró"
    And the projection reports no event-stage errors

  Scenario: A row with no category projects a null category
    Given an accepted event row with no category
    When the events projection runs
    Then every projected occurrence of that event carries a null category

  # ── ends_at: the occurrence's own end, or nothing ──────────────────────

  Scenario: A recurring occurrence carries the source duration onto its own date
    Given an accepted recurring event row starting at 22:00 and ending at 23:00 on 2026-08-12
    And that row is recurring with recurrence text "Toda quarta"
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event ends exactly 1 hour after its own start
    And no projected occurrence of that event ends before its own start

  Scenario: A recurring row whose stored end precedes its stored start projects a null end
    Given an accepted recurring event row starting at 2026-09-09 22:00 and ending at 2026-08-12 23:00
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event carries a null end time

  Scenario: A re-derived occurrence with a zero-length stored duration projects a null end
    Given an accepted recurring event row with recurrence text "Toda quarta"
    And that row's stored end equals its stored start
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event carries a null end time
    # A zero duration carried onto a re-derived start would end the
    # occurrence at the instant it begins, which tells a reader nothing.
    # It is NOT an inversion, so it is counted separately -- see the
    # outcome outline at the foot of this file.

  Scenario: A recurring row with an implausible stored duration projects a null end
    Given an accepted recurring event row whose stored end is 30 hours after its stored start
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event carries a null end time

  Scenario: A recurring row with no stored end projects a null end
    Given an accepted recurring event row with no stored end time
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event carries a null end time

  Scenario: A non-recurring occurrence carries its stored end time verbatim
    Given an accepted non-recurring event row starting at 2026-09-19 22:00 and ending at 2026-09-20 03:00
    When the events projection runs
    Then the projected occurrence of that event ends at 2026-09-20 03:00

  # ── the carried branch: recurring, but nothing was re-derived ──────────
  #
  # `expand_occurrences` sends a recurring row whose `recurrence_text`
  # yields no weekday set to the SAME single-occurrence shape a
  # non-recurring row takes: one occurrence, on the row's own stored
  # `starts_at`, keyed by the bare `event_id`. Its stored `ends_at` is
  # therefore the correct end for it and must be carried verbatim. Keying
  # the decision on `is_recurring` instead of on whether the occurrence was
  # RE-DERIVED is the defect these three scenarios exist to prevent -- it
  # would delete the end of a genuine multi-day festival, which is exactly
  # what vibes_bot declines to do at serve time.

  Scenario: A recurring row whose recurrence text cannot be parsed carries its stored end verbatim
    Given an accepted event row that is recurring with recurrence text "toda semana"
    And that row starts at 2026-09-19 22:00 and ends at 2026-09-20 03:00
    And that row was last seen 3 days ago
    When the events projection runs
    Then that event is projected as exactly one occurrence
    And that occurrence's id is the bare event id
    And that occurrence starts at 2026-09-19 22:00
    And that occurrence ends at 2026-09-20 03:00

  Scenario: A multi-day interval on the carried branch is not truncated by the 24 hour bound
    Given an accepted event row that is recurring with recurrence text "toda semana"
    And that row starts at 2026-10-01 18:00 and ends at 2026-10-04 04:00
    And that row was last seen 3 days ago
    When the events projection runs
    Then that occurrence ends at 2026-10-04 04:00
    And that occurrence's end time is more than 24 hours after its start

  Scenario: A zero-length stored interval on the carried branch is carried, not dropped
    Given an accepted non-recurring event row whose stored end equals its stored start
    When the events projection runs
    Then the projected occurrence of that event carries an end time equal to its start
    And that end time is not null

  Scenario: An inverted stored pair on the carried branch projects a null end
    Given an accepted non-recurring event row starting at 2026-09-19 22:00 and ending at 2026-09-19 19:00
    When the events projection runs
    Then the projected occurrence of that event carries a null end time

  Scenario: No occurrence written by a full cycle ever ends before it starts
    Given the projection source holds every event row from the production census
    When the events projection runs
    Then no occurrence written by that cycle has an end time before its own start time
    # Exhaustive over every payload the cycle wrote, not over the scenarios
    # named above: an enumerated check would go green while a branch nobody
    # listed stayed broken.

  Scenario: Every occurrence a full cycle writes on the carried branch keeps its row's stored end
    Given the projection source holds every event row from the production census
    And one of those rows is recurring with recurrence text this repo cannot parse
    When the events projection runs
    Then every occurrence whose id is a bare event id carries its row's stored end time unchanged

  # ── the contract is unchanged apart from those three values ────────────

  Scenario: The projected occurrence payload carries the pinned field set unchanged
    Given an accepted recurring event row with every contract field populated
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence carries exactly the pinned occurrence field set
    And no field has been added, removed or renamed

  # ── observability ──────────────────────────────────────────────────────

  Scenario: Each per-row substitution is counted once per source row, not once per occurrence
    Given an accepted recurring event row with recurrence text "Toda QUARTA" and category "forró"
    And that row expands to 6 occurrences
    And that row was last seen 3 days ago
    When the events projection runs
    Then the recurrence text outcome counter records exactly 1 observation for that cycle
    And the category outcome counter records exactly 1 observation for that cycle
    And the end time outcome counter records exactly 1 observation for that cycle

  Scenario Outline: Each per-row substitution reports its own outcome
    Given an accepted event row matching "<case>"
    When the events projection runs
    Then the "<counter>" counter records outcome "<outcome>"

    Examples:
      | case                                        | counter         | outcome              |
      | a recurrence phrase changed by normalising  | recurrence text | normalized           |
      | a recurrence phrase already normalised      | recurrence text | unchanged            |
      | no recurrence phrase                        | recurrence text | absent               |
      | a category matching the live vocabulary     | category        | canonicalized        |
      | a category already in the vocabulary spelling | category      | unchanged            |
      | a category off the live vocabulary          | category        | off_vocabulary       |
      | no category                                 | category        | absent               |
      | a re-derived occurrence with a usable duration | end time     | derived              |
      | a re-derived occurrence with an inverted pair | end time      | dropped_inverted     |
      | a re-derived occurrence with a zero duration | end time       | dropped_zero         |
      | a re-derived occurrence with a 30 hour duration | end time    | dropped_implausible  |
      | a carried occurrence with a stored end       | end time       | carried              |
      | a carried occurrence with a 3 day stored end | end time       | carried              |
      | a carried occurrence with an inverted pair   | end time       | dropped_inverted     |
      | a row with no stored end                     | end time       | absent               |

  # `dropped_zero` is deliberately NOT folded into `dropped_inverted`: a
  # zero-length interval is not an inversion, and folding them makes the
  # counter unreadable as the data-defect signal it exists to be. Note also
  # that zero is DROPPED on the re-derived branch and CARRIED on the other:
  # the label set encodes the branch, so no second label is needed.
