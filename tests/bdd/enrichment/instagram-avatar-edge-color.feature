Feature: Instagram avatar edge colour

  Every archived Instagram profile photo must record the dominant colour of the
  strips a "contain" fit would letterbox, so the app can paint that strip in the
  avatar's own colour instead of a grey bar. The colour is additive: an absent
  one is normal and must never break storing, projecting or serving a photo.

  The rows that already exist are backfilled by re-reading each object over its
  own public CDN URL — no Apify unit, no S3 read grant, no new upload.

  Background:
    Given the Instagram profile photo job is enabled
    And the media bucket and CDN base URL are configured

  # ── sampling on the store path ────────────────────────────────────────────

  Scenario: A newly stored profile photo records its edge colour
    Given a servable venue "v-edge" with the confirmed Instagram handle "edgebar"
    And the profile scrape returns an avatar whose border is "#004A9D"
    When the profile photo job runs
    Then the profile photo row for venue "v-edge" records the edge colour "#004A9D"
    And the run summary counts venue "v-edge" as "stored"

  Scenario: The projected key carries the edge colour
    Given a servable venue "v-edge" with the confirmed Instagram handle "edgebar"
    And the profile scrape returns an avatar whose border is "#FFFFFF"
    When the profile photo job runs
    And the Redis projection rebuild runs
    Then the Redis key "venue_profile_photo_v1:v-edge" carries the edge colour "#FFFFFF"

  Scenario: An undecodable image is still stored, with no edge colour
    Given a servable venue "v-bad" with the confirmed Instagram handle "badbar"
    And the profile scrape returns a picture whose bytes are not a decodable image
    When the profile photo job runs
    Then the run summary counts venue "v-bad" as "stored"
    And the profile photo row for venue "v-bad" records no edge colour

  Scenario: A row with no edge colour still projects its key
    Given a servable venue "v-none" with the confirmed Instagram handle "nonebar"
    And the profile scrape returns a picture whose bytes are not a decodable image
    When the profile photo job runs
    And the Redis projection rebuild runs
    Then the Redis key "venue_profile_photo_v1:v-none" holds a CDN URL
    And the Redis key "venue_profile_photo_v1:v-none" carries no edge colour

  Scenario: The unchanged-photo short-circuit fills a missing edge colour
    Given a servable venue "v-same" with the confirmed Instagram handle "samebar"
    And venue "v-same" has a stored profile photo with no edge colour and a border of "#171717"
    And the profile scrape returns the same avatar again
    When the profile photo job runs in "refresh_all" mode
    Then the run summary counts venue "v-same" as "unchanged"
    And no object is uploaded to the media bucket
    And the profile photo row for venue "v-same" records the edge colour "#171717"

  # ── the free backfill of rows that already exist ──────────────────────────

  Scenario: The edge-colour backfill fills rows that lack a colour
    Given venue "v-b1" has a stored profile photo with no edge colour and a border of "#3154A5"
    And venue "v-b2" has a stored profile photo with no edge colour and a border of "#FFFFFF"
    When the profile photo job runs in "edge_color" mode
    Then the profile photo row for venue "v-b1" records the edge colour "#3154A5"
    And the profile photo row for venue "v-b2" records the edge colour "#FFFFFF"
    And the run summary counts venue "v-b1" as "edge_color_sampled"

  Scenario: The edge-colour backfill spends nothing
    Given venue "v-b1" has a stored profile photo with no edge colour and a border of "#004A9D"
    When the profile photo job runs in "edge_color" mode
    Then no profile scrape is requested at all
    And no object is uploaded to the media bucket

  Scenario: The edge-colour backfill reads each photo from its stored CDN URL
    Given venue "v-b1" has a stored profile photo with no edge colour and a border of "#004A9D"
    When the profile photo job runs in "edge_color" mode
    Then the image for venue "v-b1" was fetched from its stored CDN URL

  Scenario: The edge-colour backfill preserves every other field of the row
    Given a servable venue "v-keep" with the confirmed Instagram handle "keepbar"
    And venue "v-keep" has a stored profile photo with no edge colour and a border of "#171717"
    When the profile photo job runs in "edge_color" mode
    Then the profile photo row for venue "v-keep" is otherwise unchanged
    And the run summary counts venue "v-keep" as "edge_color_sampled"

  Scenario: A backfilled venue is still skipped by the next scheduled backfill
    Given a servable venue "v-keep" with the confirmed Instagram handle "keepbar"
    And venue "v-keep" has a stored profile photo with no edge colour and a border of "#171717"
    When the profile photo job runs in "edge_color" mode
    And the profile photo job runs
    Then the run summary counts venue "v-keep" as "skipped_has_photo"
    And no profile scrape is requested for venue "v-keep"

  Scenario: A row that already carries an edge colour is skipped
    Given venue "v-done" has a stored profile photo whose edge colour is "#ABCDEF"
    When the profile photo job runs in "edge_color" mode
    Then the profile photo row for venue "v-done" records the edge colour "#ABCDEF"
    And no image is fetched for venue "v-done"

  Scenario: A failed fetch leaves the row untouched and is retried next run
    Given venue "v-fail" has a stored profile photo with no edge colour and a border of "#004A9D"
    And fetching the stored photo for venue "v-fail" fails
    When the profile photo job runs in "edge_color" mode
    Then the profile photo row for venue "v-fail" records no edge colour
    And the run summary counts venue "v-fail" as "edge_color_fetch_failed"
    And the edge-colour estimate reports 1 venue to process

  Scenario: One unreachable photo does not abort the rest of the backfill
    Given venue "v-fail" has a stored profile photo with no edge colour and a border of "#004A9D"
    And venue "v-b2" has a stored profile photo with no edge colour and a border of "#FFFFFF"
    And fetching the stored photo for venue "v-fail" fails
    When the profile photo job runs in "edge_color" mode
    Then the profile photo row for venue "v-b2" records the edge colour "#FFFFFF"

  Scenario: A venue with no stored photo is not selected by the backfill
    Given a servable venue "v-nophoto" with the confirmed Instagram handle "nophotobar"
    When the profile photo job runs in "edge_color" mode
    Then no image is fetched for venue "v-nophoto"
    And no profile scrape is requested at all

  # ── estimate, cap and mode safety ─────────────────────────────────────────

  Scenario: The edge-colour estimate matches the run and fetches nothing
    Given venue "v-b1" has a stored profile photo with no edge colour and a border of "#004A9D"
    And venue "v-b2" has a stored profile photo with no edge colour and a border of "#FFFFFF"
    When the edge-colour cost estimate is requested
    Then the edge-colour estimate reports 2 venues to process
    And no image is fetched for venue "v-b1"
    And no profile scrape is requested at all

  Scenario: The per-run cap defers the remaining venues
    Given venue "v-b1" has a stored profile photo with no edge colour and a border of "#004A9D"
    And venue "v-b2" has a stored profile photo with no edge colour and a border of "#FFFFFF"
    And the per-run profile photo cap is 1
    When the profile photo job runs in "edge_color" mode
    Then exactly 1 profile photo row carries an edge colour
    And the run summary reports 1 deferred venue

  Scenario: An unknown mode is rejected rather than defaulted
    Given venue "v-b1" has a stored profile photo with no edge colour and a border of "#004A9D"
    When the profile photo job is triggered with the mode "edge_colour"
    Then the trigger is rejected as an invalid mode
    And the profile photo row for venue "v-b1" records no edge colour
