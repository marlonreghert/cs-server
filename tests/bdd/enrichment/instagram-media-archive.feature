@wip
Feature: Instagram media archive
  As the operator of the photo archive pipeline
  I want Instagram post images and captions stored in their own source folder
  So that the lake holds Instagram media beside the Google media it already
  holds, and the flyers that announce events survive the URLs that expire

  Background:
    Given the Apify Instagram client is configured
    And the archive source "instagram_posts" is offered in the source catalog

  Scenario: Archive a venue's Instagram posts into their own source folder
    Given a venue with a confirmed Instagram handle
    And that handle has 3 recent single-image posts
    When the archive runs for the source "instagram_posts"
    Then 3 images are stored under the source folder "instagram_posts"
    And no image is stored under any Google source folder

  Scenario: Expand a carousel post into one object per child image
    Given a venue with a confirmed Instagram handle
    And that handle has one carousel post with 4 child images
    When the archive runs for the source "instagram_posts"
    Then 4 images are stored for that venue
    And each stored image carries its carousel index in the manifest

  Scenario: Stop expanding a carousel once the per-venue image cap is reached
    Given a venue with a confirmed Instagram handle
    And that handle has 3 carousel posts of 5 child images each
    And the run caps images per venue at 6
    When the archive runs for the source "instagram_posts"
    Then 6 images are stored for that venue
    And exactly 6 images are downloaded

  Scenario: Skip a venue with no Instagram handle without spending
    Given a venue with no Instagram handle
    When the archive runs for the source "instagram_posts"
    Then that venue is reported with the outcome "no_handle"
    And no Apify actor run is started
    And that outcome is counted separately from "no_match"

  Scenario: Record the post behind every archived image
    Given a venue with a confirmed Instagram handle
    And that handle has a post with a caption and a timestamp
    When the archive runs for the source "instagram_posts"
    Then the manifest entry for that image carries the caption
    And the manifest entry carries the post permalink
    And the manifest entry carries the post shortcode
    And the manifest entry carries the post timestamp

  Scenario: Produce the same photo id across runs when the signed url changes
    Given a venue with a confirmed Instagram handle
    And that handle has a post whose image url signature differs between two
      observations
    When the archive runs twice for the source "instagram_posts"
    Then both runs derive the same photo id for that image

  Scenario: File a poster under the flyer category
    Given a venue with a confirmed Instagram handle
    And that handle has a post whose image is a promotional poster
    When the archive runs for the source "instagram_posts"
    Then that image is classified as "flyer"
    And that image is stored under the category folder "flyer"

  Scenario: One failed image download does not lose the others
    Given a venue with a confirmed Instagram handle
    And that handle has 4 posts and one of their images fails to download
    When the archive runs for the source "instagram_posts"
    Then 3 images are stored for that venue
    And the failed image is counted with the result "failed"

  Scenario: Stop the run when Apify reports credit exhaustion
    Given the Apify Instagram client reports credit exhaustion on the second
      venue
    When the archive runs for the source "instagram_posts" over 5 venues
    Then the run stops after the second venue
    And no further Apify actor run is started
    And the run record is saved

  Scenario: Estimate a run without spending
    Given 10 venues with confirmed Instagram handles
    When a run is estimated for the source "instagram_posts"
    Then an estimated cost is returned in billable units of "posts scraped"
    And no Apify actor run is started
    And no object is written

  Scenario: Skip venues already archived by the previous run
    Given a venue archived by the previous run of the source "instagram_posts"
    And the run skips against the latest run
    When the archive runs for the source "instagram_posts"
    Then that venue is reported with the outcome "skipped_existing"
    And no Apify actor run is started for that venue
