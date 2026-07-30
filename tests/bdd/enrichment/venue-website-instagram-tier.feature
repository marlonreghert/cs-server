Feature: Read the Instagram handle from the venue's own website
  As the operator of the Instagram handle pipeline
  I want the venue's own website consulted before the paid search
  So that handles a venue publishes itself cost nothing to find

  Background:
    Given a venue named "Buca Trattoria"

  Scenario: Find the handle the venue links from its own website
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that page links "https://www.instagram.com/bucatrattoria/"
    When the website tier looks for a handle
    Then the website tier offers the handle "bucatrattoria"

  Scenario: Skip the fetch when the listed website is already Instagram
    Given the venue's listed website is "https://instagram.com/bucatrattoria"
    When the website tier looks for a handle
    Then the website tier offers nothing
    And the venue's website is never fetched

  Scenario: Reject a link shim found on the page
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that page links "https://l.instagram.com/?u=https%3A%2F%2Fifood.com.br%2Fx"
    When the website tier looks for a handle
    Then the website tier offers nothing

  Scenario: Reject a post link found on the page
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that page links "https://www.instagram.com/p/CxYzAbCdEfG/"
    When the website tier looks for a handle
    Then the website tier offers nothing

  Scenario: Yield nothing when the page links no Instagram
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that page links nothing
    When the website tier looks for a handle
    Then the website tier offers nothing

  Scenario: Survive a website that times out
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that website times out
    When the website tier looks for a handle
    Then the website tier offers nothing
    And the lookup does not raise

  Scenario: Survive a website that returns an enormous body
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that website returns a body larger than the cap
    When the website tier looks for a handle
    Then the website tier offers nothing
    And the lookup does not raise

  Scenario: Accept a footer link whose name matches the venue
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that page links "https://www.instagram.com/bucatrattoria/"
    When the cascade runs every free tier
    Then the cascade accepts a handle from the venue website
    And the paid search is never called

  Scenario: Reject a footer link that belongs to somebody else
    Given a venue named "The Fisherman"
    And the venue's listed website is "https://thefisherman.com.br"
    And that page links "https://www.instagram.com/smartfit/"
    When the cascade runs every free tier
    Then the cascade rejects every free-tier candidate

  Scenario: Consult the venue's own Google listing first
    Given the venue's Google listing already links "https://instagram.com/bucatrattoria"
    And the venue's listed website is "https://bucatrattoria.com.br"
    When the cascade runs every free tier
    Then the cascade accepts a handle from the Google listing

  Scenario: Honour the per-run toggle
    Given the venue's listed website is "https://bucatrattoria.com.br"
    And that page links "https://www.instagram.com/bucatrattoria/"
    And the website tier is turned off for this run
    When the cascade runs every free tier
    Then the cascade rejects every free-tier candidate
