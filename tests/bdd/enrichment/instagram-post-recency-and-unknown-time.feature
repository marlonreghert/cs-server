Feature: Instagram post recency and unknown event time
  As the operator of the event pipeline
  I want the image cap to keep the newest posts, and an unread event time to be
  visible rather than silently midnight
  So that a bounded run archives what just happened, and an operator can tell a
  time we could not read from a time that was never stated

  Background:
    Given a venue with a confirmed Instagram handle

  Scenario: Archive the newest posts when the scraper returns them out of order
    Given the scraper returns posts dated "2026-08-02, 2026-08-05, 2026-08-04, 2026-08-06 and 2026-08-01" in that order
    And each post carries one image
    And the run caps images per venue at 2
    When the archive runs for the source "instagram_posts"
    Then the images archived come from the 2026-08-06 and 2026-08-05 posts
    And no image from the 2026-08-01 post is archived

  Scenario: Keep the cap bounding images rather than posts after sorting
    Given the scraper returns 3 carousel posts of 5 images each, out of date order
    And the run caps images per venue at 6
    When the archive runs for the source "instagram_posts"
    Then 6 images are archived
    And exactly 6 images are downloaded
    And every archived image comes from the two newest posts

  Scenario: Sort a post with no timestamp last
    Given the scraper returns a post with no timestamp and a post dated 2026-08-06
    And each post carries one image
    And the run caps images per venue at 1
    When the archive runs for the source "instagram_posts"
    Then the image archived comes from the 2026-08-06 post

  Scenario: Archive an undated post when capacity remains
    Given the scraper returns a post with no timestamp and a post dated 2026-08-06
    And each post carries one image
    And the run caps images per venue at 2
    When the archive runs for the source "instagram_posts"
    Then both images are archived

  # plans/260813_review-gate-and-date-vocabulary.md §D superseded queueing
  # here: an event whose DATE resolved is no longer held up by a clock time
  # alone. The outcome/counter this scenario also pins is UNCHANGED — see
  # that plan's own Error Handling section ("keep an unread_time counter").
  Scenario: Count an event whose stated time could not be read without queueing it
    Given a qualifying post whose flyer reports that it names a time
    And the extractor returns no time
    When event extraction runs
    Then the event is not queued for an unread time
    And the outcome "unread_time" is counted

  Scenario: Store a date-only event at midnight without queueing it
    Given a qualifying post whose flyer reports that it names no time
    And the extractor returns no time
    When event extraction runs
    Then the event starts at midnight on its resolved date
    And the event time is marked unknown
    And the event is not queued for an unread time

  Scenario: Do not queue when no flyer attribute contradicts the missing time
    Given a qualifying post that has no flyer attributes
    And the extractor returns no time
    When event extraction runs
    Then the event is not queued for an unread time

  Scenario: Report a stated midnight as a known time
    Given a qualifying post whose flyer states the event begins at "00h"
    When event extraction runs
    Then the event starts at midnight on its resolved date
    And the event time is marked known
    And the event is not queued for an unread time

  Scenario: Leave a fully dated and timed event unqueued
    Given a qualifying post whose flyer states a date and a time
    When event extraction runs
    Then the event carries that date and time
    And the event is not queued for review
