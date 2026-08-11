Feature: Attach a venue's Instagram handle when the venue is added

  As an operator adding a venue to the catalog
  I want the venue's Instagram handle discovered at add time
  So that the venue reaches serving with its handle instead of waiting for a
  cascade run that nothing schedules, and so that I am told when no handle was
  found rather than having to check another tool

  Background:
    Given the add-venue endpoint is configured
    And Instagram discovery at add time is enabled
    And the Instagram cascade is configured with its free sources, the Google-search tier, and the judge

  Scenario: Attach a handle found on the venue's Google listing
    Given BestTime creates the venue "Bar do Cuscuz" successfully
    And the venue's Google listing website is "https://instagram.com/bardocuscuz"
    When I add the venue "Bar do Cuscuz"
    Then the response status is 201
    And the response "status" is "created"
    And the response "instagram.status" is "found"
    And the response "instagram.handle" is "bardocuscuz"
    And the response "instagram.url" is "https://instagram.com/bardocuscuz"
    And the response "instagram.source" is "google_website"
    And the venue's Instagram handle is persisted as "bardocuscuz"

  Scenario: Create the venue anyway when no source knows its Instagram
    Given BestTime creates the venue "Boteco Sem Rede" successfully
    And no Instagram source has a handle for the venue
    When I add the venue "Boteco Sem Rede"
    Then the response status is 201
    And the response "status" is "created"
    And the venue is persisted in the catalog
    And the response "instagram.status" is "not_found"
    And the response "instagram.handle" is null

  Scenario: Accept a Google-search handle the judge confirms
    Given BestTime creates the venue "Casa Rosa" successfully
    And no free Instagram source has a handle for the venue
    And the Google-search tier returns "https://instagram.com/casarosarecife"
    And the judge confirms the candidate matches the venue
    When I add the venue "Casa Rosa"
    Then the response status is 201
    And the response "instagram.status" is "found"
    And the response "instagram.handle" is "casarosarecife"
    And the response "instagram.source" is "google_search"

  Scenario: Do not attach a Google-search handle the judge rejects
    Given BestTime creates the venue "Praca do Arsenal" successfully
    And no free Instagram source has a handle for the venue
    And the Google-search tier returns "https://instagram.com/mosteirosaobento"
    And the judge rejects the candidate as not matching the venue
    When I add the venue "Praca do Arsenal"
    Then the response status is 201
    And the venue is persisted in the catalog
    And the response "instagram.status" is not "found"
    And the venue has no Instagram handle attached

  Scenario: Never spend the paid Instagram user search at add time
    Given BestTime creates the venue "Quiosque da Praia" successfully
    And no Instagram source has a handle for the venue
    When I add the venue "Quiosque da Praia"
    Then the response status is 201
    And the cascade attempts the "google_website" source
    And the cascade attempts the "venue_website" source
    And the cascade attempts the "google_search" source
    And the cascade does not attempt the "apify_search" source

  Scenario: Leave the venue eligible for the paid tier after an add-time miss
    Given BestTime creates the venue "Bar Escondido" successfully
    And no Instagram source has a handle for the venue
    When I add the venue "Bar Escondido"
    Then the response "instagram.status" is "not_found"
    And no not-found result is cached for the venue
    And a later full cascade run for the venue consults its sources again

  Scenario: Create the venue when discovery exceeds its deadline
    Given BestTime creates the venue "Terraco Lento" successfully
    And Instagram discovery takes longer than its add-time deadline
    When I add the venue "Terraco Lento"
    Then the response status is 201
    And the venue is persisted in the catalog
    And the response "instagram.status" is "timeout"
    And the response "instagram.handle" is null

  Scenario: Create the venue when discovery raises
    Given BestTime creates the venue "Bar Quebrado" successfully
    And Instagram discovery raises an error
    When I add the venue "Bar Quebrado"
    Then the response status is 201
    And the venue is persisted in the catalog
    And the response "instagram.status" is "error"

  Scenario: Create the venue when the cascade is not configured
    Given the Instagram cascade is not configured
    And BestTime creates the venue "Bar Sem Cascata" successfully
    When I add the venue "Bar Sem Cascata"
    Then the response status is 201
    And the venue is persisted in the catalog
    And the response "instagram.status" is "skipped"

  Scenario: Skip discovery for a venue we already hold
    Given the venue "Bar Repetido" is already in the catalog
    When I add the venue "Bar Repetido"
    Then the response status is 200
    And the response "status" is "already_exists"
    And the response has no "instagram" field
    And no Instagram discovery is attempted

  Scenario: Discover for a newly linked geo-fallback venue
    Given BestTime rejects the address for "Empório Central"
    And the geo fallback newly links the venue to a BestTime venue
    And the venue's Google listing website is "https://instagram.com/emporiocentral"
    When I add the venue "Empório Central"
    Then the response "status" is "matched_via_geo_fallback"
    And the response "newly_linked" is true
    And the response "instagram.status" is "found"
    And the response "instagram.handle" is "emporiocentral"

  Scenario: Skip discovery for a geo-fallback link to a venue we already hold
    Given BestTime rejects the address for "Empório Antigo"
    And the geo fallback links the venue to a venue already in the catalog
    When I add the venue "Empório Antigo"
    Then the response "status" is "matched_via_geo_fallback"
    And the response "newly_linked" is false
    And the response has no "instagram" field
    And no Instagram discovery is attempted

  Scenario: Report each row's Instagram outcome in a batch add
    Given a batch add job for the venues:
      | venue_name       | instagram_available |
      | Bar Com Insta    | yes                 |
      | Bar Sem Insta    | no                  |
    When the batch job completes
    Then the row for "Bar Com Insta" reports outcome "created"
    And the row for "Bar Com Insta" reports "instagram.status" as "found"
    And the row for "Bar Sem Insta" reports outcome "created"
    And the row for "Bar Sem Insta" reports "instagram.status" as "not_found"

  Scenario: Keep a zero-cost operator run free of paid calls
    Given an operator triggers a cascade run with the paid tier disabled
    When the run completes
    Then the cascade does not attempt the "apify_search" source
    And the cascade does not attempt the "google_search" source
