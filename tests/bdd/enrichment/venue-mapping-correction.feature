@wip
Feature: Venue-mapping correction — drop a spurious handle mapping, remove a wrong force-assign, and backfill the events already written under it

  As an operator who has confirmed, by reading real posts, that a
  kind='venue' crawl target's own instagram.handle mapping is wrong —
  either a spurious second venue diluting/blocking auto-link for every
  event under that handle, or a force-assigned single venue that is not
  what the handle's posts actually describe
  I want the mapping corrected and every already-written event backfilled
  to match, without inventing a second, duplicate sweep mechanism
  So that the correction is idempotent, reviewable one handle at a time,
  and reuses the one existing repair tool this repo already has for this
  class of defect

  This feature pins the two new correction shapes
  plans/260914_recife-venue-mapping-corrections.md ships on top of the
  existing scripts/backfill_event_venue_links.py: force-reassign (an
  unconditional, operator-pinned venue_id write — used both to detach a
  wrong force-assigned mapping and to force-assign a handle's now-single
  remaining venue) and unresolved-venue (re-running the existing,
  unmodified resolution ladder against events that were never resolved at
  all). Neither mode ever re-scores an event that already has a specific,
  different venue_id, and neither ever overrides an operator's own prior
  edit.

  Background:
    Given the venue-mapping-correction fixture corpus of re-verified real cases

  # ── dropping a spurious duplicate mapping unblocks stuck events ─────────

  Scenario: Dropping a spurious duplicate venue mapping frees a handle down to one real candidate
    Given a handle currently mapped to two venues, one correct and one spurious and unrelated
    And every one of the handle's live events is stuck unresolved because neither mapped venue's name alone clears the auto-link floor
    When the spurious venue's mapping is dropped for that handle
    Then the handle is mapped to exactly the one correct venue
    And a future crawl of that handle would force-assign its posts directly, with no scored ladder involved

  Scenario: Force-reassigning a handle's stuck events after the mapping fix serves them under the correct venue
    Given a handle whose spurious mapping has already been dropped, leaving one correct venue
    And every one of the handle's events is still stuck with no venue from before the fix
    When the force-reassign correction runs for that handle from unresolved to its one correct venue
    Then every one of those events is reattributed to the correct venue
    And an event that carries no other review reason is served
    And an event that still carries an unrelated review reason stays out of serving for that reason alone

  Scenario: The same correction shape generalizes to a second, independently confirmed handle
    Given a second, unrelated handle whose spurious mapping names a real venue in a different city entirely
    And that handle already has one event correctly resolved to the right venue by an unrelated mechanism
    When the force-reassign correction runs for that handle from unresolved to its one correct venue
    Then every one of that handle's still-unresolved events is reattributed to the correct venue
    And the event already resolved by the unrelated mechanism is left exactly as it was

  # ── removing a wrong force-assigned mapping from a multi-venue account ──

  Scenario: Removing a bulletin account's force-assigned mapping detaches its already-wrong events
    Given a handle force-assigned to a single venue though its posts describe many different real places
    And every one of the handle's live events currently carries that one venue, right or wrong
    When the venue mapping is removed for that handle
    And the force-reassign correction runs for that handle from its old venue to no venue
    Then every one of the handle's events is detached from that venue
    And an event with no venue is not served until something else resolves it

  Scenario: A detached event gets a fair, catalog-wide shot at auto-resolution
    Given a handle's events have just been detached from a wrong force-assigned venue
    When the unresolved-venue correction runs for that handle
    Then an event whose own text clearly and specifically names a real, catalogued venue is reattributed to it
    And an event whose own text names no specific place, or only echoes the account's own generic branding, remains unresolved

  # ── repointing a wrong single mapping to the correct venue ──────────────
  # No live, independently-confirmed case of this exact shape exists at
  # plan time (the task's intended real-world target for this scenario,
  # casabacurau, was investigated and refuted — see the plan's Evidence).
  # This scenario is pinned against a synthetic, clearly-labelled fixture
  # so the mechanism is proven and ready for a future confirmed case.

  Scenario: Repointing a single wrong venue mapping to the correct venue reattributes its events
    Given a synthetic handle force-assigned to the wrong single venue
    And the correct venue is already known and already in the catalog
    When the force-reassign correction runs for that handle from the wrong venue to the correct one
    Then every one of the handle's events is reattributed to the correct venue
    And none of them is left pointing at the wrong venue

  # ── protections shared with the existing repair modes ───────────────────

  Scenario: An operator's own venue correction is never overridden by either new mode
    Given an event whose venue_id was corrected by an operator directly
    When either new correction mode is run over the handle that event belongs to
    Then that event's venue_id is left exactly as the operator set it

  Scenario: A confirmed, manually-linked, or superseded event is skipped by either new mode
    Given an event that is confirmed, manually linked, or superseded
    When either new correction mode is run over the handle that event belongs to
    Then that event is skipped and reported as skipped, not silently ignored

  # ── idempotency ──────────────────────────────────────────────────────────

  Scenario: Running the force-reassign correction a second time changes nothing
    Given a handle whose events were already corrected by a first force-reassign run
    When the force-reassign correction runs again for the same handle with the same correction
    Then no event's venue_id, status, or review reason changes
    And no row is written

  Scenario: Running the unresolved-venue correction a second time changes nothing
    Given a handle whose events were already evaluated by a first unresolved-venue run
    When the unresolved-venue correction runs again for the same handle
    Then no event that stayed unresolved the first time is written to again
    And no event that resolved the first time is re-evaluated differently
