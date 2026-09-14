@wip
Feature: Promoter roundup caption mention
  A promoter post's caption is only trustworthy evidence for one of its own
  events when the post is genuinely about one place. A roundup post whose own
  extracted events name several different places must never have any of them
  pinned to whichever one of those places happens to already be in our
  catalog, just because the others are not.

  Background:
    Given the venue "Seu Chico Botequim" at handle "seuchicobotequim"
    And the venue "Sempre Rock Bar" at handle "semprerockbar"

  # ── the roundup shape must be refused ───────────────────────────────────────

  Scenario: Refuse the caption fallback when sibling events name different places
    Given a promoter post whose caption mentions "@seuchicobotequim"
    And the post's own extracted events have location texts:
      | @seuchicobotequim         |
      | @teatroalbertomaranhao    |
      | @eskinaprime              |
    When the events are attributed
    Then the event whose location text is "@teatroalbertomaranhao" links to no venue
    And the event whose location text is "@eskinaprime" links to no venue

  Scenario: Record venue-not-in-catalog on a refused sibling, not a generic unresolved
    Given a promoter post whose caption mentions "@seuchicobotequim"
    And the post's own extracted events have location texts:
      | @seuchicobotequim |
      | @eskinaprime      |
    When the events are attributed
    Then the event whose location text is "@eskinaprime" is queued for review with reason "venue_not_in_catalog"

  Scenario: Still link the sibling whose own text names the catalogued venue
    Given a promoter post whose caption mentions "@seuchicobotequim"
    And the post's own extracted events have location texts:
      | @seuchicobotequim |
      | @eskinaprime      |
    When the events are attributed
    Then the event whose location text is "@seuchicobotequim" links to "Seu Chico Botequim"

  # ── nothing already working may regress ─────────────────────────────────────

  Scenario: Keep the caption fallback for a single-event post
    Given a promoter post whose caption mentions "@seuchicobotequim"
    And the post's own extracted events have location texts:
      | (none) |
    When the events are attributed
    Then the event links to "Seu Chico Botequim"

  Scenario: Keep the caption fallback when every sibling shares one place
    Given a promoter post whose caption mentions "@seuchicobotequim"
    And the post's own extracted events have location texts:
      | (none) |
      | (none) |
    When the events are attributed
    Then every event links to "Seu Chico Botequim"

  Scenario: Keep refusing a caption naming several catalogued venues
    Given a promoter post whose caption mentions "@seuchicobotequim" and "@semprerockbar"
    And the post's own extracted events have location texts:
      | (none) |
    When the events are attributed
    Then the event links to no venue

  # ── the backfill repairs the already-stored rows ────────────────────────────

  Scenario: The caption-handle-mention backfill mode detaches a mis-pinned row
    Given a stored event linked by "caption_handle_mention" to "Seu Chico Botequim"
    When the venue-link backfill runs in mode "caption-handle-mention" with apply
    Then the stored event links to no venue
    And the stored event is queued for review with reason "venue_not_in_catalog"

  Scenario: The caption-handle-mention backfill mode is idempotent
    Given a stored event linked by "caption_handle_mention" to "Seu Chico Botequim"
    When the venue-link backfill runs in mode "caption-handle-mention" with apply
    And the venue-link backfill runs again in mode "caption-handle-mention" with apply
    Then the stored event links to no venue

  Scenario: The caption-handle-mention backfill mode never selects a handle-mention row
    Given a stored event linked by "handle_mention" to "Seu Chico Botequim"
    When the venue-link backfill runs in mode "caption-handle-mention" with apply
    Then the stored event still links to "Seu Chico Botequim"
