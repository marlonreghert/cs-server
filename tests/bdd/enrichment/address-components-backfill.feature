Feature: Address components backfill — Google authoritative, parser fallback
  As the events UI and the venue catalog
  I want every venue's bairro filled by Google when possible and by a data-derived
  text parser otherwise, with a recorded source per field
  So that the card shows a real neighborhood, a later authoritative answer can
  still correct an earlier guess, and a guess can never block or undo a better one

  Scenario: Google's answer fills the bairro when nothing is stored yet
    Given "Casa Bacurau" is a venue with a Google place id and no stored address components
    And Geocoding returns address components with sublocality level 1 "Santo Amaro"
    When the address backfill processes "Casa Bacurau"
    Then the stored address neighborhood is "Santo Amaro" with source "google"

  Scenario: The parser fills the bairro from a space-joined address with no Google match
    Given "Boteco da Maré" is a venue with no Google place id
    And its raw address text is "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil"
    And "Niterói" is in the approved city vocabulary
    When the address backfill processes "Boteco da Maré"
    Then the stored address neighborhood is "Santa Rosa" with source "parsed"
    And the stored address city is "Niterói" with source "parsed"

  Scenario: The parser splits a comma-delimited address for a city not yet in the vocabulary
    Given "Padaria Casa Caiada" is a venue with no Google place id
    And its raw address text is "R. Carmelita Muniz de Araújo, 225 - Casa Caiada, Olinda - PE, 53130-645, Brazil"
    When the address backfill processes "Padaria Casa Caiada"
    Then the stored address neighborhood is "Casa Caiada" with source "parsed"
    And the stored address city is "Olinda" with source "parsed"

  Scenario: Google's answer overwrites a previously parsed bairro
    Given "Boteco da Maré" has a stored address neighborhood of "Santa Rosa" with source "parsed"
    And "Boteco da Maré" has a Google place id
    And Geocoding returns address components with sublocality level 1 "Santa Rosa Baixa"
    When the address backfill processes "Boteco da Maré"
    Then the stored address neighborhood is "Santa Rosa Baixa" with source "google"

  Scenario: A parsed answer is refused over an already Google-sourced bairro
    Given "Casa Bacurau" has a stored address neighborhood of "Santo Amaro" with source "google"
    And "Casa Bacurau" has no Google place id this run
    And its raw address text is "R. Abdon Batista, 300 - Santo Amaro Recife - PE 50100-460 Brazil"
    When the address backfill processes "Casa Bacurau"
    Then the stored address neighborhood is still "Santo Amaro" with source "google"

  Scenario: An operator-entered bairro is never overwritten by Google or the parser
    Given "Restaurante do Porto" has a stored address neighborhood of "Recife Antigo" with source "operator"
    And "Restaurante do Porto" has a Google place id
    And Geocoding returns address components with sublocality level 1 "Bairro do Recife"
    When the address backfill processes "Restaurante do Porto"
    Then the stored address neighborhood is still "Recife Antigo" with source "operator"

  Scenario: The parser leaves the bairro unset rather than guess at no recognizable city
    Given "Quiosque da Praia" is a venue with no Google place id
    And its raw address text is "COHAB Recife - PE 51340-670 Brazil"
    When the address backfill processes "Quiosque da Praia"
    Then the stored address city is "Recife" with source "parsed"
    And the stored address neighborhood is still unset

  Scenario: The parser accepts a plausible multi-word bairro with no street before it
    Given "Casa do Jardim" is a venue with no Google place id
    And its raw address text is "Jardim Santa Maria São Paulo - SP 04120-000 Brazil"
    And "São Paulo" is in the approved city vocabulary
    When the address backfill processes "Casa do Jardim"
    Then the stored address neighborhood is "Jardim Santa Maria" with source "parsed"
    And the stored address city is "São Paulo" with source "parsed"

  Scenario: The parser picks the true trailing city over a look-alike name inside the bairro
    Given "Empório Boa Vista" is a venue with no Google place id
    And its raw address text is "Av. Norte, 900 - Boa Vista Recife - PE 50070-100 Brazil"
    And "Recife" is in the approved city vocabulary
    When the address backfill processes "Empório Boa Vista"
    Then the stored address neighborhood is "Boa Vista" with source "parsed"
    And the stored address city is "Recife" with source "parsed"

  Scenario: An ambiguous city match is accepted only when the venue's coordinates corroborate it
    Given "Sítio Boa Vista" is a venue with no Google place id, located near Boa Vista, Roraima
    And its raw address text is "Estr. do Sítio, s/n - Boa Vista - RR 69300-000 Brazil"
    And "Boa Vista" is in the approved city vocabulary and flagged ambiguous
    When the address backfill processes "Sítio Boa Vista"
    Then the stored address city is "Boa Vista" with source "parsed"

  Scenario: An ambiguous city match is rejected when the venue's coordinates do not corroborate it
    Given "Empório Boa Vista" is a venue with no Google place id, located in metropolitan Recife
    And its raw address text is "Av. Norte, 900 - Boa Vista - PE 50070-100 Brazil"
    And "Boa Vista" is in the approved city vocabulary and flagged ambiguous
    When the address backfill processes "Empório Boa Vista"
    Then the stored address city is still unset
    And the stored address neighborhood is still unset

  Scenario: A newly added venue with no Google match still gets a bairro automatically
    Given a new venue "Café Novo" is added with raw address text "R. Nova, 10 - Boa Viagem Recife - PE 51020-000 Brazil"
    And Google Places finds no match for "Café Novo"
    When "Café Novo" finishes its add-time enrichment
    Then the stored address neighborhood is "Boa Viagem" with source "parsed"
    And no manual backfill trigger was required

  Scenario: The backfill processes a bounded batch and resumes from where it left off
    Given 5 venues are waiting to be backfilled, a mix of servable and non-servable
    When the address backfill runs with a batch limit of 2
    Then exactly 2 venues are processed and the backfill cursor is saved
    When the address backfill runs repeatedly with a batch limit of 2 until none remain
    Then all 5 venues have been processed exactly once, servable and non-servable alike

  Scenario: Re-running the backfill over already-filled venues writes nothing new
    Given "Casa Bacurau" has a stored address neighborhood of "Santo Amaro" with source "google"
    When the address backfill runs over "Casa Bacurau" a second time with an unchanged Google response
    Then the address components outcome "unchanged" is recorded
    And the stored address neighborhood is still "Santo Amaro" with source "google"

  Scenario: The backfill makes no Geocoding calls while the free-tier switch is off
    Given the address backfill geocoding switch is off
    And "Boteco da Maré" is a venue with a Google place id and no stored address components
    And its raw address text is "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil"
    And "Niterói" is in the approved city vocabulary
    When the address backfill processes "Boteco da Maré"
    Then no Geocoding request was made
    And the stored address neighborhood is "Santa Rosa" with source "parsed"
