@wip
Feature: Instagram avatar edge colour

  Every archived Instagram profile photo must record the dominant colour of the
  strips a "contain" fit would letterbox, so the app can paint that leftover
  strip in the avatar's own colour instead of a grey bar. The colour is
  additive: an absent one is normal and must never break storing, projecting or
  serving the photo.

  Background:
    Given the Instagram profile photo feature is enabled
    And the media bucket and the CDN base URL are configured

  # ── sampling on the store path ────────────────────────────────────────────

  Scenario: A newly stored profile photo records its edge colour
    Given a servable venue with a confirmed Instagram handle and no stored photo
    And its profile picture has a flat white border
    When the profile photo job runs in backfill mode
    Then the venue's stored profile photo row should carry the edge colour "#FFFFFF"
    And the projected profile photo key for that venue should carry the same edge colour

  Scenario: An undecodable image is still stored, with no edge colour
    Given a servable venue with a confirmed Instagram handle and no stored photo
    And its profile picture downloads as bytes that cannot be decoded as an image
    When the profile photo job runs in backfill mode
    Then the venue's profile photo should still be stored
    And the venue's stored profile photo row should carry no edge colour
    And the run should report the outcome "stored"

  Scenario: The unchanged-photo short-circuit fills a missing edge colour
    Given a venue whose stored profile photo has the same content hash as the scraped picture
    And that stored row carries no edge colour
    When the profile photo job runs in refresh_all mode
    Then the run should report the outcome "unchanged"
    And no object should be uploaded to the media bucket
    And the venue's stored profile photo row should carry an edge colour

  # ── the backfill of rows that already exist ───────────────────────────────

  Scenario: The edge-colour backfill fills rows that lack a colour
    Given 3 venues with stored profile photos and no edge colour
    When the profile photo job runs in edge_color mode
    Then all 3 venues' stored profile photo rows should carry an edge colour
    And the run should report 3 venues with the outcome "edge_color_sampled"

  Scenario: The edge-colour backfill never spends an Apify unit
    Given 3 venues with stored profile photos and no edge colour
    When the profile photo job runs in edge_color mode
    Then the Apify profile-photo call counter should be unchanged
    And the Apify client should not have been called

  Scenario: The edge-colour backfill uploads nothing
    Given 3 venues with stored profile photos and no edge colour
    When the profile photo job runs in edge_color mode
    Then no object should be uploaded to the media bucket
    And the bytes-stored counter should be unchanged

  Scenario: The edge-colour backfill reads each photo from its stored public URL
    Given a venue with a stored profile photo and no edge colour
    When the profile photo job runs in edge_color mode
    Then the image should have been fetched from that row's stored photo URL
    And no object should have been read from the media bucket

  Scenario: The edge-colour backfill preserves every other field of the row
    Given a venue with a stored profile photo and no edge colour
    When the profile photo job runs in edge_color mode
    Then the row's Instagram handle, photo URL, S3 key, content hash, content type, byte size and fetched-at should be unchanged
    And a following run in backfill mode should report that venue as "skipped_has_photo"

  Scenario: A row that already carries an edge colour is skipped
    Given a venue with a stored profile photo that already carries an edge colour
    When the profile photo job runs in edge_color mode
    Then that venue's stored edge colour should be unchanged
    And no image should have been fetched for that venue

  Scenario: A failed fetch leaves the row untouched and is retried next run
    Given a venue with a stored profile photo and no edge colour
    And fetching that row's photo URL fails
    When the profile photo job runs in edge_color mode
    Then the venue's stored profile photo row should be unchanged
    And the run should report the outcome "edge_color_fetch_failed"
    And a following run in edge_color mode should select that venue again

  Scenario: One unreachable photo does not abort the rest of the backfill
    Given 3 venues with stored profile photos and no edge colour
    And fetching the first venue's photo URL fails
    When the profile photo job runs in edge_color mode
    Then the other 2 venues' rows should carry an edge colour

  Scenario: A venue with no stored photo is not selected by the backfill
    Given a servable venue with a confirmed Instagram handle and no stored photo
    When the profile photo job runs in edge_color mode
    Then that venue should not be selected
    And the Apify profile-photo call counter should be unchanged

  # ── estimate, cap and mode safety ─────────────────────────────────────────

  Scenario: The edge-colour estimate matches what the run processes and fetches nothing
    Given 3 venues with stored profile photos and no edge colour
    When the edge-colour estimate is requested
    Then the estimate should report 3 venues to process
    And no image should have been fetched
    And the Apify profile-photo call counter should be unchanged

  Scenario: The per-run cap defers the remaining venues
    Given 3 venues with stored profile photos and no edge colour
    And the per-run cap is 2
    When the profile photo job runs in edge_color mode
    Then 2 venues' rows should carry an edge colour
    And the run should report 1 deferred venue

  Scenario: The scheduled job can never enter the edge-colour mode
    When the scheduled profile photo job runs
    Then it should run in backfill mode

  Scenario: An unknown mode is rejected rather than defaulted
    When the profile photo job is triggered with the mode "edge_colour"
    Then the trigger should be rejected as an invalid mode
    And no venue should have been processed

  # ── projection ────────────────────────────────────────────────────────────

  Scenario: A row with no edge colour still projects its key
    Given a venue with a stored profile photo and no edge colour
    When the Redis projection runs
    Then the profile photo key for that venue should exist
    And that projected payload should carry no edge colour
    And it should still carry the photo URL
