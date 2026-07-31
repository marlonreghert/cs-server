Feature: Settle ambiguous Instagram candidates with a judge
  As the operator of the Instagram handle pipeline
  I want a judge consulted when the cheap signals cannot decide
  So that plausible candidates are examined instead of discarded unseen

  Background:
    Given a venue awaiting adjudication named "Tio Pepe"
    And a candidate the cheap signals scored below the bar

  Scenario: Accept a candidate the judge confirms
    Given the judge confirms the profile belongs to the venue
    When the venue is adjudicated
    Then the candidate is accepted
    And the stored record names the judge mode

  Scenario: Reject a candidate the judge rejects
    Given the judge says the profile belongs to somebody else
    When the venue is adjudicated
    Then the candidate is not accepted

  Scenario: Cap a verdict reached without any images
    Given the judge confirms the profile belongs to the venue
    And no images are available to compare
    When the venue is adjudicated
    Then the recorded confidence is at most 0.80

  Scenario: Leave the candidate alone when no judge is configured
    Given no judge is configured
    When the venue is adjudicated
    Then the candidate keeps the confidence the cheap signals gave it
    And the run records that the judge was unavailable

  Scenario: Leave the candidate alone when the judge fails
    Given the judge fails to answer
    When the venue is adjudicated
    Then the candidate keeps the confidence the cheap signals gave it
    And the run records that the judge was unavailable

  Scenario: Adjudicate a low-provenance candidate the old floor dropped
    Given the candidate came from the paid search and scored 0.43
    And the judge confirms the profile belongs to the venue
    When the venue is adjudicated
    Then the candidate is accepted

  Scenario: Never adjudicate a candidate already above the bar
    Given the candidate already scores above the bar
    When the venue is adjudicated
    Then the judge is never consulted

  Scenario: Never adjudicate a profile confirmed absent
    Given the profile is confirmed not to exist
    When the venue is adjudicated
    Then the judge is never consulted
    And the candidate is not accepted

  Scenario: Turn the judge off for a single run
    Given the judge confirms the profile belongs to the venue
    And the judge is turned off for this run
    When the venue is adjudicated
    Then the judge is never consulted
