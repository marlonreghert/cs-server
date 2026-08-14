@wip
Feature: Stop reading absence as an answer
  As the operator of the event pipeline
  I must have a reels stream that ran and found nothing recorded as seeded, and
  an admin config value validated before it is stored, so that an empty account
  does not re-buy its one-time seed three times a week forever and turning a
  switch off can never turn it on.

  # ── A. A stream that completed is seeded, even when it found nothing ──────

  Scenario: Record a reels stream that returned nothing as seeded
    Given a crawl target whose reels have never been seeded
    When its reels stream completes and returns no items
    Then the target is recorded as having seeded its reels

  Scenario: Never run the reels seed twice for a target whose stream completed empty
    Given a crawl target whose reels stream already completed and returned no items
    When the target is crawled again
    Then no reels stream runs
    And the run reports that reels were skipped because they are already seeded

  Scenario: Leave a blocked reels stream unseeded so its seed is retried
    Given a crawl target whose reels have never been seeded
    When its reels stream is blocked
    Then the target is not recorded as having seeded its reels
    And the next crawl runs the reels stream again

  Scenario: Leave a timed-out reels stream unseeded
    Given a crawl target whose reels have never been seeded
    When its reels stream times out
    Then the target is not recorded as having seeded its reels

  Scenario: Leave a handle-not-found reels stream unseeded
    Given a crawl target whose reels have never been seeded
    When its reels stream reports the handle does not exist
    Then the target is not recorded as having seeded its reels

  Scenario: Keep seeding a reels stream that returned items
    Given a crawl target whose reels have never been seeded
    When its reels stream returns items
    Then the target is recorded as having seeded its reels
    And the reels cursor advances to the newest reel

  Scenario: Leave the reels cursor empty when nothing was reached
    Given a crawl target whose reels have never been seeded
    When its reels stream completes and returns no items
    Then the reels cursor is still empty

  Scenario: Do not mark a target seeded when its bookkeeping write failed
    Given a crawl target whose reels have never been seeded
    And the post-run bookkeeping write fails
    When its reels stream completes and returns no items
    Then the target is not recorded as having seeded its reels

  Scenario: Report whether reels are seeded on the admin read
    Given a crawl target whose reels stream already completed and returned no items
    When the crawl targets are read from the admin API
    Then the target reports that its reels are seeded

  # ── B. Config is validated on write, not coerced on read ─────────────────

  Scenario: Reject a non-boolean auto-merge flag before it is stored
    When an operator sets the auto-merge flag to a value that is not a boolean
    Then the write is refused
    And the stored auto-merge flag is unchanged

  Scenario: Never let the string "false" enable auto-merge
    When an operator sets the auto-merge flag to the text "false"
    Then the write is refused
    And auto-merge is not enabled

  Scenario: Reject a malformed value for every dedup config key
    When an operator sets each event dedup config key to a malformed value
    Then every one of those writes is refused

  Scenario: Keep a valid write working for every dedup config key
    When an operator sets each event dedup config key to a valid value
    Then every one of those writes is stored

  Scenario: Fall back to the shipped default when a stored value has the wrong type
    Given a stored dedup config value whose type is wrong
    When the dedup configuration is read
    Then the shipped default is used for that key
    And the type fallback is counted
    And every other dedup key keeps its own stored value
