Feature: Agentic venue-link fallback — an audit-sourced, address-aware reviewer that closes its own false positives
  As an operator triaging the venue-handle link audit
  I want a second, agentic look at every flagged (handle, venue) pair, given the
  venue's real street address and not just its name and neighbourhood
  So that a pair the audit flagged only because its textual check cannot see an
  address is confirmed and stops being flagged, a pair whose posts genuinely
  name a different place stays flagged with the reason recorded, and every
  verdict the machine reached is durably visible to me afterwards

  See plans/260914_agentic-venue-resolution-fallback.md. This pass takes its
  input from `collect_venue_link_audit`'s own flagged pairs, never from an
  extraction run's touched event ids — measured live, 8 of the 9 named handles
  have zero `event_venue_link_candidate` rows at all, because a single-mapped
  `kind='venue'` crawl target force-assigns its events and never runs the
  resolution ladder. The candidate set here is the handle's own currently-mapped
  venue(s), the identical closed set the audit already compares against; the
  catalog is never searched. This pass writes no `venue_id`, no
  `location_resolution`, and touches no `events.post_item` row — across all 12
  live flagged pairs, zero call for a reattribution. Both new flags ship False.

  Background:
    Given the venue-link-audit reviewer fixture corpus of re-verified real pairs
    And the venue-link-audit reviewer flag is enabled
    And the venue-link-audit reviewer auto-apply flag is enabled

  # ── the address is the signal the audit's own check cannot see ─────────────

  Scenario: A flagged pair whose posts give the venue's own street address is confirmed and stops being flagged
    Given a flagged pair whose posts repeatedly give the mapped venue's own street and number
    When the reviewer runs over the audit's flagged pairs
    Then the pair's recorded verdict is "confirm"
    And the recorded matched field is "street"
    And the recorded evidence quote appears verbatim in one of the pair's location texts
    And the pair no longer appears in the audit's flagged output

  Scenario: A pair whose catalogued neighbourhood is wrong but whose street matches is confirmed
    Given a flagged pair whose catalogued neighbourhood disagrees with every post
    And whose posts give the mapped venue's own street and number
    When the reviewer runs over the audit's flagged pairs
    Then the pair's recorded verdict is "confirm"
    And the recorded matched field is "street"
    And the review record notes that the catalogued neighbourhood was not the matching field

  Scenario: A pair matched only on a shortened form of the venue's own name is confirmed
    Given a flagged pair whose posts name a short form contained in the mapped venue's own long catalogue name
    When the reviewer runs over the audit's flagged pairs
    Then the pair's recorded verdict is "confirm"
    And the recorded matched field is "name"
    And the pair no longer appears in the audit's flagged output

  Scenario: The venue's street reaches the model alongside its name, neighbourhood and city
    Given a flagged pair whose mapped venue has a full structured address
    When the reviewer builds the evidence for that pair
    Then the evidence includes the venue's street, neighbourhood and city
    And the evidence includes both the corroborating and the non-corroborating location texts

  Scenario: A pair whose venue has no stored address is still reviewed on name and neighbourhood alone
    Given a flagged pair whose mapped venue has no stored address row
    When the reviewer runs over the audit's flagged pairs
    Then the reviewer still reaches a recorded outcome for that pair
    And the reviewer run does not fail

  # ── a contradiction escalates, and never force-fits a venue ────────────────

  Scenario: A pair whose posts give a different street is left flagged and marked as needing a new catalogue venue
    Given a flagged pair whose posts consistently give a street and neighbourhood that are not the mapped venue's
    When the reviewer runs over the audit's flagged pairs
    Then the pair's recorded verdict is "contradict"
    And the pair still appears in the audit's flagged output
    And the review record states that the mapped venue is not the place the posts describe
    And no event row's venue is changed

  # ── the gates that decide whether a verdict may act ────────────────────────

  Scenario: A handle mapped to more than one venue is never auto-applied even when every call agrees
    Given a flagged handle mapped to two sibling venues
    And every independent call returns "confirm" for both siblings
    When the reviewer runs over the audit's flagged pairs
    Then neither sibling is suppressed from the audit's flagged output
    And each sibling's recorded outcome is "skipped_multi_mapped"
    And no model call is charged for either sibling

  Scenario: A pair whose independent calls disagree is recorded and left flagged
    Given a flagged pair whose independent calls return a mixture of "confirm" and "insufficient"
    When the reviewer runs over the audit's flagged pairs
    Then the pair's recorded outcome is "no_consensus"
    And the review record carries the tally of every verdict returned
    And the pair still appears in the audit's flagged output

  Scenario: A unanimous verdict is accepted only when every independent call also validates
    Given a flagged pair whose independent calls all return "confirm"
    But one of those calls quotes text that was never supplied
    When the reviewer runs over the audit's flagged pairs
    Then the pair is not suppressed from the audit's flagged output
    And the pair's recorded outcome is "validator_rejected"

  # ── the validator gate ─────────────────────────────────────────────────────

  Scenario: A model answer quoting text that was never supplied is rejected and writes nothing
    Given a model answer whose evidence quote appears in no supplied location text
    When the reviewer validates that answer
    Then the answer is rejected for evidence that is not verbatim
    And nothing is written for that pair

  Scenario: A model answer naming an unrecognised verdict is rejected
    Given a model answer whose verdict is not one of confirm, contradict or insufficient
    When the reviewer validates that answer
    Then the answer is rejected as unrecognised
    And nothing is written for that pair

  Scenario: An acting verdict that names no matched field is rejected
    Given a model answer whose verdict is "confirm" and whose matched field is "none"
    When the reviewer validates that answer
    Then the answer is rejected
    And nothing is written for that pair

  Scenario: An empty model response is counted separately and never read as a rejection
    Given a flagged pair whose model call returns an empty response
    When the reviewer runs over the audit's flagged pairs
    Then the pair's recorded outcome is "empty_response"
    And the outcome is not counted as a validator rejection

  # ── the skip table still decides what may be touched ───────────────────────

  Scenario: An event an operator already confirmed is never used as evidence
    Given a flagged pair whose non-corroborating events are all confirmed
    When the reviewer runs over the audit's flagged pairs
    Then those events are not shown to the model
    And the pair's recorded outcome is "skipped_all_events_protected"

  Scenario: An event linked by hand is never used as evidence
    Given a flagged pair carrying an event whose location resolution is manual
    When the reviewer builds the evidence for that pair
    Then that event's location text is not included in the evidence

  Scenario: An event whose venue an operator edited is never used as evidence
    Given a flagged pair carrying an event whose operator-edited fields include its venue
    When the reviewer builds the evidence for that pair
    Then that event's location text is not included in the evidence

  # ── the record is durable, re-openable, and an operator overrides it ───────

  Scenario: Every pair the reviewer looked at gets a review record
    Given flagged pairs that the reviewer confirms, contradicts, skips and fails to agree on
    When the reviewer runs over the audit's flagged pairs
    Then each of those pairs has exactly one review record
    And each record carries its verdict, its outcome and the audit counts as of the review

  Scenario: A confirmed pair re-opens when new non-corroborating evidence arrives
    Given a pair the reviewer already confirmed and suppressed
    When the pair's non-corroborating count grows beyond the count recorded at review
    Then the pair appears in the audit's flagged output again

  Scenario: An operator rejection is permanent and suppresses nothing
    Given a pair the reviewer confirmed and suppressed
    When an operator rejects that review
    Then the pair appears in the audit's flagged output again
    And a later reviewer run makes no model call for that pair

  Scenario: An operator can list every review the reviewer has recorded
    Given the reviewer has recorded verdicts of every kind
    When an operator requests the venue-link-audit review list
    Then every recorded review is returned with its verdict, evidence quote and reason
    And the list can be narrowed to a single verdict

  # ── off by default, and unchanged on a second run ──────────────────────────

  Scenario: The reviewer makes no model call and writes nothing while its flag is off
    Given the venue-link-audit reviewer flag is disabled
    When the reviewer runs over the audit's flagged pairs
    Then no model call is charged for any pair
    And no review record is written
    And the audit's flagged output is unchanged

  Scenario: A consensus verdict suppresses nothing while auto-apply is off
    Given the venue-link-audit reviewer auto-apply flag is disabled
    And a flagged pair every independent call confirms
    When the reviewer runs over the audit's flagged pairs
    Then the pair's verdict is recorded
    But the pair still appears in the audit's flagged output

  Scenario: Running the reviewer a second time over unchanged data changes nothing
    Given the reviewer has already run over the audit's flagged pairs
    When the reviewer runs again over the same unchanged data
    Then no review record's verdict changes
    And the audit's flagged output is the same as after the first run
