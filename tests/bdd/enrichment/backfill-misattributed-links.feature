Feature: Backfill mis-attributed venue links
  As an operator
  I want every item whose venue came from a post-level caption mention re-decided
  from the event's own stored location text
  So that a roundup post's events stop being attributed to a venue that is not
  hosting them, without any operator decision being overwritten

  Background:
    Given the per-event venue attribution fix has landed
    And the venue catalog holds "Sempre Rock Bar" with Instagram handle "semprerockbar"
    And the venue catalog holds "Casa do Matuto" with Instagram handle "casadomatutonatal"
    And the venue catalog holds "Teatro Riachuelo" with Instagram handle "teatroriachuelonatal"

  Scenario: Re-point an item to the venue named in its own stated location
    Given an accepted item linked to "Teatro Riachuelo" by a caption mention
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is linked to "Sempre Rock Bar"
    And the item is not linked to "Teatro Riachuelo"
    And the item records that the link came from its own stated location

  Scenario: Re-point every event of one roundup post to its own venue
    Given a roundup post whose twenty items are all linked to "Teatro Riachuelo" by a caption mention
    And each item's stored extraction states a different venue handle
    When the backfill runs with apply
    Then each item is linked to the venue its own stored location names
    And no two of those items share a venue unless their stored locations agree

  Scenario: Detach an item whose stated venue is not in the catalog
    Given an accepted item linked to "Sempre Rock Bar" by a caption mention
    And its stored extraction states the location "@donanapubnatal"
    And no venue in the catalog has the handle "donanapubnatal"
    When the backfill runs with apply
    Then the item has no venue
    And the item is queued with the reason that its venue is not in the catalog
    And the item is not queued with the reason that its venue could not be resolved

  Scenario: Report an item with no stated location as unresolved, not as absent from the catalog
    Given an accepted item linked to "Sempre Rock Bar" by a caption mention
    And its stored extraction states no location at all
    When the backfill runs with apply
    Then the item has no venue
    And the item is queued with the reason that its venue could not be resolved
    And the item is not queued with the reason that its venue is not in the catalog

  Scenario: Leave a confirmed item untouched
    Given a confirmed item linked to "Teatro Riachuelo" by a caption mention
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is still linked to "Teatro Riachuelo"
    And the item is still confirmed
    And the report counts the item as skipped because an operator confirmed it

  Scenario: Leave a manually linked item untouched
    Given a manually linked item linked to "Teatro Riachuelo"
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is still linked to "Teatro Riachuelo"
    And the item's location resolution is still manual
    And the report counts the item as skipped because an operator linked it

  Scenario: Leave an item whose operator edited its venue untouched
    Given an accepted item linked to "Teatro Riachuelo" by a caption mention
    And an operator has edited the item's venue
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is still linked to "Teatro Riachuelo"
    And the report counts the item as skipped because an operator edited its venue

  Scenario: Re-resolve from an operator-corrected location text
    Given an accepted item linked to "Teatro Riachuelo" by a caption mention
    And an operator has corrected the item's location text to "@casadomatutonatal"
    When the backfill runs with apply
    Then the item is linked to "Casa do Matuto"
    And the item's location resolution is automatic

  Scenario: Leave a superseded item untouched
    Given a superseded item linked to "Teatro Riachuelo" by a caption mention
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is still linked to "Teatro Riachuelo"
    And the item is still superseded
    And the report counts the item as skipped because it was superseded

  Scenario: Leave an extraction-failed placeholder untouched
    Given an extraction-failed item with no content identity
    When the backfill runs with apply
    Then the item is unchanged
    And the report counts the item as skipped because its extraction failed

  Scenario: Keep an item queued for a date problem queued after its venue is fixed
    Given a pending item linked to "Teatro Riachuelo" by a caption mention
    And the item is queued because its date could not be read
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is linked to "Sempre Rock Bar"
    And the item is still pending review
    And the item is still queued because its date could not be read

  Scenario: Return a repaired item to accepted only when the extraction is otherwise clean
    Given a pending item linked to "Teatro Riachuelo" by a caption mention
    And the item is queued only because its venue could not be resolved
    And the item has a resolved date and confidence above the floor
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is linked to "Sempre Rock Bar"
    And the item is accepted
    And the item has no review reason

  Scenario: Keep a repaired low-confidence item queued
    Given a pending item linked to "Teatro Riachuelo" by a caption mention
    And the item has confidence below the floor
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs with apply
    Then the item is linked to "Sempre Rock Bar"
    And the item is still pending review

  Scenario: Report every change and write nothing on a dry run
    Given an accepted item linked to "Teatro Riachuelo" by a caption mention
    And its stored extraction states the location "@semprerockbar"
    When the backfill runs without apply
    Then the item is still linked to "Teatro Riachuelo"
    And the report names the item, its current venue and the venue it would move to

  Scenario: Change nothing on a second apply run
    Given an accepted item linked to "Teatro Riachuelo" by a caption mention
    And its stored extraction states the location "@semprerockbar"
    And the backfill has already run with apply
    When the backfill runs with apply again
    Then the item is linked to "Sempre Rock Bar"
    And the report counts no changes

  Scenario: Refuse to spend anything on external services
    Given a roundup post whose items are all linked by a caption mention
    When the backfill runs with apply
    Then no Apify request is made
    And no OpenAI request is made
    And no archived post is read from storage

  Scenario: Report an identity collision created by a repair without merging
    Given two accepted items linked to "Teatro Riachuelo" by a caption mention
    And both stored extractions state the location "@semprerockbar"
    And both items share a date and a title
    When the backfill runs with apply
    Then both items are linked to "Sempre Rock Bar"
    And both items still exist
    And the report names the pair as a venue identity collision

  Scenario: Report a detached item that a resolved sibling could later absorb
    Given an accepted item linked to "Teatro Riachuelo" by a caption mention
    And its stored extraction states a venue that is not in the catalog
    And another item from the same account shares its date and title and is linked to a venue
    When the backfill runs with apply
    Then the detached item has no venue
    And the report names the pair as a handle identity collision

  Scenario: Report the handles that detached items point at
    Given twelve accepted items linked by a caption mention whose stated venues are not in the catalog
    When the backfill runs without apply
    Then the report lists each stated handle with the number of items it would recover
    And the list is ordered by that number

  Scenario: Fail loudly when the reported totals do not balance
    Given a backfill run whose per-outcome counts do not sum to the rows it selected
    When the backfill finishes
    Then the backfill exits with a failure
    And the report says the totals did not balance

  Scenario: Fail loudly when a write affects no rows
    Given an accepted item linked to "Teatro Riachuelo" by a caption mention
    And the update of that item affects no rows
    When the backfill runs with apply
    Then the backfill exits with a failure
    And the report names the item whose write did nothing

  Scenario: Refuse to run against a build without the per-event attribution fix
    Given the per-event venue attribution fix has not landed
    When the backfill is started
    Then the backfill exits with a failure before reading any item
    And no item is changed
