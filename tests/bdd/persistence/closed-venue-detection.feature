@wip
Feature: Closed-venue detection excludes permanently closed venues from serving
  Venues whose reviewers report them as permanently closed must not be served as
  open, busy places. Detection reads the review evidence already stored in RDS,
  flags the venue, and the serving projection drops it — without touching the
  venue's lifecycle, so a reopened venue returns on its own.

  Background:
    Given closure detection is enabled
    And the venue "Bar do Fim" is active and servable

  Scenario: A venue whose newest review reports permanent closure is excluded
    Given the venue "Bar do Fim" has a review published "2026-01-12" saying "SO PRA AVISAR PRA QUEM NAO SABE. ESSE BAR FECHOU"
    And its remaining reviews were published before "2023-05-08"
    When closure detection runs
    Then the venue "Bar do Fim" is flagged closed with confidence "high"
    And the recorded evidence names the review published "2026-01-12"
    When the serving projection is rebuilt
    Then the venue "Bar do Fim" is absent from the serving projection

  Scenario: A closure phrase in an old review does not close a venue
    Given the venue "Bar do Fim" has a review published "2021-03-02" saying "esse bar fechou"
    And the venue "Bar do Fim" has a review published "2026-05-30" saying "cerveja gelada e atendimento otimo"
    When closure detection runs
    Then the venue "Bar do Fim" is not flagged closed
    And the venue "Bar do Fim" is present in the serving projection

  Scenario Outline: Temporary or speculative closure phrases must not flag a venue
    Given the venue "Bar do Fim" has a newest review saying "<text>"
    When closure detection runs
    Then the venue "Bar do Fim" is not flagged closed

    Examples:
      | text                                 |
      | fechado hoje, voltamos amanha        |
      | fechado para reforma ate dezembro    |
      | acho que vai fechar em breve         |

  Scenario: A reopened venue returns to serving without manual intervention
    Given the venue "Bar do Fim" is flagged closed with confidence "high"
    And the venue "Bar do Fim" is absent from the serving projection
    When a review published "2026-07-20" saying "reabriu, casa cheia no sabado" is added
    And closure detection runs
    And the serving projection is rebuilt
    Then the venue "Bar do Fim" is not flagged closed
    And the venue "Bar do Fim" is present in the serving projection

  Scenario: A low-confidence closure signal is recorded but does not change serving
    Given the venue "Bar do Fim" has review evidence that yields confidence "low"
    When closure detection runs
    Then the venue "Bar do Fim" is flagged closed with confidence "low"
    And the venue "Bar do Fim" is present in the serving projection

  Scenario Outline: Venues without orderable review evidence are never flagged
    Given the venue "Bar do Fim" has <evidence>
    When closure detection runs
    Then the venue "Bar do Fim" is not flagged closed
    And the venue "Bar do Fim" is present in the serving projection

    Examples:
      | evidence                                          |
      | no reviews                                        |
      | only reviews with no publish time                 |

  Scenario: Flagging a venue closed must not alter its lifecycle or stored data
    Given the venue "Bar do Fim" has a newest review saying "esse bar fechou"
    When closure detection runs
    Then the venue "Bar do Fim" is flagged closed with confidence "high"
    And the venue "Bar do Fim" still has lifecycle status "active"
    And the stored venue row and enrichment records for "Bar do Fim" are unchanged

  Scenario: Detection disabled by configuration flags and excludes nothing
    Given closure detection is disabled
    And the venue "Bar do Fim" has a newest review saying "esse bar fechou"
    When closure detection runs
    Then the venue "Bar do Fim" is not flagged closed
    And the venue "Bar do Fim" is present in the serving projection

  Scenario: A malformed review payload isolates to its own venue
    Given the venue "Bar Quebrado" has a malformed review payload
    And the venue "Bar do Fim" has a newest review saying "esse bar fechou"
    When closure detection runs
    Then the venue "Bar do Fim" is flagged closed with confidence "high"
    And the run summary reports at least one error naming "Bar Quebrado"

  Scenario: Excluded closed venues are visible to an operator
    Given the venue "Bar do Fim" is flagged closed with confidence "high"
    When an operator requests the closed-venue report
    Then the report lists "Bar do Fim" with its reason, confidence, evidence date and matched phrase
