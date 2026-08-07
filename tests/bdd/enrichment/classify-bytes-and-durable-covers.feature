Feature: Classify from bytes, and give every event a durable cover
  As the operator of the event pipeline
  I want photos classified from the bytes we already downloaded, and every event
  pointing at its archived image
  So that Instagram photos are not archived uncategorised, and an event survives
  the post behind it being edited or deleted

  Scenario: Classify an archived Instagram photo from its bytes
    Given an Instagram post whose image has been downloaded
    When the archive classifies it
    Then the photo is stored under its classified category
    And the manifest entry records that category

  Scenario: Send no provider url to the model on the live archive path
    Given an Instagram post whose image has been downloaded
    When the archive classifies it
    Then the model receives the image as bytes
    And no provider url is sent to the model

  # NOTE (execute-feature, 2026-08-07): the plan's approach doc describes this
  # path as "presigns S3". Verified against app/services/venue_photo_archive_
  # service.py::_rederive_venue: it does not presign — it reads the object's
  # bytes back (media_store.read_image_data_uri, the s3:GetObject grant on
  # retrieved/* exists for exactly this) and inlines them as a data URI,
  # because a presigned url built from temporary instance-role credentials is
  # ~1,900 chars and OpenAI already refuses it outright. That was already true
  # before this branch. The scenario is corrected to match shipped behaviour
  # rather than assert something that would reintroduce a known, already-fixed
  # bug.
  Scenario: Classify from the archived bytes when re-deriving over an archived run
    Given an archived run whose attributes are re-derived
    When the archived photos are classified
    Then the model receives each photo's own archived bytes
    And no provider url is sent to the model

  Scenario: Report an unfetchable image as an image failure
    Given the model cannot fetch an image
    When the archive classifies it
    Then the failure is reported as an image failure
    And the failure is not reported as a rejected parameter
    And it is counted under its own reason

  Scenario: Keep a photo archived when its classification fails
    Given the model cannot fetch an image
    When the archive classifies it
    Then the photo is still archived
    And it keeps the category its source gave it

  Scenario: Keep signed urls out of the classifier logs
    Given the model cannot fetch an image
    When the archive classifies it
    Then no signed url appears in the classifier logs

  Scenario: Record the archived cover on a promoter event
    Given a promoter post whose image has been archived
    When the promoter crawl extracts an event from it
    Then the event records the archived cover key
    And the event still records its source permalink

  Scenario: Give every event from one promoter post the same cover
    Given a promoter post whose image has been archived
    And that post announces three events
    When the promoter crawl extracts them
    Then all three events record the archived cover key

  Scenario: Leave the cover empty when a post stored no image
    Given a promoter post whose images could not be archived
    When the promoter crawl extracts an event from it
    Then the event records no cover key
    And the event is still persisted

  Scenario: Evaluate the whole category gate output by default
    Given 40 venues pass the category gate
    When event targeting runs with its default bound
    Then all 40 venues are evidence-evaluated

  Scenario: Skip a venue with no Instagram handle before any archive listing
    Given a venue that passes the category gate but has no Instagram handle
    When event targeting runs
    Then that venue is recorded as unevaluated
    And no archive listing is performed for it
