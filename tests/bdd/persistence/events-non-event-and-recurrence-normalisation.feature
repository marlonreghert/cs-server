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

  Scenario: A recurring row with a zero-length stored duration projects a null end
    Given an accepted recurring event row whose stored end equals its stored start
    And that row was last seen 3 days ago
    When the events projection runs
    Then every projected occurrence of that event carries a null end time

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

  Scenario: No occurrence written by a full cycle ever ends before it starts
    Given the projection source holds every event row from the production census
    When the events projection runs
    Then no occurrence written by that cycle has an end time before its own start time

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
      | a category off the live vocabulary          | category        | off_vocabulary       |
      | no category                                 | category        | absent               |
      | a recurring row with a usable duration      | end time        | derived              |
      | a recurring row with an inverted stored end | end time        | dropped_inverted     |
      | a recurring row with a 30 hour duration     | end time        | dropped_implausible  |
      | a non-recurring row with a stored end       | end time        | carried              |
      | a row with no stored end                    | end time        | absent               |
