@wip
Feature: Match venue names the way venues name themselves
  As the operator of the Instagram handle pipeline
  I want a name check that tolerates prefixes, suffixes and accents
  So that correct handles are not rejected for cosmetic differences

  Scenario: Match through a category prefix
    Given a venue called "Pizzaria Atlântico Graças"
    And a candidate handle "pizzariaatlantico"
    Then the names are considered a match

  Scenario: Match through a neighbourhood suffix
    Given a venue called "Camarada Camarão RioMar Recife"
    And a candidate handle "camaradacamarao"
    Then the names are considered a match

  Scenario: Match through both a prefix and a suffix
    Given a venue called "Bode do Nô Boa Viagem - Restaurante"
    And a candidate handle "bodedono"
    Then the names are considered a match

  Scenario: Match when the handle drops the accents
    Given a venue called "Armazém Guimarães RioMar Recife"
    And a candidate handle "armazemguimaraes"
    Then the names are considered a match

  Scenario: Match when the handle appends a marketing suffix
    Given a venue called "Restaurante Parraxaxá Boa Viagem"
    And a candidate handle "parraxaxaoficial"
    Then the names are considered a match

  Scenario: Reject the agency that built the venue's website
    Given a venue called "Ordinário Bar e Música"
    And a candidate handle "marketingpararestaurante"
    Then the names are not considered a match

  Scenario: Reject an unrelated business linked from the page
    Given a venue called "Lower Deck Bar & Nightclub"
    And a candidate handle "parkelanzacbe"
    Then the names are not considered a match

  Scenario: Never score a pair lower than a plain comparison
    Given a venue called "Ponte Nova"
    And a candidate handle "ponte_nova"
    Then the score is at least the plain comparison score
