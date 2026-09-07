Feature: Events venue bairro, ticket URL and nightlife-day projection
  As the VibeSense app
  I want every event card to show a real bairro and an openable ticket link,
  and last night's party to still be in the index while the night is still on
  So that a served event neither names a venue complex as its neighbourhood,
  nor offers a link the phone refuses to open, nor vanishes at midnight while
  it is still running

  # Every scenario below that touches the index or an occurrence payload drives
  # the REAL projector and asserts on what the projector itself wrote. No
  # scenario may hand-seed `events_index_v1:*` or `event_occurrence_v1:*` — a
  # hand-seeded index is exactly what would go green while production stayed
  # broken (review finding F01).

  Background:
    Given the events projection is enabled
    And "Downtown Beer Garden" is a servable venue in Recife
    And the current time is 2026-09-06 18:00 in Recife

  # ── ticket_url: normalised at projection, verbatim in RDS ────────────────

  Scenario: Qualify a scheme-less ticket host with a scheme
    Given an accepted event at "Downtown Beer Garden" whose stored ticket url is "evenyx.com"
    When the events projection runs
    Then the occurrence payload carries the ticket url "https://evenyx.com"
    And the ticket url normalisation outcome "scheme_added" is counted once

  Scenario: Preserve the path, query and fragment when adding the scheme
    Given an accepted event whose stored ticket url is "evenyx.com/e/42?ref=ig#lote2"
    When the events projection runs
    Then the occurrence payload carries the ticket url "https://evenyx.com/e/42?ref=ig#lote2"

  Scenario: Qualify a scheme-less ticket host that carries a port
    Given an accepted event whose stored ticket url is "evenyx.com:8080/lote2"
    When the events projection runs
    Then the occurrence payload carries the ticket url "https://evenyx.com:8080/lote2"
    And the ticket url normalisation outcome "scheme_added" is counted once

  Scenario: Project no ticket url for a host whose pseudo-port is not numeric
    Given an accepted event whose stored ticket url is "evenyx.com:lote2"
    When the events projection runs
    Then the occurrence payload carries no ticket url
    And the ticket url normalisation outcome "rejected" is counted once

  Scenario: Project no ticket url for a host whose port is not a real TCP port
    Given an accepted event whose stored ticket url is "evenyx.com:80808/x"
    When the events projection runs
    Then the occurrence payload carries no ticket url
    And the ticket url normalisation outcome "rejected" is counted once

  Scenario: Leave an already absolute ticket url exactly as stored
    Given an accepted event whose stored ticket url is "https://sympla.com.br/evento/123"
    When the events projection runs
    Then the occurrence payload carries the ticket url "https://sympla.com.br/evento/123"
    And the ticket url normalisation outcome "passthrough" is counted once

  Scenario: Do not upgrade a stored plaintext ticket url to https
    Given an accepted event whose stored ticket url is "http://ingressos.example.com/x"
    When the events projection runs
    Then the occurrence payload carries the ticket url "http://ingressos.example.com/x"

  Scenario: Project no ticket url for caption prose
    Given an accepted event whose stored ticket url is "ingressos na portaria"
    When the events projection runs
    Then the occurrence payload carries no ticket url
    And the ticket url normalisation outcome "rejected" is counted once

  Scenario: Project no ticket url for a non-web scheme
    Given an accepted event whose stored ticket url is "mailto:vendas@casa.com"
    When the events projection runs
    Then the occurrence payload carries no ticket url
    And the ticket url normalisation outcome "rejected" is counted once

  Scenario: Project no ticket url when the extraction found none
    Given an accepted event with no stored ticket url
    When the events projection runs
    Then the occurrence payload carries no ticket url
    And the ticket url normalisation outcome "absent" is counted once

  Scenario: Leave the stored ticket url verbatim in the system of record
    Given an accepted event whose stored ticket url is "evenyx.com"
    When the events projection runs
    Then the event row still holds the ticket url "evenyx.com"

  Scenario: Re-assert the normalised ticket url on the next projection cycle
    Given an accepted event whose stored ticket url is "evenyx.com"
    And the events projection has already run once
    When the events projection runs again
    Then the occurrence payload carries the ticket url "https://evenyx.com"
    And no event row was written during either cycle

  # The counter is bumped once per SOURCE ROW, never once per occurrence: one
  # badly-extracted recurring row must not read as 22 rejections (review
  # finding R15).
  Scenario: Count a recurring event's ticket url once, not once per occurrence
    Given an accepted recurring event whose recurrence text is "todo dia" and whose stored ticket url is "ingressos na portaria"
    When the events projection runs
    Then more than one occurrence is projected for that event
    And every one of those occurrence payloads carries no ticket url
    And the ticket url normalisation outcome "rejected" is counted once

  # ...and it is re-counted every cycle, because the projector rebuilds every
  # payload every cycle. This pins the per-cycle-census reading the plan's
  # Observability section documents (R15).
  Scenario: Count the ticket url again on the next projection cycle
    Given an accepted event whose stored ticket url is "evenyx.com"
    And the events projection has already run once
    When the events projection runs again
    Then the ticket url normalisation outcome "scheme_added" is counted twice in total

  # ── venue_neighborhood: make the authoritative rung reachable ────────────

  Scenario: Select a fully populated parsed address row in upgrade mode
    Given "Downtown Beer Garden" has street, neighborhood, city and postal code stored
    And every one of those four values is sourced "parsed"
    When the address components backfill selects candidates in upgrade mode
    Then "Downtown Beer Garden" is among the selected candidates

  Scenario: Do not select a fully populated parsed address row in the default mode
    Given "Downtown Beer Garden" has street, neighborhood, city and postal code stored
    And every one of those four values is sourced "parsed"
    When the address components backfill selects candidates in the default mode
    Then no candidates are selected

  Scenario: Reject an unknown selection mode instead of falling back
    When the address components backfill selects candidates in mode "everything"
    Then the selection is rejected with an error naming the allowed modes

  Scenario: Replace a parsed venue-complex name with Google's bairro
    Given "Downtown Beer Garden" has the neighborhood "Quintal Espinheiro" sourced "parsed"
    And the free-tier address lookup switch is on
    And Google answers that venue's address components with the sublocality "Espinheiro"
    When the address components backfill runs one batch in upgrade mode
    Then "Downtown Beer Garden" has the neighborhood "Espinheiro"
    And that neighborhood is sourced "google"

  Scenario: Never replace an operator-set bairro
    Given "Downtown Beer Garden" has the neighborhood "Espinheiro" sourced "operator"
    And the free-tier address lookup switch is on
    And Google answers that venue's address components with the sublocality "Aflitos"
    When the address components backfill runs one batch in upgrade mode
    Then "Downtown Beer Garden" has the neighborhood "Espinheiro"
    And that neighborhood is sourced "operator"

  Scenario: Never null a stored bairro when Google answers nothing
    Given "Downtown Beer Garden" has the neighborhood "Quintal Espinheiro" sourced "parsed"
    And the free-tier address lookup switch is on
    And Google answers that venue's address components with no components at all
    When the address components backfill runs one batch in upgrade mode
    Then "Downtown Beer Garden" has the neighborhood "Quintal Espinheiro"
    And that neighborhood is sourced "parsed"

  Scenario: Suppress a Google neighbourhood that is only the city name
    Given a servable venue in a municipality that publishes no sublocality
    And the free-tier address lookup switch is on
    And Google answers that venue's address components with the same name for the second-level area and the city
    When the address components backfill runs one batch in upgrade mode
    Then that venue has no neighborhood stored
    And that venue has the city stored

  Scenario: Drop a parsed bairro that is only the city name
    Given a servable venue whose stored city is "Igarassu"
    When the address text parser writes the neighborhood "Igarassu" for that venue
    Then that venue has no neighborhood stored
    And that venue still has the city "Igarassu" stored

  Scenario: Fold case and accents when comparing a bairro to the stored city
    Given a servable venue whose stored city is "São Paulo"
    When the address text parser writes the neighborhood "SAO PAULO" for that venue
    Then that venue has no neighborhood stored

  Scenario: Store a bairro that genuinely differs from the city
    Given a servable venue whose stored city is "Recife"
    When the address text parser writes the neighborhood "Espinheiro" for that venue
    Then that venue has the neighborhood "Espinheiro"
    And that neighborhood is sourced "parsed"

  # A bairro named after its city is not always wrong — Bairro do Recife is a
  # real neighbourhood of Recife. The guard is a heuristic over machine-written
  # values; a human write is trusted verbatim and is the repair path when the
  # heuristic drops a correct value (review finding R08).
  Scenario: Trust an operator's bairro that is named after its city
    Given a servable venue whose stored city is "Recife"
    When an operator writes the neighborhood "Recife" for that venue
    Then that venue has the neighborhood "Recife"
    And that neighborhood is sourced "operator"

  # The write boundary guards every column independently, so a write's own city
  # can lose its precedence check while its neighborhood still lands. The
  # comparison must therefore be against the city the row will actually HOLD
  # after the write, not only against the city the write carries (R11).
  Scenario: Drop a bairro that equals the city the row will keep, even when the write's own city is different
    Given a servable venue whose stored city is "Recife" sourced "google"
    When the address text parser writes the city "Igarassu" and the neighborhood "Recife" for that venue
    Then that venue has no neighborhood stored
    And that venue still has the city "Recife" stored
    And that city is sourced "google"

  # The mirror case: when the incoming city WINS, the row does not end up with
  # neighborhood == city, so a bairro that merely matched the OLD city must be
  # kept. This is the false positive a plain "incoming or stored" rule causes.
  Scenario: Keep a bairro when the write's own city wins and differs from it
    Given a servable venue whose stored city is "Recife" sourced "parsed"
    When Google writes the city "Igarassu" and the neighborhood "Recife" for that venue
    Then that venue has the city "Igarassu" stored
    And that venue has the neighborhood "Recife"

  Scenario: Write the other address fields even when the bairro is dropped
    Given a servable venue whose stored city is "Igarassu"
    When the address text parser writes the neighborhood "Igarassu" and the street "Rua do Sol" for that venue
    Then that venue has no neighborhood stored
    And that venue has the street "Rua do Sol" stored

  Scenario: Make no address lookup at all while the free-tier switch is off
    Given "Downtown Beer Garden" has the neighborhood "Quintal Espinheiro" sourced "parsed"
    And the free-tier address lookup switch is off
    When the address components backfill runs one batch in upgrade mode
    Then no Place Details address lookup is made
    And the address lookup outcome "disabled" is counted
    And "Downtown Beer Garden" still has the neighborhood "Quintal Espinheiro"

  Scenario: Serve the corrected bairro on the next projection cycle
    Given an accepted event at "Downtown Beer Garden" starting 2026-09-06 20:00
    And that venue's stored neighborhood has been corrected to "Espinheiro"
    When the events projection runs
    Then the occurrence payload carries the venue neighborhood "Espinheiro"
    And the occurrence payload still carries every other contracted field

  Scenario: Report the address provenance distribution after a batch
    Given the catalog holds address rows sourced "parsed", "google" and "operator"
    When the address components backfill runs one batch in upgrade mode
    Then the address provenance gauge reports a row count per field and per source
    And the reported counts match the rows actually stored

  Scenario: Report how many stored bairros are still just the city name
    Given the catalog holds two address rows whose stored neighborhood equals its stored city
    When the address components backfill runs one batch in upgrade mode
    Then the neighborhood-equals-city gauge reports 2

  # Both address gauges only ever move at the end of an operator-triggered
  # backfill batch — the job has no scheduler entry — so the reading needs its
  # own age, or a months-old snapshot is indistinguishable from a live one
  # (review finding R10).
  # Rewritten: as first drafted this scenario set the gauge to 0 in its Given
  # and asserted it was 0 in its Then, with no When and no production code in
  # the path — it asserted `Gauge.set` on itself and could not fail. The real,
  # falsifiable claim underneath it is that ONLY a completed backfill batch
  # moves the stamp. Refreshing these gauges from the 2-minute projection
  # cycle was considered and rejected in the plan (it would bolt an
  # address-quality GROUP BY onto the serving projector's all-or-nothing
  # preparation read), so a projection cycle must leave the stamp untouched —
  # which is what makes `time() - <stamp>` a usable staleness alert instead of
  # a number that silently refreshes itself.
  Scenario: Report no refresh timestamp until a backfill batch actually runs
    Given no address components backfill batch has ever run
    And an accepted event at "Downtown Beer Garden" starting 2026-09-06 20:00
    When the events projection runs
    Then the occurrence payload carries no ticket url
    And the address gauges refresh timestamp reports 0

  Scenario: Stamp when the address gauges were last refreshed
    Given no address components backfill batch has ever run
    When the address components backfill runs one batch in upgrade mode
    Then the address gauges refresh timestamp reports that batch's time

  # ── nightlife day: last night's recurring party is still indexed ─────────

  Scenario: Keep last night's recurring occurrence in the index at 00:30 local
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "toda quarta" and whose resolved time is 22:00
    And the current time is 2026-09-10 00:30 in Recife
    When the events projection runs
    Then the city index for "recife" contains the occurrence for 2026-09-09
    And that occurrence has a payload key
    And that occurrence is scored by its own start time of 2026-09-09 22:00 in Recife

  Scenario: Keep last night's recurring occurrence in the venue index too
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "toda quarta" and whose resolved time is 22:00
    And the current time is 2026-09-10 00:30 in Recife
    When the events projection runs
    Then the venue index for "Downtown Beer Garden" contains the occurrence for 2026-09-09

  Scenario: Prune last night's recurring occurrence after 06:00 local
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "toda quarta" and whose resolved time is 22:00
    And the events projection has already run at 2026-09-10 00:30 in Recife
    And the current time is 2026-09-10 06:30 in Recife
    When the events projection runs again
    Then the city index for "recife" does not contain the occurrence for 2026-09-09
    And the venue index for "Downtown Beer Garden" does not contain the occurrence for 2026-09-09
    And the occurrence for 2026-09-09 has no payload key

  Scenario: Roll the near edge back only while the local hour is below the cutoff
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "toda quarta" and whose resolved time is 22:00
    And the current time is 2026-09-10 05:59 in Recife
    When the events projection runs
    Then the city index for "recife" contains the occurrence for 2026-09-09

  Scenario: Keep the forward horizon unchanged during the small hours
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "todo dia" and whose resolved time is 22:00
    And the current time is 2026-09-10 00:30 in Recife
    When the events projection runs
    Then the city index for "recife" contains the occurrence for 2026-10-01
    And the city index for "recife" does not contain the occurrence for 2026-10-02

  Scenario: Keep the same forward horizon after the cutoff has passed
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "todo dia" and whose resolved time is 22:00
    And the current time is 2026-09-10 06:30 in Recife
    When the events projection runs
    Then the city index for "recife" contains the occurrence for 2026-10-01
    And the city index for "recife" does not contain the occurrence for 2026-10-02

  Scenario: Keep last night's non-recurring occurrence in the index as well
    Given an accepted event at "Downtown Beer Garden" starting 2026-09-09 22:00
    And the current time is 2026-09-10 00:30 in Recife
    When the events projection runs
    Then the city index for "recife" contains the occurrence for 2026-09-09

  Scenario: Count the cycles that ran with the nightlife day rolled back
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "toda quarta" and whose resolved time is 22:00
    And the current time is 2026-09-10 00:30 in Recife
    When the events projection runs
    Then the nightlife rollback counter is bumped once

  Scenario: Do not count a rollback for a cycle that ran after the cutoff
    Given an accepted recurring event at "Downtown Beer Garden" whose recurrence text is "toda quarta" and whose resolved time is 22:00
    And the current time is 2026-09-10 06:30 in Recife
    When the events projection runs
    Then the nightlife rollback counter is not bumped

  # ── city vocabulary: what vibes_bot validates an incoming city against ───

  Scenario: Remember a configured geo-fence city that holds no events
    Given the geo fence configures the cities "recife" and "joao-pessoa"
    And every accepted event is in Recife
    When the events projection runs
    Then the known events cities are "joao-pessoa" and "recife"

  Scenario: Keep remembering a city after its last occurrence is gone
    Given the geo fence configures the cities "recife" and "joao-pessoa"
    And the events projection has already run once
    When the geo fence is reduced to the city "recife"
    And the events projection runs again
    Then the known events cities are "joao-pessoa" and "recife"

  Scenario: Rebuild the whole vocabulary on the next cycle after the set is erased
    Given the geo fence configures the cities "recife" and "joao-pessoa"
    And the events projection has already run once
    When the known events cities set is erased
    And the events projection runs again
    Then the known events cities are "joao-pessoa" and "recife"

  # The configured-slug loop is ADDITIVE and best-effort, and it runs
  # BEFORE the write pass (redis_projection_service.py:397 vs :516), so an
  # unguarded raise there would lose EVERY occurrence in the cycle. It is
  # also the only writer for a configured city that has no events tonight —
  # a slug `index_event_occurrence` will never be asked to remember — which
  # is exactly the failure this guard has to absorb without costing a cycle
  # that would otherwise serve.
  Scenario: Keep projecting when remembering a configured city fails
    Given the geo fence configures the cities "recife" and "joao-pessoa"
    And every accepted event is in Recife
    And remembering the configured city "joao-pessoa" fails
    When the events projection runs
    Then remembering that configured city was attempted and failed
    And the projection still writes every accepted occurrence
    And the projection cycle is not reported as failed

  # The OPPOSITE contract, and deliberately so — this one must NOT be
  # guarded. `index_event_occurrence` (redis_venue_dao.py:1429-1442) writes
  # the two index memberships and the known-cities membership in ONE call
  # precisely so the durability write can never lag the index write it
  # exists to protect. vibes_bot validates an incoming `city` against that
  # set, so an occurrence indexed under a slug that was never remembered
  # would make it 422 a city that genuinely has events. Swallowing this
  # failure would hide that divergence; failing the cycle surfaces it, and
  # main.py:321 already isolates the raise from the venue projection.
  Scenario: Fail the cycle when the city an occurrence is indexed under cannot be remembered
    Given the geo fence configures the cities "recife" and "joao-pessoa"
    And every accepted event is in Recife
    And remembering any city slug fails
    When the events projection runs and is allowed to fail
    Then the projection cycle fails loudly instead of returning a summary
    And the divergence that failure exists to surface is visible
