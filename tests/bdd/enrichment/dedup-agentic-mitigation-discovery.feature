Feature: Measure before building — ground the agentic mitigation decision in real signal coverage

  As an operator scaling event crawling from a handful of venues toward
  roughly a thousand Instagram accounts across multiple cities
  I want every claim about how well the existing deterministic pipeline
  already covers cross-post fragmentation, multi-location misattribution,
  and multi-post duplication measured against real incident cases before any
  new agentic mechanism is built
  So that this plan ships only the infrastructure the numbers actually
  justify, never a guess dressed up as a design

  This feature pins the two pieces built unconditionally — a read-only
  measurement pass over existing signals, and a closed-candidate-set
  advisory validator — and the advisory hook itself, which only takes effect
  once its own kill switch is turned on.

  Background:
    Given the labeled discovery corpus of known incident cases
    And the venue catalog and Instagram handle mappings from that corpus

  # ── read-only baseline measurement ─────────────────────────────────────

  Scenario: Measure without writing anything
    When the baseline measurement script runs against the corpus
    Then no event, venue, or promoter-account row is written
    And a report names, for each corpus case, which existing signal already caught it, if any

  Scenario: Replay the promoter-discovery mention threshold against a known roundup handle
    Given a corpus handle whose captions mention another handle at least three times across distinct posts
    When the baseline measurement script replays promoter discovery
    Then that other handle is reported as a proposed candidate
    And the report records the mention count and the mention threshold used

  Scenario: Replay the promoter-discovery mention threshold against a handle that never clears it
    Given a corpus handle whose captions mention another handle only once
    When the baseline measurement script replays promoter discovery
    Then that other handle is not reported as a proposed candidate

  Scenario: Report the current ambiguous-band volume as a measured fraction, not an assumption
    When the baseline measurement script runs against production data
    Then the report states the fraction of extracted events currently landing in the suggest band, the queued resolution state, or an attribution dispute
    And the report does not state any fraction that was not computed from real data

  # ── one post naming several venues resolves each event independently ───
  # Already true today (see the plan's Evidence trace); pinned here as a
  # regression guard, not a new capability this plan adds.

  Scenario: Resolve two events from one post to two different venues independently
    Given a single post from a corpus handle announces two events whose location texts each name a different venue
    When that post is reconciled
    Then each event's venue attribution is evaluated against its own location text alone
    And the two events are not forced to share one venue attribution

  # ── the unrelated-venue gap: undetected today, by design of no existing signal ──
  # Documents a real, currently-open gap (the "barchef" / hidden-promoter
  # pattern): a handle mapped to one venue whose post names a different,
  # unrelated, catalogued venue in plain text, with no handle mention, no
  # location tag, and no shared brand-root token. Expected to stay
  # undetected until a future phase closes it; this scenario exists so the
  # gap is measured and visible, not silently assumed away.

  Scenario: Leave an unrelated-venue misattribution undisputed today
    Given a corpus handle mapped to one venue
    And a post from that handle names a different, unrelated, catalogued venue in its location text alone, with no handle mention, no location tag, and no shared brand-root token with the mapped venue
    When that post is reconciled
    Then the event stays attributed to the handle's mapped venue
    And the event carries no review reason
    And no attribution dispute is counted
    And the baseline measurement report records this corpus case as undetected by every existing signal

  # ── the closed-candidate-set advisory validator ────────────────────────

  Scenario: Accept a recommendation whose venue and evidence are both real
    Given a closed set of ranked venue candidates for an event
    And a model answer naming one of those candidates with a quoted evidence span
    When the evidence span is a verbatim substring of the event's own location text
    Then the recommendation is accepted

  Scenario: Reject a recommendation naming a venue outside the candidate set
    Given a closed set of ranked venue candidates for an event
    And a model answer naming a venue that is not among those candidates
    When the recommendation is validated
    Then the recommendation is rejected
    And the rejection reason records that the venue was outside the candidate set

  Scenario: Reject a recommendation whose evidence was never actually written
    Given a closed set of ranked venue candidates for an event
    And a model answer whose quoted evidence does not appear anywhere in the event's location text or caption
    When the recommendation is validated
    Then the recommendation is rejected
    And the rejection reason records that the evidence was not verbatim

  Scenario: Reject a recommendation whose evidence was borrowed from a different candidate
    Given a closed set of ranked venue candidates for an event
    And a model answer naming one candidate but quoting evidence that only appears in a note about a different candidate
    When the recommendation is validated
    Then the recommendation is rejected

  # ── the advisory hook, conditional on the measured gate ────────────────
  # Ships only if the baseline measurement shows the signals above leave
  # real residual volume uncovered. Every scenario below still governs its
  # behaviour if it ships — see the plan's Phase 4 and Acceptance Criteria.

  Scenario: Leave production untouched while the advisor is disabled
    Given the event venue advisor is disabled
    And an event resolves to the queued state with ranked venue candidates
    When the post is reconciled
    Then no advisor call is made
    And the event's venue and resolution state are exactly what the deterministic ladder produced

  Scenario: Attach a validated recommendation without changing the resolution
    Given the event venue advisor is enabled
    And an event resolves to the queued state with ranked venue candidates
    And the advisor's recommendation validates
    When the post is reconciled
    Then the event's venue candidate row carries the validated recommendation
    And the event's venue remains unset
    And the event's resolution state remains queued

  Scenario: Never call the advisor for an event the ladder already resolved automatically
    Given the event venue advisor is enabled
    And an event resolves automatically through the handle-mention method
    When the post is reconciled
    Then no advisor call is made

  Scenario: Surface the validated recommendation on the existing review queue
    Given the event venue advisor is enabled
    And an event resolves to the queued state with a validated recommendation attached
    When an operator requests the review queue
    Then that event's entry in the review queue carries the recommendation
    And no new endpoint was needed to see it
