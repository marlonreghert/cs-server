@wip
Feature: History repair — re-extraction
  Recover the two facts no deterministic pass can reach: posts mis-typed as
  events that are really greetings or recaps, and the roundup events discarded
  past the per-post cap. From archived images only, with the cost known before
  it is spent, and without ever moving the identity of an event that already
  exists.

  Background:
    Given the re-extraction script is available
    And every targeted post's images are archived

  # ── prerequisites are enforced, not assumed ────────────────────────────────

  Scenario: Refuse to run when the prompt change is not deployed
    Given the improved post-type prompt is not deployed
    When the re-extraction runs in post-type mode
    Then the run aborts before any model call
    And no cost is incurred

  Scenario: Refuse to run truncated mode when the cap has not been raised
    Given the per-post event cap is still at its old value
    When the re-extraction runs in truncated mode
    Then the run aborts before any model call

  # ── post-type mode ─────────────────────────────────────────────────────────

  Scenario: Re-type a greeting as other
    Given a stored item "31 Anos" typed as "event" with no lineup and no known time
    And the post reads "Parabéns pelos seus 31 anos! Feliz aniversário!"
    When the re-extraction runs in post-type mode with apply
    Then the item's post type is "other"

  Scenario: Keep a genuine announcement typed as an event
    Given a stored item announcing a party with its lineup and door price
    When the re-extraction runs in post-type mode with apply
    Then the item's post type is "event"

  Scenario: Leave an announcement with an empty lineup alone
    Given a stored item announcing a party on Friday with a door price and no lineup
    When the re-extraction runs in post-type mode with apply
    Then the item's post type is "event"

  Scenario: Re-type one item without disturbing its siblings
    Given a post from which three items were extracted
    And only one of them is a greeting
    When the re-extraction runs in post-type mode with apply
    Then only that item's post type changes
    And the other two items are unchanged

  # ── truncated mode ─────────────────────────────────────────────────────────

  Scenario: Recover the events a post lost past the old cap
    Given a post flagged as truncated at 20 events
    And re-extracting it returns 26 events
    When the re-extraction runs in truncated mode with apply
    Then 6 new items are stored for that post
    And the 20 existing items are still stored

  Scenario: Give a recovered event a fresh content identity
    Given a post flagged as truncated at 20 events
    When the re-extraction runs in truncated mode with apply
    Then each new item's source event key equals what a clean extraction would compute

  Scenario: Stop reporting a post as truncated once it fits
    Given a post flagged as truncated at 20 events
    And re-extracting it under the raised cap returns 26 events
    When the re-extraction runs in truncated mode with apply
    Then the post is no longer recorded as truncated

  # ── identity is pinned across re-extraction ────────────────────────────────

  Scenario: Keep the stored identity of an event that already existed
    Given a stored item whose post is re-extracted
    And the model returns a re-phrased title and a different date text for it
    When the re-extraction runs with apply
    Then the item's source event key is unchanged

  Scenario: Leave an event alone when re-extraction no longer returns it
    Given a post from which 20 items were extracted
    And re-extracting it returns only 19 of them
    When the re-extraction runs with apply
    Then all 20 items are still stored
    And no item is deleted

  # ── the operator always wins ───────────────────────────────────────────────

  Scenario: Never re-type a confirmed row and report the disagreement
    Given a confirmed item that the improved prompt would type as "other"
    When the re-extraction runs in post-type mode with apply
    Then the item's post type is unchanged
    And the report lists it as a confirmed conflict

  Scenario: Never overwrite a field an operator edited
    Given a stored item whose category was edited by an operator
    When the re-extraction runs with apply
    Then the item's category is unchanged

  # ── cost is known before it is spent ───────────────────────────────────────

  Scenario: Estimate cost and write nothing without apply
    Given 12 posts match the target set
    When the re-extraction runs without apply
    Then the report states how many posts would be re-extracted
    And the report states the estimated cost
    And no model call is made

  Scenario: Stop at the configured maximum number of posts
    Given 40 posts match the target set
    And the maximum is set to 5 posts
    When the re-extraction runs with apply
    Then exactly 5 posts are re-extracted

  Scenario: Resume after an interrupted run
    Given a run that completed 3 of 8 posts before being interrupted
    When the re-extraction runs again with apply
    Then only the remaining 5 posts are re-extracted

  Scenario: Never call Apify in either mode
    Given a post targeted for re-extraction
    When the re-extraction runs with apply
    Then no Apify call is made
    And the post's images are read from the archive
