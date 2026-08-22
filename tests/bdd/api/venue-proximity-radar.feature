@wip
Feature: Venue proximity (Radar) — staging, rollup, and notification ledger
  A consenting user's device computes which venues it passed near, in which band,
  in which part of which day, and uploads those day-rows. cs-server stages them
  briefly, rolls them up into an anonymous venue-day aggregate, and holds the
  ledger that caps how often the Radar alert may fire.

  A band is stored, never a distance. Integer metres against several known venue
  anchors multilaterate back to the user's real position, and the measurement is
  not accurate enough to justify the precision anyway.

  Only the aggregate survives. Staging is a dedup buffer with a hard partition
  drop, and every cell that reaches the aggregate carries at least five distinct
  users, enforced by constraint rather than convention.

  Background:
    Given the location pseudonymization key is configured and distinct from the engagement key
    And the maximum accepted accuracy is 60 meters
    And the maximum accepted batch size is 500 rows
    And the per-user per-day venue cap is 60
    And the staging retention window is 7 days
    And the rollup minimum distinct users is 5

  Scenario: A batch of proximity rows is persisted under the location pseudonym
    Given a consenting user with two proximity day-rows for different venues
    When the batch is uploaded
    Then the response must report 2 accepted rows
    And each persisted row must carry the location pseudonym
    And the location pseudonym must differ from the engagement pseudonym for the same user
    And the raw user id must not appear in any persisted column

  Scenario: Re-uploading the same natural key merges rather than duplicating
    Given a consenting user whose proximity batch has already been uploaded
    When the identical batch is uploaded again
    Then the response must report 0 accepted rows
    And the response must report 2 merged rows
    And the total number of staged rows must remain 2

  Scenario: The closer band wins a merge regardless of arrival order
    Given a staged row for a venue in the "passing" band
    When a row for the same venue, day, and day-part arrives in the "at" band
    Then the stored band must be "at"

  Scenario: The closer band still wins when the rows arrive in the reverse order
    Given a staged row for a venue in the "at" band
    When a row for the same venue, day, and day-part arrives in the "passing" band
    Then the stored band must remain "at"

  Scenario: A row measured with poor accuracy is rejected
    Given a consenting user with one proximity row whose best accuracy is 150 meters
    When the batch is uploaded
    Then the response must report 1 rejected row
    And no row must be staged
    And the rejection must be counted with reason "accuracy_out_of_range"

  Scenario: An invalid band is rejected without failing the batch
    Given a consenting user with one valid row and one row whose band is 7
    When the batch is uploaded
    Then the response must report 1 accepted row
    And the response must report 1 rejected row
    And exactly one row must be staged

  Scenario: The per-user per-day venue cap drops the surplus
    Given a consenting user with proximity rows for 70 distinct venues on one day
    When the batch is uploaded
    Then at most 60 rows must be staged for that day
    And the surplus must be reported as rejected
    And the rejection must be counted with reason "user_day_cap"

  Scenario: A venue inside the ring of a blocked venue is suppressed
    Given a consenting user who has blocked a venue
    And a proximity row for a different venue 200 meters from the blocked venue
    When the batch is uploaded
    Then the row must be suppressed
    And the rejection must be counted with reason "block_ring"

  Scenario: A row predating the user's erasure is dropped
    Given a consenting user who has erased their data
    And a proximity row whose business period precedes the erasure
    When the batch is uploaded
    Then the row must not be staged
    And the rejection must be counted with reason "tombstoned"

  Scenario: An oversized batch is truncated rather than refused
    Given a consenting user with 501 proximity rows
    When the batch is uploaded
    Then the response status must be 200
    And the surplus must be reported as rejected
    And the device must not be told to retry the same batch

  Scenario: The nightly rollup emits only cells with enough distinct users
    Given a venue with staged proximity rows from 6 distinct users in one day-part
    And another venue with staged rows from 4 distinct users in the same day-part
    When the rollup job runs
    Then an aggregate cell must exist for the venue with 6 users
    And no aggregate cell must exist for the venue with 4 users
    And the suppressed cell must be counted

  Scenario: Expired staging partitions are dropped
    Given staged proximity rows older than the retention window
    And staged proximity rows inside the retention window
    When the partition drop job runs
    Then the rows older than the window must be gone
    And the rows inside the window must remain

  Scenario: Account deletion erases the trace but not the aggregate
    Given a consenting user with staged proximity rows and radar notifications
    When the user's data is erased
    Then no staged proximity row must remain for that user
    And no radar notification record must remain for that user
    And the erasure response must report a buffer epoch
    And the aggregate cells must be unchanged

  Scenario: The radar cap survives a reinstall
    Given a user who has already received the maximum radar notifications this week
    And the device reports itself as freshly installed
    When a radar notification is considered for that user
    Then the notification must not be permitted
    And the cap-hit counter must be incremented

  Scenario: Proximity ingestion never logs the raw user id or coordinates
    Given a consenting user with one proximity row
    When the batch is uploaded
    Then the emitted logs must not contain the raw user id
    And the emitted logs must not contain any latitude or longitude
