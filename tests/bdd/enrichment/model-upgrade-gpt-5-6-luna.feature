Feature: Model upgrade to gpt-5.6-luna
  As the operator of the enrichment pipelines
  I want every OpenAI path to run the new model without sending it a parameter
  it rejects
  So that the upgrade does not 400 every call, and a path pinned back to the old
  model behaves exactly as it does today

  Scenario: Omit temperature for a model that pins sampling
    Given the target model is "gpt-5.6-luna"
    When a classification request is built with temperature 0.1
    Then the request carries no temperature

  Scenario: Pass temperature through for a model that accepts it
    Given the target model is "gpt-5.4-nano"
    When a classification request is built with temperature 0.1
    Then the request carries temperature 0.1

  Scenario: Preserve a zero temperature for a model that accepts it
    Given the target model is "gpt-5.4-mini"
    When a judge request is built with temperature 0
    Then the request carries temperature 0

  Scenario: Pass temperature through for an unrecognised model
    Given the target model is an unrecognised model name
    When a classification request is built with temperature 0.1
    Then the request carries temperature 0.1

  Scenario: Return a verdict for every photo in a batch of ten
    Given the target model is "gpt-5.6-luna"
    And a batch of 10 photos is classified
    Then 10 verdicts are returned
    And no verdict falls back for a missing response

  Scenario: Surface a rejected parameter as a clear failure
    Given the model rejects a request parameter
    When a classification request is made
    Then the failure is reported with the rejected parameter
    And the request is not silently retried

  Scenario: Count reasoning tokens in the token metric
    Given the target model reports reasoning tokens in its usage
    When a classification request completes
    Then the reasoning tokens are counted in the token metric

  Scenario: Keep the judge verdict schema unchanged
    Given the target model is "gpt-5.6-luna"
    When the Instagram judge adjudicates a candidate
    Then the verdict carries the same fields as before the upgrade
