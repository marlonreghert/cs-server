@wip
Feature: Photo resolve cost controls

  Every photo fetched from Google is a billed call. A caller must be able to ask
  for only the photos it will display, a cached entry must satisfy a caller that
  needs no more than it holds, and an entry that holds too few must upgrade
  rather than truncate what the caller shows.

  Background:
    Given the Google Places photo service is configured
    And the venue "v1" has a stored google_place_id

  # ── Buying only what is displayed ──────────────────────────────────────

  Scenario: A hero-only resolve buys exactly one photo
    Given no photos are cached for "v1"
    When photos are resolved for "v1" with max_photos 1
    Then exactly 1 photo media call must be made
    And the cached photo list for "v1" must hold 1 entry
    And the photos-fetched counter must increment by 1

  Scenario: Omitting max_photos preserves today's behaviour
    Given no photos are cached for "v1"
    When photos are resolved for "v1" without max_photos
    Then the number of photo media calls must equal the configured photos per venue
    And the cached photo list for "v1" must hold that many entries

  # ── The cache decides whether Google is called ─────────────────────────

  Scenario: A cached entry that already holds enough costs nothing
    Given 5 photos are cached for "v1"
    When photos are resolved for "v1" with max_photos 1
    Then no photo media call must be made
    And the response must contain 1 photo
    And the resolve outcome must be recorded as a cache hit

  Scenario: A cached entry holding too few is upgraded and overwritten
    Given 1 photo is cached for "v1"
    When photos are resolved for "v1" with max_photos 5
    Then 5 photo media calls must be made
    And the cached photo list for "v1" must hold 5 entries
    And the resolve outcome must be recorded as an upgrade

  Scenario: An empty cached entry is a definitive answer
    Given an empty photo list is cached for "v1"
    When photos are resolved for "v1" with max_photos 5
    Then no photo media call must be made
    And the response must contain no photos

  Scenario: A cached entry that exactly matches the request costs nothing
    Given 1 photo is cached for "v1"
    When photos are resolved for "v1" with max_photos 1
    Then no photo media call must be made
    And the response must contain 1 photo

  # ── Failure paths must not regress ─────────────────────────────────────

  Scenario: A Google failure never writes to the cache
    Given no photos are cached for "v1"
    And the Google photo details call fails
    When photos are resolved for "v1" with max_photos 1
    Then the response must contain no photos
    And nothing must be cached for "v1"

  Scenario: A Google failure never destroys an existing entry
    Given 5 photos are cached for "v1"
    And the Google photo details call fails
    When photos are resolved for "v1" with max_photos 5 forcing a re-resolve
    Then the previously cached photo list for "v1" must be unchanged

  Scenario: A venue with no google_place_id caches an empty list
    Given the venue "v2" has no stored google_place_id
    When photos are resolved for "v2" with max_photos 1
    Then no photo media call must be made
    And an empty photo list must be cached for "v2"

  Scenario: A Redis read failure degrades to resolving
    Given the photo cache cannot be read
    When photos are resolved for "v1" with max_photos 1
    Then 1 photo media call must be made
    And the request must not fail

  # ── Forced re-resolve: the dead-URL repair path ────────────────────────

  Scenario: A forced re-resolve ignores a live cached entry and overwrites it
    Given 5 photos are cached for "v1"
    And Google returns different photo urls for "v1"
    When photos are resolved for "v1" forcing a re-resolve
    Then photo media calls must be made
    And the cached photo list for "v1" must hold the newly resolved urls

  # ── Validation and TTL ─────────────────────────────────────────────────

  Scenario Outline: An out-of-range max_photos is rejected
    When photos are resolved for "v1" with max_photos <value>
    Then the request must be rejected as invalid

    Examples:
      | value |
      | 0     |
      | 99    |

  Scenario: A cached photo list is written with the 24 hour default TTL
    Given no admin override for the fresh photo cache TTL
    When photos are resolved for "v1" with max_photos 1
    Then the cached photo list for "v1" must expire in 24 hours

  Scenario: An admin TTL override still wins over the default
    Given the admin fresh photo cache TTL is set to 6 hours
    When photos are resolved for "v1" with max_photos 1
    Then the cached photo list for "v1" must expire in 6 hours

  # ── The pre-bake stays retired ─────────────────────────────────────────

  Scenario: The catalogue-wide photo pre-bake remains unreachable
    When the photo pre-bake job is triggered through the admin trigger endpoint
    Then the request must be rejected as not found
