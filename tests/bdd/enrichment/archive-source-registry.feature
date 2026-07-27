Feature: Choose where archived photos come from
  As the venue platform operator
  I must be able to pick which source a photo archive run uses, see what each
  one costs, and be stopped before spending when a source is not usable,
  so that a cheaper source can be adopted without rebuilding the pipeline and
  without guessing the bill.

  Background:
    Given the media archive is enabled with a configured bucket
    And Google Places photos are available for the catalog

  # 1 — The catalog
  Scenario: Publish every source with its label and its own settings
    When the archive job's sources are listed
    Then both the Google and the Apify sources are offered
    And the Apify source declares its own configuration fields
    And the Google source declares no extra configuration fields

  Scenario: Report a source whose dependency is missing as unavailable
    Given the Apify token is not configured
    When the archive job's sources are listed
    Then the Apify source is reported unavailable
    And the reason names the missing Apify token

  # 2 — Refusing to spend on an unusable choice
  Scenario: Reject an unknown source before spending
    Given the run names the source "carrier_pigeon"
    When the photo archive job is triggered
    Then the run is rejected before any Google request is made

  Scenario: Reject a source whose dependency is missing before spending
    Given the Apify token is not configured
    And the run names the source "apify_gmaps_extractor"
    When the photo archive job is triggered
    Then the run is rejected before any Google request is made

  # 3 — Running against the chosen source
  Scenario: Archive through the Apify source
    Given the Apify source is configured
    And a venue the extractor can find with 4 photos
    When the photo archive job runs for that venue using the Apify source
    Then the Apify source partition holds the images
    And no Google request is made

  Scenario: Keep everything the extractor returned that is not an image
    Given the Apify source is configured
    And a venue the extractor can find with 4 photos
    When the photo archive job runs for that venue using the Apify source
    Then the venue's info folder holds the place data
    And the place data keeps the fields the scrape already paid for
    And no image payload is duplicated inside the place data

  Scenario: Keep the place data even when the venue has no photos
    Given the Apify source is configured
    And a venue the extractor finds with no photos
    When the photo archive job runs for that venue using the Apify source
    Then the venue's info folder holds the place data
    And the venue is counted as info only

  Scenario: File photos under the category they belong to
    Given the Apify source is configured
    And a venue whose photos are 2 from the owner and 3 from visitors
    When the photo archive job runs for that venue using the Apify source
    Then the owner photos are stored in their own folder
    And the visitor photos are stored in their own folder

  Scenario: Cap how many photos are kept per category
    Given the Apify source is configured
    And a venue whose photos are 2 from the owner and 3 from visitors
    And at most 1 photo per category is kept
    When the photo archive job runs for that venue using the Apify source
    Then only 1 photo is stored in each category folder

  Scenario: Count a venue the extractor cannot find without failing the run
    Given the Apify source is configured
    And a venue the extractor cannot find
    And a second venue the extractor can find with 2 photos
    When the photo archive job runs using the Apify source
    Then the unmatched venue is counted as unmatched
    And the matched venue is archived

  Scenario: Stop the run cleanly when the Apify credits run out
    Given the Apify source is configured
    And the Apify account has no credits left
    When the photo archive job runs using the Apify source
    Then the run stops and reports that the credits are exhausted

  # 4 — Costing each source on its own terms
  Scenario: Count the Place Details call in the Google estimate
    Given 10 venues are eligible
    And the maximum number of photos per venue is 4
    When a cost estimate is requested for the Google source
    Then the estimate reports 50 billable units
    And the estimate describes the units as Google requests

  Scenario: Price the Apify source per place rather than per photo
    Given the Apify source is configured
    And 10 venues are eligible
    And the maximum number of photos per venue is 4
    When a cost estimate is requested for the Apify source
    Then the estimate reports 10 billable units
    And the estimate describes the units as places
    And the estimate states that the per-image charge is not published
