Feature: Stop one venue-night producing several listings
  As an operator of the event pipeline
  I want an event filed at the venue its own text names, two announcements of
  one weekly night recognised as one night, and a club's same-night rows
  collapsed where that venue runs one night rather than a programme
  So that the Eventos tab stops showing several cards for one place on one
  evening, while a venue that genuinely runs four workshops on a Tuesday still
  shows four

  Three independent defects produce that one symptom, and this feature pins all
  three apart from each other. Every widened behaviour is gated and ships off,
  because auto-merge is already live in production: with no configuration set,
  every scenario here must leave the corpus exactly as it is today.

  Background:
    Given the event extraction pipeline is configured for a known venue
    And the venue catalog carries "BeerDock Boa Viagem" at an address in Boa Viagem
    And the venue catalog carries "BeerDock Casa Forte" at an address in Casa Forte
    And the Instagram handle "beerdock_recife" maps only to "BeerDock Boa Viagem"
    And the candidate window is 8 hours

  # ── Defect 2: the event's own location text disagrees with its handle's venue ──

  Scenario: Flag an event whose own location text names a different known venue by handle
    Given "BeerDock Casa Forte" is reachable by the handle "beerdock.casaforte"
    When a post from "beerdock_recife" announces an event whose location text is "@beerdock.casaforte"
    Then the event stays attributed to "BeerDock Boa Viagem"
    And the event carries the review reason "location_text_disputes_venue"
    And the event offers "BeerDock Casa Forte" as a ranked venue candidate
    And an attribution dispute is counted for the handle-mention method

  Scenario: Flag an event whose own location text names a sibling branch's neighbourhood
    When a post from "beerdock_recife" announces an event whose location text is "CASA FORTE"
    Then the event stays attributed to "BeerDock Boa Viagem"
    And the event carries the review reason "location_text_disputes_venue"
    And the event offers "BeerDock Casa Forte" as a ranked venue candidate

  Scenario: Re-attribute the event when the dispute action is set to reattribute
    Given the attribution dispute action is "reattribute"
    When a post from "beerdock_recife" announces an event whose location text is "CASA FORTE"
    Then the event is attributed to "BeerDock Casa Forte"
    And the event records that its venue came from a neighbourhood match

  Scenario: Never dispute an attribution on a fuzzy name match alone
    Given the venue catalog carries "Maria Café" at an unrelated address
    When a post from "beerdock_recife" announces an event whose location text is "nosso cafe"
    Then the event stays attributed to "BeerDock Boa Viagem"
    And the event carries no review reason
    And no attribution dispute is counted

  Scenario: Never dispute an attribution on a post caption mention alone
    Given "BeerDock Casa Forte" is reachable by the handle "beerdock.casaforte"
    When a post from "beerdock_recife" whose caption mentions "@beerdock.casaforte" announces an event with no location text
    Then the event stays attributed to "BeerDock Boa Viagem"
    And the event carries no review reason

  Scenario: Leave a single-venue handle's ordinary post exactly as it is today
    When a post from "beerdock_recife" announces an event with no location text
    Then the event is attributed to "BeerDock Boa Viagem"
    And the event carries no review reason
    And the event is accepted without review

  Scenario: Record a venue-acquisition entry when the named branch is not in the catalog
    When a post from "beerdock_recife" announces an event whose location text is "@beerdock.madalena"
    Then the event stays attributed to "BeerDock Boa Viagem"
    And the event carries the review reason "location_text_disputes_venue"
    And the handle "beerdock.madalena" appears in the venue-acquisition backlog

  Scenario: Withhold a disputed event from serving only when withholding is enabled
    Given the attribution dispute withholding is enabled
    When a post from "beerdock_recife" announces an event whose location text is "CASA FORTE"
    Then the disputed event is awaiting review
    And the event is not selectable for the serving projection

  Scenario: Keep serving a disputed event while withholding is disabled
    When a post from "beerdock_recife" announces an event whose location text is "CASA FORTE"
    Then the disputed event is still accepted
    And the event is selectable for the serving projection

  Scenario: Never dispute an event whose operator edited its venue
    Given an operator has corrected the venue of a stored event to "BeerDock Boa Viagem"
    When that post is re-extracted with the location text "CASA FORTE"
    Then the event stays attributed to "BeerDock Boa Viagem"
    And the event carries no dispute review reason

  # ── Defect 2: repairing what is already stored ────────────────────────────

  Scenario: Repair a stored mis-attribution through the disputed-location-text backfill
    Given a stored event at "BeerDock Boa Viagem" whose recorded location text is "CASA FORTE"
    When the disputed-location-text backfill runs with apply
    Then the event is attributed to "BeerDock Casa Forte"
    And the event's title and start time are unchanged

  Scenario: Report every repair and write nothing when the backfill runs without apply
    Given a stored event at "BeerDock Boa Viagem" whose recorded location text is "CASA FORTE"
    When the disputed-location-text backfill runs without apply
    Then the report names the event and its proposed venue
    And the event is still attributed to "BeerDock Boa Viagem"

  Scenario: Change nothing on a second run of the disputed-location-text backfill
    Given a stored event at "BeerDock Boa Viagem" whose recorded location text is "CASA FORTE"
    When the disputed-location-text backfill runs with apply twice
    Then the second run repairs nothing
    And the event is attributed to "BeerDock Casa Forte"

  Scenario: Never merge anything from the attribution backfill
    Given two stored events at "BeerDock Boa Viagem" whose recorded location text is "CASA FORTE" and whose titles are identical
    When the disputed-location-text backfill runs with apply
    Then both events are attributed to "BeerDock Casa Forte"
    And both events still exist as separate rows

  # ── Defect 3: two announcements of one weekly night ───────────────────────

  Scenario: Merge two weekly announcements of one night stored three weeks apart
    Given the recurring candidate window is enabled
    And auto-merge is enabled
    And a stored weekly event "Aula de FORRÓ na Sala de Reboco" recurring every Wednesday, stored for the 5th
    And a stored weekly event "Aula de FORRÓ na Sala de Reboco" recurring every Wednesday, stored for the 26th
    When the merge pass runs for that venue
    Then one weekly event survives titled "Aula de FORRÓ na Sala de Reboco"
    And the absorbed weekly event is superseded rather than deleted
    And the surviving event still recurs every Wednesday

  Scenario: Refuse to widen the window while the recurring candidate window is disabled
    Given auto-merge is enabled
    And a stored weekly event "Aula de FORRÓ na Sala de Reboco" recurring every Wednesday, stored for the 5th
    And a stored weekly event "Aula de FORRÓ na Sala de Reboco" recurring every Wednesday, stored for the 26th
    When the merge pass runs for that venue
    Then both weekly events survive

  Scenario: Refuse to merge two weekly announcements on different weekdays
    Given the recurring candidate window is enabled
    And auto-merge is enabled
    And a stored weekly event "Noite de Samba" recurring every Wednesday
    And a stored weekly event "Noite de Samba" recurring every Friday
    When the merge pass runs for that venue
    Then both weekly events survive

  Scenario: Merge two weekly announcements whose weekday sets overlap on one day
    Given the recurring candidate window is enabled
    And auto-merge is enabled
    And a stored weekly event "Noite de Samba" recurring every Wednesday
    And a stored weekly event "Noite de Samba" recurring on Wednesdays and Fridays
    When the merge pass runs for that venue
    Then one weekly event survives titled "Noite de Samba"

  Scenario: Refuse to pair a recurring event with a one-off event
    Given the recurring candidate window is enabled
    And auto-merge is enabled
    And a stored weekly event "Noite de Samba" recurring every Wednesday
    And a stored one-off event "Noite de Samba" on a Wednesday three weeks later
    When the merge pass runs for that venue
    Then both events survive

  Scenario: Keep the surviving weekly event serving the nights it served before
    Given the recurring candidate window is enabled
    And auto-merge is enabled
    And two stored weekly events "Aula de FORRÓ na Sala de Reboco" recurring every Wednesday, stored three weeks apart
    When the merge pass runs for that venue
    Then the surviving event is served on every Wednesday inside the projection horizon
    And no Wednesday inside the horizon serves two listings for that venue

  # ── Defect 1: the bar for collapsing a venue-night ────────────────────────

  Scenario: Collapse a venue-night to one listing for a venue on the single-night list
    Given "Club Metrópole" runs one night rather than a programme
    And auto-merge is enabled
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the merge pass runs for that venue
    Then one event survives for that Saturday
    And the surviving event names every one of the five acts in its lineup
    And every absorbed event is superseded rather than deleted

  Scenario: Leave a venue-night alone for a venue that is not on the single-night list
    Given auto-merge is enabled
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the merge pass runs for that venue
    Then five events survive for that Saturday

  Scenario: Refuse to collapse a programme venue's same-night workshops
    Given auto-merge is enabled
    And stored events "Oficina Vida de Inseto", "Oficina Cobra Gigante" and "Oficina de Sorvete" at "Entre Amigos O Bode" on one day
    When the merge pass runs for that venue
    Then three events survive
    And no merge suggestion is recorded for any pair of them

  Scenario: Never absorb a confirmed event at a single-night venue
    Given "Club Metrópole" runs one night rather than a programme
    And auto-merge is enabled
    And two stored events at "Club Metrópole" on one Saturday, both confirmed by an operator
    When the merge pass runs for that venue
    Then both events survive

  Scenario: Never absorb an event whose operator edited its title at a single-night venue
    Given "Club Metrópole" runs one night rather than a programme
    And auto-merge is enabled
    And two stored events at "Club Metrópole" on one Saturday, one of whose titles an operator edited
    When the merge pass runs for that venue
    Then both events survive
    And a merge suggestion is recorded for the pair

  Scenario: Never absorb a non-event post at a single-night venue
    Given "Club Metrópole" runs one night rather than a programme
    And auto-merge is enabled
    And a stored event and a stored birthday greeting at "Club Metrópole" on one Saturday
    When the merge pass runs for that venue
    Then the greeting survives as its own row

  Scenario: Merge a pair sharing one performer only when the lineup threshold is one
    Given auto-merge is enabled
    And the lineup threshold is 1
    And two stored events at "Club Metrópole" on one Saturday sharing exactly one performer
    When the merge pass runs for that venue
    Then one event survives for that Saturday

  Scenario: Refuse to merge a pair sharing one performer at the shipped threshold
    Given auto-merge is enabled
    And two stored events at "Club Metrópole" on one Saturday sharing exactly one performer
    When the merge pass runs for that venue
    Then both events survive

  Scenario: Keep the exact-identity merge behaving exactly as it does today
    Given auto-merge is enabled
    And two stored events at "Club Metrópole" on one Saturday with the same title and the same date
    When the merge pass runs for that venue
    Then one event survives for that Saturday
    And the absorbed event is deleted rather than superseded

  # ── the one-off historical sweep ──────────────────────────────────────────

  Scenario: Sweep an existing cluster that no crawl has touched
    Given "Club Metrópole" runs one night rather than a programme
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the dedup sweep runs with apply for that single-night venue
    Then one event survives for that Saturday
    And the report names the surviving title and every absorbed title

  Scenario: Write nothing and fail when the sweep exceeds its auto-pair ceiling
    Given "Club Metrópole" runs one night rather than a programme
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the dedup sweep runs with apply and an auto-pair ceiling of 1
    Then the sweep writes nothing
    And the sweep reports failure
    And five events survive for that Saturday

  Scenario: Change nothing on a second sweep
    Given "Club Metrópole" runs one night rather than a programme
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the dedup sweep runs with apply twice for that single-night venue
    Then the second sweep merges nothing
    And one event survives for that Saturday

  Scenario: Write nothing when the sweep runs without apply
    Given "Club Metrópole" runs one night rather than a programme
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the dedup sweep runs without apply for that single-night venue
    Then the report names every pair it would merge
    And five events survive for that Saturday

  Scenario: Sweep only the auto band, never the suggest band
    Given two stored events at "Sempre Rock Bar" on one day whose titles differ only in the speaker's name
    When the dedup sweep runs with apply
    Then both events survive
    And no merge suggestion is recorded for the pair

  Scenario: Reverse a swept merge and restore the absorbed event with its sources
    Given "Club Metrópole" runs one night rather than a programme
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    And the dedup sweep has run with apply for that single-night venue
    When an operator reverses the merge that absorbed one of those events
    Then that event is readable again with its original status
    And the sources that moved to the survivor are attached to it again

  # ── the deploy must change nothing ────────────────────────────────────────

  Scenario: Change no stored row when no new configuration is set
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    And two stored weekly events "Aula de FORRÓ na Sala de Reboco" recurring every Wednesday, stored three weeks apart
    And a stored event at "BeerDock Boa Viagem" whose recorded location text is "CASA FORTE"
    When the merge pass runs for every venue
    Then every stored event is unchanged
