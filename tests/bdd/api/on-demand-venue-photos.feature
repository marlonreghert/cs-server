Feature: On-demand venue photo resolution with fresh keyless CDN URLs
  As the venue photo pipeline
  I must resolve a single venue's Google Places photos on demand, cache fresh
  keyless CDN URLs under a short-TTL Redis key that cs-server alone writes, serve
  them through an internal resolve endpoint, and degrade to an empty list without
  ever caching or serving a stale, dead, or key-bearing URL.

  Background:
    Given the internal photo resolve endpoint is available
    And Google photo resolution returns keyless googleusercontent.com URLs via the media endpoint with skipHttpRedirect
    And the fresh photo cache key for a venue is "venue_photos_fresh_v1:{venue_id}"
    And the legacy photo cache key for a venue is "venue_photos_v1:{venue_id}"
    And the fresh photo cache time-to-live is driven by "photo_fresh_cache_ttl_hours" with a default of 6 hours
    And at most "photos_per_venue" photos are resolved per venue

  Scenario: Resolve returns fresh keyless photo URLs and caches them
    Given a venue with a stored google_place_id
    And Google returns 3 photos for that place
    When the internal resolve endpoint is called for the venue
    Then the response status is 200
    And the response body contains a "venue_photos" list of 3 items
    And each item has a "url" and an "author_name"
    And every "url" is a keyless googleusercontent.com URL with no "key" query parameter
    And no "url" is a places.googleapis.com media URL
    And the fresh photo cache for the venue holds the same 3 items

  Scenario: The first author attribution is preserved and missing attribution is null
    Given a venue with a stored google_place_id
    And Google returns a photo with an author attribution "Ana" and a photo with no attribution
    When the internal resolve endpoint is called for the venue
    Then the response status is 200
    And one returned item has "author_name" equal to "Ana"
    And the item without attribution has a null "author_name"

  Scenario: Resolution caps the number of photos at photos_per_venue
    Given "photos_per_venue" is 5
    And a venue with a stored google_place_id
    And Google returns 8 photos for that place
    When the internal resolve endpoint is called for the venue
    Then the response status is 200
    And the response body contains a "venue_photos" list of exactly 5 items
    And the fresh photo cache for the venue holds exactly 5 items

  Scenario: The fresh cache time-to-live follows photo_fresh_cache_ttl_hours
    Given "photo_fresh_cache_ttl_hours" is 6
    And a venue with a stored google_place_id
    And Google returns 2 photos for that place
    When the internal resolve endpoint is called for the venue
    Then the fresh photo cache for the venue has a positive time-to-live
    And the fresh photo cache time-to-live is at most 6 hours

  Scenario: Resolution writes only the fresh key and never the legacy key
    Given a venue with a stored google_place_id
    And Google returns 2 photos for that place
    When the internal resolve endpoint is called for the venue
    Then the fresh photo cache for the venue is written
    And the legacy photo cache for the venue is not written by the resolve path

  Scenario: A venue with no google_place_id resolves to an empty list
    Given a venue with no stored google_place_id
    When the internal resolve endpoint is called for the venue
    Then the response status is 200
    And the response body contains an empty "venue_photos" list
    And no url-bearing entry is written to the fresh photo cache

  Scenario: A venue whose place returns no photos resolves to an empty list and caches it
    Given a venue with a stored google_place_id
    And Google returns no photos for that place
    When the internal resolve endpoint is called for the venue
    Then the response status is 200
    And the response body contains an empty "venue_photos" list
    And the fresh photo cache for the venue holds an empty list

  Scenario: A Google failure degrades to an empty list without caching a dead URL
    Given a venue with a stored google_place_id
    And Google photo resolution raises an error
    When the internal resolve endpoint is called for the venue
    Then the response status is 200
    And the response body contains an empty "venue_photos" list
    And no url-bearing entry is written to the fresh photo cache

  Scenario: A retry after a transient Google failure still returns photos
    Given a venue with a stored google_place_id
    And a previous resolve attempt failed and cached no url-bearing entry
    And Google now returns 2 photos for that place
    When the internal resolve endpoint is called for the venue
    Then the response status is 200
    And the response body contains a "venue_photos" list of 2 items
    And the fresh photo cache for the venue holds the same 2 items

  Scenario: The retired catalog-wide photo pre-bake trigger is unavailable
    When the "photos" admin enrichment job is triggered
    Then the response status is 404
    And the response indicates an unknown job
