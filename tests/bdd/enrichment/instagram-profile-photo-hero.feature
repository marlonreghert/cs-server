Feature: Instagram profile photo as the venue list hero

  A venue's Instagram profile picture is archived to the media bucket and
  projected to Redis, so the venue list can show a thumbnail without any
  serve-time Google call. Spending is gated before every billed call, and an
  absent photo is a normal absence, never an error.

  Background:
    Given the Instagram profile photo job is enabled
    And the media bucket and CDN base URL are configured

  Scenario: A venue with a confirmed handle stores its profile photo and is projected
    Given a servable venue "v-alpha" with the confirmed Instagram handle "alphabar"
    And the profile scrape returns a profile picture
    When the profile photo job runs
    Then the image bytes are stored in the media bucket under a content-addressed key
    And the stored object carries an immutable one-year cache-control header
    And the venue's profile photo row records the CDN URL
    And the Redis key "venue_profile_photo_v1:v-alpha" holds that CDN URL
    And the run summary counts venue "v-alpha" as "stored"

  Scenario: A venue whose stored photo is still fresh is never billed
    Given a servable venue "v-bravo" with the confirmed Instagram handle "bravobar"
    And venue "v-bravo" already has a profile photo stored 3 days ago
    And the refresh window is 30 days
    When the profile photo job runs
    Then no profile scrape is requested for venue "v-bravo"
    And the run summary counts venue "v-bravo" as "skipped_fresh"

  Scenario: An unchanged photo re-uploads nothing and keeps the served URL identical
    Given a servable venue "v-charlie" with the confirmed Instagram handle "charliebar"
    And venue "v-charlie" already has a profile photo stored beyond the refresh window
    And the profile scrape returns a profile picture whose bytes are unchanged
    When the profile photo job runs
    Then no object is uploaded to the media bucket
    And the CDN URL projected for venue "v-charlie" is unchanged
    And the run summary counts venue "v-charlie" as "unchanged"

  Scenario: A venue without a confirmed handle is never fetched
    Given a servable venue "v-delta" with no confirmed Instagram handle
    When the profile photo job runs
    Then no profile scrape is requested for venue "v-delta"
    And no Redis profile photo key exists for venue "v-delta"
    And the run summary counts venue "v-delta" as "no_handle"

  Scenario: A profile carrying no picture writes nothing
    Given a servable venue "v-echo" with the confirmed Instagram handle "echobar"
    And the profile scrape returns a profile with no picture URL
    When the profile photo job runs
    Then no object is uploaded to the media bucket
    And no Redis profile photo key exists for venue "v-echo"
    And the run summary counts venue "v-echo" as "no_pic"

  Scenario: An oversized download is discarded before any storage write
    Given a servable venue "v-foxtrot" with the confirmed Instagram handle "foxtrotbar"
    And the profile scrape returns a profile picture
    And the profile picture download exceeds the maximum allowed size
    When the profile photo job runs
    Then no object is uploaded to the media bucket
    And no profile photo row is written for venue "v-foxtrot"
    And the run summary counts venue "v-foxtrot" as "download_failed"

  Scenario: A download that is not an allowed image type is discarded
    Given a servable venue "v-golf" with the confirmed Instagram handle "golfbar"
    And the profile scrape returns a profile picture
    And the profile picture download returns a disallowed content type
    When the profile photo job runs
    Then no object is uploaded to the media bucket
    And the run summary counts venue "v-golf" as "download_failed"

  Scenario: Exhausted scraping credit stops the run instead of spending further
    Given three servable venues with confirmed Instagram handles
    And the profile scrape reports exhausted credit on the first venue
    When the profile photo job runs
    Then no profile scrape is requested for the remaining venues
    And the run summary reports the run stopped for exhausted credit

  Scenario: One venue failing does not abort the run
    Given a servable venue "v-hotel" with the confirmed Instagram handle "hotelbar"
    And a servable venue "v-india" with the confirmed Instagram handle "indiabar"
    And the profile scrape fails for venue "v-hotel"
    And the profile scrape returns a profile picture for venue "v-india"
    When the profile photo job runs
    Then the run summary counts venue "v-hotel" as "fetch_failed"
    And the run summary counts venue "v-india" as "stored"
    And the Redis key "venue_profile_photo_v1:v-india" holds a CDN URL

  Scenario: The projector removes a Redis key whose row was soft-deleted
    Given a servable venue "v-juliet" with a projected profile photo
    And the profile photo row for venue "v-juliet" is soft-deleted
    When the Redis projection rebuild runs
    Then no Redis profile photo key exists for venue "v-juliet"

  Scenario: The job is inert while the feature is disabled
    Given the Instagram profile photo job is disabled
    And a servable venue "v-kilo" with the confirmed Instagram handle "kilobar"
    When the profile photo job runs
    Then no profile scrape is requested for venue "v-kilo"
    And no object is uploaded to the media bucket

  Scenario: The venue detail photo path is unaffected
    Given a servable venue "v-lima" with a projected profile photo
    And venue "v-lima" has stored Google photos
    When the Redis projection rebuild runs
    Then the venue's Google photo projection is unchanged
