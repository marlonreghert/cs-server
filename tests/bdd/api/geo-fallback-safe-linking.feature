Feature: Geo-fallback safe linking — fold+best-match, generic-name guard, undoable links
  As the venue platform
  I must link a rejected add only to the right nearby venue,
  say when a link created a new catalog row,
  let an operator undo a fresh link without poisoning future re-adds,
  refuse to link at all when I don't trust the coordinates a match would rest
  on, and let an operator repair a link that was already cached wrong,
  so a BestTime rejection never silently attaches the wrong place to the
  catalog — and if it already has, an operator can put that right.

  # (a) fold + best-match
  Scenario: An accent-folded name links to BestTime's normalized inventory name
    Given BestTime rejects a create with a venue info block without a venue id
    And the geo fallback offers a candidate whose name differs only by accents and punctuation
    When an operator adds the venue by name and address
    Then the add completes as matched via geo fallback

  Scenario: Among several candidates the best address match is linked
    Given BestTime rejects a create with a venue info block without a venue id
    And the geo fallback offers two same-named candidates at different addresses
    When an operator adds the venue by name and address
    Then the linked venue is the one whose address overlaps the request

  # (b) generic-name guard
  Scenario: A short generic name does not containment-match a longer name
    Given BestTime rejects a create with a venue info block without a venue id
    And the operator's venue name is a short generic word contained in a nearby venue's name
    When an operator adds the venue by name and address
    Then the add fails telling the operator no matching venue was found nearby

  Scenario: An exact short name still matches
    Given BestTime rejects a create with a venue info block without a venue id
    And the geo fallback offers a candidate whose folded name equals the short name exactly
    When an operator adds the venue by name and address
    Then the add completes as matched via geo fallback

  # newly_linked flag
  Scenario: The link outcome says whether a new catalog row was created and why it matched
    Given BestTime rejects a create with a venue info block without a venue id
    And the geo fallback offers a matching candidate not yet in the catalog
    When an operator adds the venue by name and address
    Then the geo fallback outcome reports the venue as newly linked
    And the outcome reports which matching rule linked it

  # (c) undo
  Scenario: Undoing a fresh link removes it from serving and returns the slot
    Given a venue was newly linked via the geo fallback
    When the operator undoes the geo link
    Then the venue is deprecated with the geo-link-undo source
    And the monthly counter slot is returned
    And the venue is no longer eligible for serving

  Scenario: Undo is idempotent
    Given a venue was newly linked via the geo fallback and already undone
    When the operator undoes the geo link again
    Then the undo reports it was already undone
    And the monthly counter is not decremented a second time

  Scenario: Undo is refused for venues older than a day
    Given a venue that has been in the catalog for more than a day
    When the operator undoes the geo link
    Then the undo is rejected with an explanatory error

  Scenario: A re-add after an undo reactivates the venue
    Given a venue was newly linked via the geo fallback and then undone
    When the same venue is added again and BestTime confirms it
    Then the venue is active again in the catalog

  # (d) coordinate-trust gate
  @wip
  Scenario: A geo fallback is not attempted when the coordinates came from an unbiased search
    Given BestTime rejects a create with a venue info block without a venue id
    And the request's coordinates were resolved from a text search with no place id and no caller-supplied location
    And a nearby venue would otherwise have matched by name
    When an operator adds the venue by name and address
    Then no geo fallback link is attempted
    And the add falls through to a normal create instead
    And no address lookup entry is cached for this attempt

  @wip
  Scenario: A geo fallback still links when the coordinates are caller-supplied
    Given BestTime rejects a create with a venue info block without a venue id
    And the request carries a caller-supplied latitude and longitude
    And the geo fallback offers a matching candidate nearby
    When an operator adds the venue by name and address
    Then the add completes as matched via geo fallback

  @wip
  Scenario: A geo fallback still links when the coordinates came from a caller-supplied place id
    Given BestTime rejects a create with a venue info block without a venue id
    And the request's coordinates were resolved from a caller-supplied place id
    And the geo fallback offers a matching candidate nearby
    When an operator adds the venue by name and address
    Then the add completes as matched via geo fallback

  # (e) address-cache repair
  @wip
  Scenario: An operator repairs an address lookup entry that was cached wrong
    Given a venue name and address are cached against the wrong venue id
    When the operator clears the address lookup entry for that name and address
    Then the cleared entry's previous venue id is reported
    And a subsequent add for that same name and address no longer resolves to the old venue

  @wip
  Scenario: Clearing an address lookup entry that was never cached reports nothing to clear
    Given no address lookup entry exists for a given name and address
    When the operator clears the address lookup entry for that name and address
    Then the response reports nothing was cached
    And no error is raised
