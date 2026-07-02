Feature: Geo-fallback safe linking — fold+best-match, generic-name guard, undoable links
  As the venue platform
  I must link a rejected add only to the right nearby venue,
  say when a link created a new catalog row,
  and let an operator undo a fresh link without poisoning future re-adds,
  so a BestTime rejection never silently attaches the wrong place to the catalog.

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
