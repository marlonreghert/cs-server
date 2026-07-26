@wip
Feature: Archive raw BestTime responses to the S3 data lake
  As the venue platform
  I must persist every response BestTime returns as immutable, partitioned raw
  data, and I must never let that archival affect ingestion,
  so the full history of BestTime observations stays queryable while refresh
  pipelines keep working exactly as they do today.

  Background:
    Given the data lake is enabled with a configured bucket
    And the data lake writer is running

  # 1 — Every BestTime dataset is archived
  Scenario: Archive a successful live forecast fetch
    Given a venue that is due for a live busyness refresh
    When the live forecast refresh job fetches that venue from BestTime
    Then one archived record is written for the "live_forecast" dataset
    And the record's payload is byte-identical to the response BestTime returned
    And the record reports the outcome "success" with the HTTP status BestTime returned
    And the record carries the venue id, the job name, and the run id

  Scenario Outline: Map each BestTime endpoint to its own dataset
    When the platform calls BestTime for "<call>"
    Then the archived record belongs to the "<dataset>" dataset
    And the archived record names the endpoint BestTime was called on

    Examples:
      | call                  | dataset            |
      | live forecast         | live_forecast      |
      | weekly raw forecast   | week_raw_forecast  |
      | venue filter search   | venue_filter       |
      | venue create          | venue_create       |
      | account inventory     | account_inventory  |

  Scenario: Archive one record per page of account inventory
    Given the BestTime account inventory spans three pages
    When the platform lists the account inventory
    Then three archived records are written for the "account_inventory" dataset

  # 2 — Partition layout is the query contract
  Scenario: Partition archived records by source, dataset, and UTC time
    When a live forecast is archived
    Then the archived object key is partitioned by source, dataset, date, and hour
    And every partition is expressed as a "key=value" directory
    And the archived object is gzipped NDJSON with one JSON object per line

  Scenario: Partition by UTC while carrying Recife local time in the record
    Given the platform fetches a live forecast at 21:00 Recife time on 25 July 2026
    When that response is archived
    Then the record is stored under the UTC date 2026-07-26 and UTC hour 00
    And the record reports the Recife date 2026-07-25 and the Recife hour 21

  Scenario: Group many responses from one refresh window into a single object
    Given the live refresh job fetches forty venues in one window
    When the archival buffer is flushed
    Then a single archived object contains forty NDJSON lines
    And one archived object is written rather than one per venue

  # 3 — Credentials must never reach the lake
  Scenario: Never archive BestTime credentials
    Given BestTime is called with its private and public API keys in the query string
    When the response is archived
    Then the archived record contains no BestTime private key
    And the archived record contains no BestTime public key
    And the archived record still reports the non-secret request parameters

  # 4 — Failures are data too
  Scenario: Archive a failed BestTime fetch
    Given BestTime times out for a venue's live forecast
    When the refresh job handles that failure
    Then an archived record reports the outcome "error" with an empty payload
    And the record describes the failure
    And the refresh job continues with the remaining venues

  # 5 — Archival must never break ingestion
  Scenario: Complete the refresh when S3 is unreachable
    Given every upload to the data lake fails
    When the live forecast refresh job runs
    Then the refresh job completes successfully
    And the refreshed venues are still written to the system of record
    And the serving projection is still updated
    And the dropped records are counted with the reason "flush_failed"
    And an error is logged naming the dataset and the number of records lost

  Scenario: Never block ingestion when the archival queue is full
    Given the archival queue is saturated
    When the live forecast refresh job runs
    Then the refresh job completes without waiting on the data lake
    And the excess records are counted as dropped with the reason "queue_full"

  Scenario: Absorb an archival defect without changing BestTime behavior
    Given the data lake writer raises an error for every record it receives
    When the platform calls BestTime for a venue filter search that matches nothing
    Then the search result is unchanged from when the data lake is disabled
    And the error from the data lake writer is logged and swallowed

  Scenario: Keep BestTime quota errors unchanged while archiving
    Given BestTime rejects a venue create with its monthly cap message
    When the platform handles that rejection
    Then the rejection is surfaced exactly as it is when the data lake is disabled
    And the rejection is archived for the "venue_create" dataset

  # 6 — Kill switch and shutdown
  Scenario: Stay inert when the data lake is disabled
    Given the data lake is disabled
    When the live forecast refresh job runs
    Then no object is written to the data lake
    And no data lake metric is emitted

  Scenario: Flush buffered records on shutdown
    Given records are buffered but not yet flushed
    When the application shuts down
    Then the buffered records are uploaded before shutdown completes

  # 7 — Grafana visibility
  Scenario: Expose archival health as metrics
    When records are archived and flushed
    Then the enqueued record count is exposed per source and dataset
    And the flush count is exposed per dataset with a success or error status
    And the current archival queue depth is exposed
    And the timestamp of the last successful flush is exposed
