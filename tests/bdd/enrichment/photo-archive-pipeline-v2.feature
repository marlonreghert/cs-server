Feature: Operate the photo archive as a bounded, versioned, costed pipeline
  As the venue platform operator
  I must be able to target a photo archive run, see what it will cost before I
  start it, keep each run in its own versioned partition, and trace what a run
  did afterwards,
  so that capturing catalog imagery is a controlled spend rather than one
  unbounded click against a per-photo Google bill.

  Background:
    Given the media archive is enabled with a configured bucket
    And Google Places photos are available for the catalog

  # 1 — Versioned run partitions
  Scenario: Give each run its own versioned partition
    Given the path mode is "new_run"
    When the photo archive job runs
    Then the images are stored under a run timestamp and run id partition
    And every media partition is expressed as a "key=value" directory

  Scenario: Keep two runs on the same day apart
    Given a run has already archived a venue today
    When a second run starts with the path mode "new_run"
    Then the second run writes to a different run partition
    And the first run's stored images remain untouched

  Scenario: Append to the most recent run when asked
    Given a previous run partition exists for the source
    When the photo archive job runs with the path mode "append_latest"
    Then the images are stored under that previous run partition

  Scenario: Fall back to a new run when there is nothing to append to
    Given no run partition exists for the source
    When the photo archive job runs with the path mode "append_latest"
    Then the images are stored under a newly created run partition

  Scenario: Resolve the latest run without reading any stored object
    Given a previous run partition exists for the source
    When the photo archive job resolves the latest run partition
    Then the partition is resolved by listing the bucket only
    And no archived object is read back

  Scenario: Record where the latest dump landed
    Given the path mode is "new_run"
    When the photo archive job completes successfully
    Then a latest marker for the source records the run partition it wrote to
    And the marker reports the run id, the venues archived, and the photos stored

  Scenario: Reject a path override that escapes the media prefix
    Given the path mode is "override" with a prefix outside the media root
    When the photo archive job is triggered
    Then the run is rejected before any Google request is made

  # 2 — Not paying twice
  Scenario: Skip a venue the previous run already archived
    Given a venue was archived by the previous run
    And the skip scope is "latest_run"
    When the photo archive job runs for that venue with the path mode "new_run"
    Then no Google request is made for that venue
    And the venue is counted as skipped

  Scenario: Re-fetch an already archived venue when overwrite is set
    Given a venue was archived by the previous run
    When the photo archive job runs for that venue with overwrite enabled
    Then the venue's photos are fetched again
    And the new run partition holds images for that venue

  Scenario: Refuse to disable skipping without an explicit overwrite
    Given the skip scope is "none" and overwrite is disabled
    When the photo archive job is triggered
    Then the run is rejected before any Google request is made

  # 3 — Bounding the run
  Scenario: Cap the number of venues in a run
    Given 40 venues are eligible
    And the maximum number of venues is 10
    When the photo archive job runs
    Then at most 10 venues are processed
    And the summary reports the number of venues the selection was truncated from

  Scenario: Cap the number of photos fetched per venue
    Given a venue with 8 available Google photos
    And the maximum number of photos per venue is 3
    When the photo archive job runs for that venue
    Then at most 3 photos are requested for that venue

  # 4 — Choosing the venues
  Scenario: Select venues within a radius of a point
    Given venues inside and outside a 2 km radius of a point
    When the photo archive job runs with a point and radius eligibility
    Then only the venues inside the radius are processed

  Scenario: Select venues by an explicit id list
    Given an eligibility list naming two known venues and one unknown venue
    When the photo archive job runs
    Then only the two known venues are processed
    And the unknown venue id is reported without failing the run

  Scenario: Reject an out-of-range radius
    Given a point and radius eligibility with a radius of 0 km
    When the photo archive job is triggered
    Then the run is rejected before any Google request is made

  # 5 — Estimating the cost first
  Scenario: Estimate a run without spending anything
    Given 20 venues are eligible
    And the maximum number of photos per venue is 5
    When a cost estimate is requested for that configuration
    Then the estimate reports at most 100 Google requests
    And the estimate reports an estimated cost in dollars
    And no Google request is made
    And the estimate states that it is an upper bound and may be wrong

  Scenario: Rehearse a run without writing anything
    Given the run is configured as a dry run
    When the photo archive job runs
    Then the eligible venues are selected and an estimate is produced
    And no Google request is made
    And no image is stored

  # 6 — Rate limiting and throttling
  Scenario: Retry a throttled Google response
    Given Google responds to the first photo request with a throttling error
    When the photo archive job runs for that venue
    Then the request is retried after a backoff
    And the throttled response is counted
    And the run completes

  Scenario: Give up on a persistently throttled venue without ending the run
    Given Google throttles one venue beyond the retry limit
    And another eligible venue responds normally
    When the photo archive job runs
    Then the throttled venue is counted as failed
    And the other venue is archived

  # 7 — Tracing a run
  Scenario: Return a job id when a run is triggered
    When the photo archive job is triggered
    Then the response carries a job id for that run

  Scenario: Retrieve what a run did by its job id
    Given a photo archive run has completed
    When the run record is requested by its job id
    Then the record reports the configuration, the counts, and the duration of that run

  # 8 — Failure isolation
  Scenario: Keep archiving after one venue fails
    Given two eligible venues where the first fails to fetch
    When the photo archive job runs
    Then the first venue is counted as failed
    And the second venue is archived
