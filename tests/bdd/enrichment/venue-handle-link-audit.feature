Feature: Venue-handle link audit — flag a kind='venue' crawl target whose own posts never corroborate its mapped venue

  As an operator relying on automatic venue attribution for ~80 crawled
  venue handles that never pass through the resolution ladder at all
  I want every kind='venue' crawl target whose own posts give no
  corroborating evidence for its currently-mapped venue, across at least
  two posts, surfaced for review
  So that a wrong catalog link (the editaisculturape shape: real, live,
  force-assigned, currently-served misattribution) is found by a
  repeatable sweep instead of by someone happening to hand-check that one
  handle

  This feature pins the read-only comparison and aggregation this plan
  ships, and the existing GET /admin/events/dedup-backlog report it
  extends — gated off by default, so enabling it is an explicit operator
  choice and disabling it costs nothing.

  Background:
    Given the venue-handle link audit fixture corpus of re-verified real cases

  # ── the core comparison ──────────────────────────────────────────────────

  Scenario: A post naming a real, different, unrelated place does not corroborate the mapped venue
    Given a kind='venue' crawl target mapped to one venue
    And one of its posts' own location text names a specific, different place by name
    When the venue-handle link audit comparison runs for that post against the mapped venue
    Then the post is reported as not corroborating the mapped venue

  Scenario: A post naming the mapped venue's own name corroborates it
    Given a kind='venue' crawl target mapped to one venue
    And one of its posts' own location text is a spelling variant of the mapped venue's own name
    When the venue-handle link audit comparison runs for that post against the mapped venue
    Then the post is reported as corroborating the mapped venue

  Scenario: A post naming the mapped venue's own name plus its own address still corroborates it
    Given a kind='venue' crawl target mapped to one venue
    And one of its posts' own location text combines the mapped venue's name with its own neighbourhood
    When the venue-handle link audit comparison runs for that post against the mapped venue
    Then the post is reported as corroborating the mapped venue, via the neighbourhood match

  Scenario: Location text too short to mean anything is not counted either way
    Given a kind='venue' crawl target mapped to one venue
    And one of its posts' own location text is empty
    When the venue-handle link audit comparison runs for that post against the mapped venue
    Then the post is excluded from both the corroborating and non-corroborating counts

  # ── aggregation and the false-positive guard ────────────────────────────

  Scenario: A handle with a real, repeated mismatch is flagged
    Given a kind='venue' crawl target whose posts give at least two non-corroborating, checkable pieces of location text for its mapped venue
    When the venue-handle link audit aggregation runs for that target
    Then the target's mapped venue is reported as a venue-link-audit candidate

  Scenario: A single incidental mention does not flag a handle
    Given a kind='venue' crawl target whose posts consistently corroborate its mapped venue except for exactly one post naming an unrelated place in passing
    When the venue-handle link audit aggregation runs for that target
    Then the target's mapped venue is not reported as a venue-link-audit candidate

  Scenario: A handle mapped to two venues flags only the one its posts never corroborate
    Given a kind='venue' crawl target currently mapped to two venues, where every post's location text corroborates only one of them
    When the venue-handle link audit aggregation runs for that target
    Then only the uncorroborated venue is reported as a venue-link-audit candidate
    And the corroborated venue is not reported

  Scenario: A crawl target with no venue currently mapped to it is skipped, not flagged
    Given a kind='venue' crawl target with no venue currently mapped to its handle
    When the venue-handle link audit aggregation runs for that target
    Then the target produces no venue-link-audit candidate and no error

  # ── an event's current attribution, not just its handle's history ──────
  # 2026-09-13/14: running the shipped audit against production for the
  # first time found it re-flagging beerdock_recife for events an unrelated
  # mechanism had already, correctly, reattributed to a sibling venue hours
  # earlier. Fixed by checking each event's own current venue_id.

  Scenario: An event already reattributed to a different venue is never held against its original mapping
    Given a kind='venue' crawl target mapped to one venue
    And most of its posts' location text has already been reattributed to a different, specific venue by an unrelated mechanism
    And fewer than two of its remaining posts fail to corroborate the mapped venue
    When the venue-handle link audit aggregation runs for that target
    Then the target's mapped venue is not reported as a venue-link-audit candidate

  Scenario: An event that was never resolved to any venue still counts toward the mapped venue's totals
    Given a kind='venue' crawl target mapped to one venue
    And at least two of its posts have never been resolved to any venue and do not corroborate the mapped venue
    When the venue-handle link audit aggregation runs for that target
    Then the target's mapped venue is reported as a venue-link-audit candidate

  # ── the existing admin surface, gated off by default ────────────────────

  Scenario: The audit is invisible in the dedup-backlog report by default
    Given venue link auditing is disabled in admin config
    And a kind='venue' crawl target that would otherwise be flagged
    When an operator requests the dedup-backlog report
    Then the report's venue-link-audit fields are empty
    And the venue-handle link audit aggregation is never invoked

  Scenario: Enabling the audit surfaces flagged candidates in the existing dedup-backlog report
    Given venue link auditing is enabled in admin config
    And a kind='venue' crawl target that would be flagged
    When an operator requests the dedup-backlog report
    Then the report includes that target as a venue-link-audit candidate
    And every other field of the report is unchanged from today's shape

  # ── read-only measurement ────────────────────────────────────────────────

  Scenario: The measurement script reproduces the three re-verified cases without writing anything
    When the venue-handle link audit measurement script runs against the fixture corpus
    Then it flags the re-verified wrong-venue case
    And it flags the re-verified force-assigned-misattribution case
    And it does not flag the re-verified benign case
    And no event, venue, or crawl-target row is written
