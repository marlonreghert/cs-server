@wip
Feature: Events venue bairro and ticket URL
  As the VibeSense app
  I want every event card to show a real bairro and an openable ticket link
  So that a served event neither names a venue complex as its neighbourhood nor
  offers a link the phone refuses to open

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
