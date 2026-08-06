Feature: Event cover presign
  As the operator reviewing extracted events
  I want a short-lived viewable url for an event's archived flyer
  So that the console can show me the image the event was read from, without
  the bucket ever being public and without the browser choosing which object
  gets signed

  Background:
    Given the operator is authenticated as an admin

  Scenario: Return a signed url for an event with an archived cover
    Given an event whose cover photo is archived
    When the cover url for that event is requested
    Then a signed url is returned
    And an expiry is returned with it
    And the result "signed" is counted

  Scenario: Report an event that has no archived cover
    Given an event with no cover photo key
    When the cover url for that event is requested
    Then the request is rejected as not found
    And the reason distinguishes a missing cover from a missing event
    And the result "no_key" is counted

  Scenario: Report an unknown event
    Given no event exists for the requested id
    When the cover url for that event is requested
    Then the request is rejected as not found
    And the reason names the missing event
    And the result "not_found" is counted

  Scenario: Never return a success carrying no url
    Given an event whose cover photo is archived
    And signing the object fails
    When the cover url for that event is requested
    Then the request fails with a server error
    And no successful response carrying an empty url is returned
    And the result "failed" is counted

  Scenario: Require admin authentication
    Given the caller is not authenticated as an admin
    When the cover url for an event is requested
    Then the request is rejected as unauthorised

  Scenario: Resolve the object from the event, never from the request
    Given an event whose cover photo is archived
    When the cover url is requested with an object key supplied in the request
    Then the signed object is the one recorded on the event
    And the key supplied in the request is ignored

  Scenario: Keep the signed url out of the logs
    Given an event whose cover photo is archived
    When the cover url for that event is requested
    Then the signed url does not appear in the logs

  Scenario: Resolve the cover route ahead of the event catch-all
    Given an event whose cover photo is archived
    When the cover url for that event is requested
    Then the cover route handles the request
    And the request is not handled as a single-event lookup
