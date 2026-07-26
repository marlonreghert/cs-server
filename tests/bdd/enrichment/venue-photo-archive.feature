Feature: Archive venue photos to the dated S3 media prefix
  As the venue platform operator
  I must be able to download every available Google photo for the venues I choose
  into a dated media partition, without paying Google twice for photos I already
  have,
  so the catalog's imagery is captured as dated snapshots instead of expiring
  keyless URLs.

  Background:
    Given the media archive is enabled with a configured bucket
    And Google Places photos are available for the catalog

  # 1 — Archiving the images
  Scenario: Archive every available photo for a venue
    Given a venue with 8 available Google photos
    When the photo archive job runs for that venue
    Then 8 images are stored for that venue
    And each image is stored under the source, day, and venue partition
    And every media partition is expressed as a "key=value" directory

  Scenario: Keep the author attributions with the images
    Given a venue with 3 available Google photos carrying author attributions
    When the photo archive job runs for that venue
    Then a manifest stored beside the images records each photo's author attribution
    And the manifest names the photo id and content type of each image

  # 2 — Choosing where a run writes
  Scenario: Write to today's partition by default
    Given the path mode is "new_day"
    When the photo archive job runs
    Then the images are stored under today's day partition

  Scenario: Append to the most recent existing day
    Given the media archive already holds partitions for 2026-07-20 and 2026-07-24
    And the path mode is "append_latest"
    When the photo archive job runs
    Then the images are stored under the 2026-07-24 partition
    And no new day partition is created

  Scenario: Fall back to a new day when nothing has been archived yet
    Given the media archive holds no partitions
    And the path mode is "append_latest"
    When the photo archive job runs
    Then the images are stored under today's day partition

  Scenario: Write to an explicit override prefix
    Given the path mode is "override" with the prefix "media/manual/backfill-2026-07/"
    When the photo archive job runs
    Then the images are stored under that prefix

  Scenario Outline: Reject an override prefix that escapes the media prefix
    Given the path mode is "override" with the prefix "<prefix>"
    When the photo archive job is triggered
    Then the run is rejected before any Google call is made
    And no images are stored

    Examples:
      | prefix              |
      | raw/source=besttime |
      | media/../raw/       |
      |                     |

  # 3 — Never pay Google twice
  Scenario: Skip a venue already archived in the target partition
    Given a venue whose images are already stored in the target partition
    When the photo archive job runs for that venue
    Then no Google call is made for that venue
    And the venue is reported as skipped

  Scenario: Re-download an already archived venue when overwrite is requested
    Given a venue whose images are already stored in the target partition
    And overwrite is requested
    When the photo archive job runs for that venue
    Then the venue's photos are fetched from Google again
    And the venue is reported as archived

  # 4 — Choosing which venues run
  Scenario: Restrict the run to a comma-separated venue id list
    Given the catalog holds venues "ven_a", "ven_b", and "ven_c"
    And the run is restricted to the venue ids "ven_a, ven_c"
    When the photo archive job runs
    Then only venues "ven_a" and "ven_c" are considered
    And no photos are fetched for "ven_b"

  Scenario: Report unknown venue ids instead of failing the run
    Given the catalog holds venue "ven_a"
    And the run is restricted to the venue ids "ven_a, ven_missing"
    When the photo archive job runs
    Then venue "ven_a" is archived
    And the summary reports "ven_missing" as unknown
    And the run completes successfully

  Scenario: Archive the whole active catalog when no venue ids are given
    Given the catalog holds 3 active venues
    And the run names no venue ids
    When the photo archive job runs
    Then all 3 venues are considered

  # 5 — One failure must not lose the run
  Scenario: Continue when one venue's Google fetch fails
    Given a venue whose Google photo fetch fails
    And a second venue with available photos
    When the photo archive job runs
    Then the failing venue is reported as failed
    And the second venue's photos are still archived
    And the run completes successfully

  Scenario: Keep the other photos when one download fails
    Given a venue with 4 available Google photos where the second download fails
    When the photo archive job runs for that venue
    Then 3 images are stored for that venue
    And the failed photo is counted as a download failure

  Scenario: Skip a venue that has no Google place id
    Given a venue with no Google place id
    When the photo archive job runs for that venue
    Then no Google call is made for that venue
    And the venue is reported as having no place id

  # 6 — Knowing what a run did and cost
  Scenario: Report a summary of the run
    When the photo archive job completes
    Then the summary reports the venues considered, skipped, archived, and failed
    And the summary reports the number of photos stored
    And the summary names the day partition the run wrote to

  Scenario: Expose the metrics a run's cost is judged by
    When the photo archive job completes
    Then the number of photos stored is exposed per source
    And the venue outcomes are exposed per source and result
    And the bytes stored are exposed per source
    And the timestamp of the last successful run is exposed
