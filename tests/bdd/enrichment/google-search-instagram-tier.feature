@wip
Feature: Find the Instagram handle through Google, and never trust it alone
  As the operator of the Instagram handle pipeline
  I want venues with no web presence found through search, but always adjudicated
  So that coverage grows without ever attaching somebody else's account

  Background:
    Given a searched venue named "Gildo Lanches" in "Boa Viagem"
    And the venue has no website and no archived payload

  Scenario: Find the handle Google surfaces
    Given Google returns a result linking "https://www.instagram.com/gildolanchespe/"
    When the search tier looks for a handle
    Then the search tier offers the handle "gildolanchespe"

  Scenario: Include the venue name and neighbourhood in the query
    Given Google returns a result linking "https://www.instagram.com/gildolanchespe/"
    When the search tier looks for a handle
    Then the query contained "Gildo Lanches"
    And the query contained "Boa Viagem"

  Scenario: Reject a link shim in the results
    Given Google returns a result linking "https://l.instagram.com/?u=https%3A%2F%2Fifood.com.br"
    When the search tier looks for a handle
    Then the search tier offers nothing

  Scenario: Reject a post link in the results
    Given Google returns a result linking "https://www.instagram.com/p/CxYzAbCd/"
    When the search tier looks for a handle
    Then the search tier offers nothing

  Scenario: Yield nothing when no Instagram appears in the results
    Given Google returns results with no Instagram link
    When the search tier looks for a handle
    Then the search tier offers nothing

  Scenario: Survive a failed search
    Given the search fails
    When the search tier looks for a handle
    Then the search tier offers nothing
    And the search lookup does not raise

  Scenario: Never accept a searched handle without the judge
    Given Google returns a result linking "https://www.instagram.com/gildolanchespe/"
    And no judge is available to adjudicate
    When the cascade discovers the searched venue
    Then the cascade does not accept the searched handle

  Scenario: Accept a searched handle the judge confirms
    Given Google returns a result linking "https://www.instagram.com/gildolanchespe/"
    And the judge confirms the searched profile
    When the cascade discovers the searched venue
    Then the cascade accepts the searched handle
    And the accepted handle came from the search tier

  Scenario: Reject a searched handle the judge rejects
    Given Google returns a result linking "https://www.instagram.com/saopedrorestaurante/"
    And the judge rejects the searched profile
    When the cascade discovers the searched venue
    Then the cascade does not accept the searched handle

  Scenario: Never search when a free tier already found the handle
    Given the venue's Google listing links "https://instagram.com/gildolanchespe"
    And the judge confirms the searched profile
    When the cascade discovers the searched venue
    Then Google is never searched

  Scenario: Honour the per-run toggle
    Given Google returns a result linking "https://www.instagram.com/gildolanchespe/"
    And the judge confirms the searched profile
    And the search tier is turned off for this run
    When the cascade discovers the searched venue
    Then Google is never searched
