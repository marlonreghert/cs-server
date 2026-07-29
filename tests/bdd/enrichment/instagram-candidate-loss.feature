@wip
Feature: Instagram candidate loss
  As the operator of the Instagram handle pipeline
  I want every usable candidate a search returns to survive parsing
  So that a change in the upstream payload cannot silently empty the pipeline

  Background:
    Given the Instagram search returns results from Apify

  Scenario: Keep a candidate whose external link is an object
    Given a search result whose external link is an object with a lynx url
    When the search results are parsed
    Then the candidate is kept
    And the candidate carries the url from that object

  Scenario: Keep a candidate whose external link is a plain string
    Given a search result whose external link is a plain string
    When the search results are parsed
    Then the candidate is kept
    And the candidate carries that string as its url

  Scenario: Keep a candidate that has no external link
    Given a search result with no external link
    When the search results are parsed
    Then the candidate is kept
    And the candidate carries no url

  Scenario: Keep the profile when only its link is unusable
    Given a search result whose external link has no recognisable url
    When the search results are parsed
    Then the candidate is kept
    And the candidate carries no url

  Scenario: Count a candidate that cannot be parsed at all
    Given a search result that cannot be parsed
    When the search results are parsed
    Then that candidate is not returned
    And a dropped candidate is counted with the reason "parse_error"

  Scenario: One unusable result does not discard the usable ones
    Given a search result that cannot be parsed
    And a search result whose external link is an object with a lynx url
    When the search results are parsed
    Then exactly 1 candidate is kept

  Scenario: Restrict a run to the venue ids the operator supplied
    Given the servable catalogue holds the venues "ven_a, ven_b, ven_c"
    When the cascade runs for the venue ids "ven_a, ven_c"
    Then the cascade is attempted for exactly the venues "ven_a, ven_c"
    And the run considered 2 venues

  Scenario: Report unknown venue ids without failing the run
    Given the servable catalogue holds the venues "ven_a, ven_b"
    When the cascade runs for the venue ids "ven_a, ven_ghost"
    Then the cascade is attempted for exactly the venues "ven_a"
    And the run reports 1 unknown venue id

  Scenario: Run the whole catalogue when no venue ids are given
    Given the servable catalogue holds the venues "ven_a, ven_b, ven_c"
    When the cascade runs with no venue ids
    Then the cascade is attempted for exactly the venues "ven_a, ven_b, ven_c"

  Scenario: Treat a blank venue id list as the whole catalogue
    Given the servable catalogue holds the venues "ven_a, ven_b"
    When the cascade runs for the venue ids ""
    Then the cascade is attempted for exactly the venues "ven_a, ven_b"
