Feature: Give a merged venue-night one display title the app can read
  When five posts about five acts collapse into one listing, the surviving title
  is whichever source was last seen — an arbitrary pick for something a reader
  looks at. Where one source title obviously contains the others, that title is
  chosen deterministically and no model is consulted. Only where no source title
  wins is a model asked, once per merge group, and only ever for the display
  string: it can never cause, prevent or reverse a merge, and an answer that
  invents a word no post contained is thrown away.

  The chosen title reaches the app through the serving projection's existing
  title field. No app-facing field is added and none is removed.

  Background:
    Given the events serving projection is enabled
    And the venue catalog carries "Club Metrópole" as a servable venue
    And "Club Metrópole" runs one night rather than a programme
    And display-title selection is enabled
    And auto-merge is enabled

  # ── the deterministic pick ────────────────────────────────────────────────

  Scenario: Choose the containing title without calling the model
    Given stored events at "Club Metrópole" on one Saturday titled "Rodolpho" and "Rodolpho Produções"
    When the merge pass and the display-title pass run for that venue
    Then the display title is "Rodolpho Produções"
    And no title model call is made

  Scenario: Choose the single title of a group whose titles differ only in case
    Given stored events at "Club Metrópole" on one Saturday titled "NOITE DA PATROA" and "Noite da Patroa"
    When the merge pass and the display-title pass run for that venue
    Then the display title is one of the two stored titles
    And no title model call is made

  Scenario: Never call the model for an event with a single source
    Given a single-source stored event at "Club Metrópole" on one Saturday
    When the display-title pass runs for that venue
    Then no title model call is made
    And the event has no display title

  Scenario: Never call the model for a group whose operator edited its title
    Given stored events at "Club Metrópole" on one Saturday, each naming a different act
    And an operator has edited the title of the surviving event
    When the merge pass and the display-title pass run for that venue
    Then no title model call is made
    And the display title is the operator's title

  # ── the one narrow model call ─────────────────────────────────────────────

  Scenario: Call the model exactly once for a merge group with no obvious title
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the merge pass and the display-title pass run for that venue
    Then exactly one model call is made
    And the call is given all five source titles
    And the display title is the title the model chose

  Scenario: Reject a model title that introduces a word no post contained
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    And the model answers with a title naming an act no post mentioned
    When the merge pass and the display-title pass run for that venue
    Then the event has no display title
    And a rejected display title is counted

  Scenario: Reject a model title that is blank
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    And the model answers with a blank title
    When the merge pass and the display-title pass run for that venue
    Then the event has no display title
    And a rejected display title is counted

  Scenario: Keep the stored title when the model call fails
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    And the model call fails
    When the merge pass and the display-title pass run for that venue
    Then the event has no display title
    And the extraction run still completes

  Scenario: Never let a model answer change which events survive
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    And the model answers with a different title on every call
    When the merge pass and the display-title pass run twice for that venue
    Then the same event survives both times
    And the same events are superseded both times

  # ── what the app reads ────────────────────────────────────────────────────

  Scenario: Serve the chosen display title in the projection's title field
    Given a stored event at "Club Metrópole" carrying a chosen display title
    When the events projection runs over the stored events
    Then the projected occurrence's title is the chosen display title

  Scenario: Serve the stored title when no display title was chosen
    Given a stored event at "Club Metrópole" carrying no display title
    When the events projection runs over the stored events
    Then the projected occurrence's title is the stored title

  Scenario: Serve the merged listing's full lineup alongside its display title
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the merge pass, the display-title pass and the events projection run over the stored events
    Then one occurrence is projected for that Saturday
    And the projected occurrence names every one of the five acts

  # ── staying correct as a group grows ──────────────────────────────────────

  Scenario: Clear a stale display title when a later merge grows the group
    Given a stored event at "Club Metrópole" carrying a chosen display title
    When a later post about that same Saturday is absorbed into it
    Then the event has no display title
    And the next display-title pass chooses a title again

  Scenario: Change nothing on a second display-title pass
    Given five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the merge pass and the display-title pass run, and the display-title pass runs again
    Then only one model call is made in total
    And the display title is unchanged

  Scenario: Change nothing while display-title selection is disabled
    Given display-title selection is disabled
    And five stored events at "Club Metrópole" on one Saturday, each naming a different act
    When the merge pass and the display-title pass run for that venue
    Then no title model call is made
    And the surviving event has no display title
    And the projected occurrence's title is the surviving event's stored title
