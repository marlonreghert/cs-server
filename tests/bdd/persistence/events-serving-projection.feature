@wip
Feature: Events serving projection
  As the VibeSense app
  I want extracted events to reach the shared Redis serving projection
  So that the events list and detail screens can be served without touching
  RDS or S3 on the request path

  Background:
    Given the events projection is enabled
    And the projection horizon is 21 days
    And "Casa Bacurau" is a servable venue in Recife
    And the current time is 2026-09-05 18:00 in Recife

  # ── what gets projected ───────────────────────────────────────────────────

  Scenario: Project an accepted event with its full serving payload
    Given an accepted event "Grande Encontro Techno" at "Casa Bacurau" starting 2026-09-06 20:00
    And the event has category "techno", price text "R$ 40", ticket info "Grátis até 23:30"
    And the event has a description and an attractions list with one act on stage "Pista NY"
    When the events projection runs
    Then the occurrence payload contains the title, description, category, price text and ticket info
    And the occurrence payload contains the attractions list with the act name, type, stage and styles
    And the occurrence payload contains the venue id, venue name, latitude and longitude
    And the occurrence payload contains the source permalink and source handle
    And the occurrence carries the local occurrence date "2026-09-06"
    And the occurrence carries "starts_at" as a UTC timestamp
    And the Recife events index contains the occurrence scored by its "starts_at" epoch

  Scenario: Carry the price text and the ticket info independently
    Given an accepted event whose price text is "R$100 em consumação"
    And whose ticket info is "Grátis até 23:30"
    When the events projection runs
    Then the occurrence payload reports both strings on their own fields

  Scenario: Project an event whose stated time is unknown
    Given an accepted event at "Casa Bacurau" starting 2026-09-06 with no stated clock time
    When the events projection runs
    Then the occurrence payload reports "time_known" as false

  Scenario Outline: Exclude events that must never be served
    Given an event at "Casa Bacurau" starting 2026-09-06 20:00 that is <disqualifier>
    When the events projection runs
    Then no occurrence is projected for that event

    Examples:
      | disqualifier                      |
      | still pending review              |
      | rejected                          |
      | superseded by another event       |
      | of post type "menu"               |
      | linked to no venue                |
      | linked to a non-servable venue    |

  Scenario: Hide promoter-sourced events while the admin flag is set
    Given "hide_promoter_events" is true
    And an accepted event at "Casa Bacurau" whose every source is a promoter post
    When the events projection runs
    Then no occurrence is projected for that event
    When an admin sets "hide_promoter_events" to false
    And the events projection runs
    Then the occurrence is projected for that event

  Scenario: Keep last night's event visible into the small hours
    Given an accepted event at "Casa Bacurau" starting 2026-09-04 22:00
    And the current time is 2026-09-05 01:00 in Recife
    When the events projection runs
    Then the occurrence is projected

  Scenario: Drop an event that is genuinely past
    Given an accepted event at "Casa Bacurau" starting 2026-09-01 22:00
    When the events projection runs
    Then no occurrence is projected for that event

  # ── recurrence expansion ──────────────────────────────────────────────────

  Scenario: Expand a weekly announcement into one occurrence per night
    Given an accepted recurring event "Forró da Quinta" at "Casa Bacurau"
    And its recurrence text is "toda quinta" and its resolved time is 21:00
    When the events projection runs
    Then an occurrence is projected for every Thursday within the horizon
    And each occurrence carries its own occurrence id ending in its own date
    And each occurrence starts at 21:00 Recife time on its own date
    And no occurrence is projected beyond the horizon

  Scenario: Expand a daily announcement into one occurrence per day
    Given an accepted recurring event at "Casa Bacurau" whose recurrence text is "todo dia"
    When the events projection runs
    Then an occurrence is projected for every day within the horizon

  Scenario: Invent no days for a recurrence with no computable day
    Given an accepted recurring event at "Casa Bacurau" whose recurrence text is "toda semana"
    When the events projection runs
    Then exactly one occurrence is projected at its resolved start time

  Scenario: Project nothing for an event that has no start time at all
    Given an accepted event at "Casa Bacurau" with no resolved start time
    When the events projection runs
    Then no occurrence is projected for that event

  # ── re-assertion and failure posture ──────────────────────────────────────

  Scenario: Index every occurrence by city and by venue
    Given an accepted event at "Casa Bacurau" starting 2026-09-06 20:00
    When the events projection runs
    Then the Recife events index contains the occurrence
    And the venue events index for "Casa Bacurau" contains the same occurrence
    And the occurrence carries the same score in both indexes

  Scenario: Read one venue's week from its own index
    Given two accepted events at "Casa Bacurau" and one at another venue
    When the events projection runs
    Then the venue events index for "Casa Bacurau" contains exactly its two occurrences

  Scenario: Remove an occurrence once its event stops qualifying
    Given an accepted event at "Casa Bacurau" starting 2026-09-06 20:00
    And the events projection has run
    When an admin rejects the event
    And the events projection runs
    Then the occurrence key is deleted
    And the occurrence is no longer a member of the Recife events index
    And the occurrence is no longer a member of the venue events index

  Scenario: Leave the projection intact when the source query fails
    Given the events projection has run and projected three occurrences
    When the events selection query fails
    And the events projection runs
    Then the three occurrences are still present in Redis
    And the run summary reports an error

  Scenario: Isolate a single failing event from the rest of the cycle
    Given three accepted events at "Casa Bacurau"
    And projecting the second one raises an error
    When the events projection runs
    Then the other two occurrences are projected
    And the run summary names the failing event id

  # ── flyer media ───────────────────────────────────────────────────────────

  Scenario: Copy an archived flyer to the app media bucket and serve its url
    Given an accepted event at "Casa Bacurau" with an archived cover photo
    When the events projection runs
    Then the flyer is stored in the media bucket under a content-addressed key
    And the stored object carries the immutable cache control header
    And the occurrence payload carries the CloudFront flyer url

  Scenario: Upload nothing when the flyer bytes have not changed
    Given an accepted event at "Casa Bacurau" whose flyer has already been copied
    When the events projection runs
    Then no object is uploaded to the media bucket
    And the occurrence payload carries the same flyer url as before

  Scenario: Project an event that has no flyer at all
    Given an accepted event at "Casa Bacurau" with no archived cover photo
    When the events projection runs
    Then the occurrence is projected with a null flyer url
    And the flyer copy outcome "no_key" is recorded

  Scenario: Report a denied media write loudly and still project the event
    Given an accepted event at "Casa Bacurau" with an archived cover photo
    And the media bucket rejects the write with access denied
    When the events projection runs
    Then the occurrence is projected with a null flyer url
    And the flyer copy outcome "access_denied" is recorded

  Scenario: Never serve a data-lake object directly
    Given an accepted event at "Casa Bacurau" with an archived cover photo
    When the events projection runs
    Then no occurrence payload contains a data-lake key or a presigned url

  # ── neighbourhood ─────────────────────────────────────────────────────────

  Scenario: Store the bairro from the Places address components
    Given Google Places returns address components with sublocality level 1 "Santo Amaro"
    When "Casa Bacurau" is enriched
    Then the stored address neighborhood is "Santo Amaro"

  Scenario: Fall back to sublocality when no sublocality level 1 is returned
    Given Google Places returns address components with sublocality "Boa Vista" and no sublocality level 1
    When "Casa Bacurau" is enriched
    Then the stored address neighborhood is "Boa Vista"

  Scenario: Keep a stored bairro when the response carries no component for it
    Given the stored address neighborhood is "Santo Amaro"
    And Google Places returns address components with no sublocality of any kind
    When "Casa Bacurau" is enriched
    Then the stored address neighborhood is still "Santo Amaro"

  Scenario: Carry the bairro onto every occurrence at that venue
    Given the stored address neighborhood for "Casa Bacurau" is "Santo Amaro"
    And an accepted event at "Casa Bacurau" starting 2026-09-06 20:00
    When the events projection runs
    Then the occurrence payload reports the venue neighborhood "Santo Amaro"

  Scenario: Project a null neighbourhood for a venue that has never been enriched
    Given "Casa Bacurau" has no stored address neighborhood
    And an accepted event at "Casa Bacurau" starting 2026-09-06 20:00
    When the events projection runs
    Then the occurrence payload reports a null venue neighborhood
    And the occurrence is still projected

  # ── observability ─────────────────────────────────────────────────────────

  Scenario: Report the size of the projection every cycle
    Given four accepted events at "Casa Bacurau"
    When the events projection runs
    Then the run summary reports the projected occurrence count
    And the run summary reports the total projected payload bytes
