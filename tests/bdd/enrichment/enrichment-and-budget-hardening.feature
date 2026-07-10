@wip
Feature: Enrichment and budget hardening
  Google enrichment must detect closures for already-enriched venues without
  re-running full enrichment, must never permanently poison a venue because
  Google failed transiently, and Instagram validation must only delete on
  definitive non-existence. Paid refresh jobs must never run concurrently
  with their scheduled twins, and the manual-add path must keep the monthly
  BestTime ledger exact under undo, races, and timeout recovery.

  Scenario: A permanently closed venue is deprecated by the nightly recheck
    Given "venue-a" was fully enriched in a previous run
    And Google now reports business status "CLOSED_PERMANENTLY" for "venue-a"
    And the business-status recheck flag is enabled
    When the nightly Google enrichment job runs
    Then "venue-a" must be deprecated through the permanently-closed path
    And no full vibe enrichment must be performed for "venue-a"
    And "venue-a" must not appear in the next live refresh selection

  Scenario: A transport error during place search does not poison the venue
    Given "venue-b" has no vibe attributes cached
    And the Google place search fails with a rate-limit error
    When the enrichment job processes "venue-b"
    Then no empty vibe-attributes marker must be written for "venue-b"
    And the run must record the venue as skipped due to error
    And the next enrichment run must process "venue-b" again

  Scenario: A genuine zero-result still writes the no-match marker
    Given "venue-c" has no vibe attributes cached
    And the Google place search returns no results for "venue-c"
    When the enrichment job processes "venue-c"
    Then an empty vibe-attributes marker must be written for "venue-c"

  Scenario: Instagram validation keeps handles on ambiguous failures
    Given "venue-a" has a cached Instagram handle
    And the Instagram profile check returns a rate-limit response
    When the Instagram validation sweep runs
    Then the handle for "venue-a" must be kept

  Scenario: Instagram validation deletes handles only on definitive absence
    Given "venue-a" has a cached Instagram handle
    And the Instagram profile check returns a definitive not-found
    When the Instagram validation sweep runs
    Then the handle for "venue-a" must be soft-deleted

  Scenario: An admin trigger cannot double a scheduled paid refresh
    Given the scheduled live forecast refresh is mid-run
    When an operator triggers the live forecast job via the admin endpoint
    Then the trigger must be refused as already running
    And no additional BestTime calls must be spent by the trigger

  Scenario: Undoing a geo-link releases the slot of the month it consumed
    Given a venue was geo-linked last month consuming last month's budget slot
    When the operator undoes the geo-link this month
    Then last month's counter must be decremented
    And this month's counter must be unchanged

  Scenario: Undo is refused for venues without geo-link provenance
    Given a venue was created through the normal paid add path within 24 hours
    When the operator requests a geo-link undo for that venue
    Then the undo must be rejected
    And no budget counter must change

  Scenario: Concurrent duplicate manual adds spend exactly one create
    Given two identical add requests for the same name and address arrive concurrently
    When both requests are processed
    Then exactly one budget slot must be reserved
    And exactly one paid BestTime create must be issued
    And both requests must resolve to the same venue

  Scenario: Timeout recovery refuses short-name containment matches
    Given a paid create timed out for a venue whose folded name has 4 characters
    And the account inventory contains an unrelated venue whose folded name contains those 4 characters
    When timeout recovery scans the account inventory
    Then the unrelated venue must not be linked
    And the address cache must not be poisoned with the wrong venue id

  Scenario: Fresh photos carry the classifier's category tags
    Given "venue-a" has a vibe profile with categorized evidence photos
    When the fresh photos for "venue-a" are projected and resolved
    Then each resolved photo with a known category must carry that category
