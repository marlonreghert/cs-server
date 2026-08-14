Feature: Show every archived image behind an event
  As the operator of the event pipeline
  I must be able to see every image the pipeline archived for an event, grouped
  by the post it came from and marked with the one the extractor actually read,
  so that I can tell whether merging several posts into one item combined their
  information or quietly dropped a flyer nobody ever looked at.

  Background:
    Given an event announced by three posts on the same venue account
    And the archive holds five classified images across those three posts

  Scenario: Return every archived image for the event
    When the event's media is requested
    Then five images are returned

  Scenario: Group the images under the post each came from
    When the event's media is requested
    Then each image is reported under its own post's shortcode
    And no image is reported under a post it does not belong to

  Scenario: Order the posts oldest first
    When the event's media is requested
    Then the posts are ordered by when they were published, oldest first

  Scenario: Mark the image the extractor read
    When the event's media is requested
    Then exactly one image per post is marked as the one the extractor read
    And the image classified as a flyer that was never read is marked as unread

  Scenario: Report each image's classification
    When the event's media is requested
    Then each image reports its classified category and its confidence

  Scenario: Report an event with no archived media as empty, not as an error
    Given an event whose posts archived no images at all
    When the event's media is requested
    Then the request succeeds
    And no images are returned

  Scenario: Fall back to the stored cover when a run manifest cannot be read
    Given the archived manifest for one of the posts cannot be read
    When the event's media is requested
    Then that post still reports its stored cover image
    And the manifest fallback is counted

  Scenario: Omit an image that could not be signed and return the rest
    Given one image's url cannot be signed
    When the event's media is requested
    Then the remaining images are still returned
    And the signing failure is counted

  Scenario: Read one manifest per run when several posts share a run
    Given all three posts were archived in the same run
    When the event's media is requested
    Then the archive is read once

  Scenario: Refuse the request without the admin credential
    When the event's media is requested without the admin credential
    Then the request is refused

  Scenario: Never sign a key supplied by the caller
    When the event's media is requested with a storage key in the request
    Then no url is signed for that key

  Scenario: Report an unknown event as not found
    When media is requested for an event that does not exist
    Then the request reports the event was not found

  Scenario: Leave the existing cover endpoint unchanged
    When the event's cover is requested
    Then a single signed url for the event's own cover is returned
