Feature: Serve the Instagram handle with each venue inventory row

  As an operator reviewing the venue inventory
  I want each row to carry the venue's Instagram handle and how it was found
  So that I can judge a handle in the table instead of opening each venue or
  querying Redis by hand

  Background:
    Given the venue inventory endpoint is configured

  Scenario: Return an accepted handle with its confidence and tier
    Given the venue "Champagne Club" has an accepted Instagram handle "champagne_recifee" found by the "venue_website" tier with confidence 0.78
    When I list the venue inventory
    Then the row for "Champagne Club" reports the Instagram handle "champagne_recifee"
    And the row reports the Instagram url "https://instagram.com/champagne_recifee"
    And the row reports the Instagram status "found"
    And the row reports the Instagram confidence 0.78
    And the row reports the Instagram source "venue_website"

  Scenario: Distinguish a searched-and-found-nothing venue from an unsearched one
    Given the venue "Boteco Sem Rede" has an Instagram record with status "not_found"
    And the venue "Bar Nunca Olhado" has no Instagram record at all
    When I list the venue inventory
    Then the row for "Boteco Sem Rede" reports the Instagram status "not_found"
    And the row for "Boteco Sem Rede" reports a null Instagram handle
    And the row for "Bar Nunca Olhado" reports no Instagram object

  Scenario: Report a low-confidence handle as such
    Given the venue "Casa Duvidosa" has a low-confidence Instagram handle "casaduvidosa" with confidence 0.55
    When I list the venue inventory
    Then the row for "Casa Duvidosa" reports the Instagram status "low_confidence"
    And the row for "Casa Duvidosa" reports the Instagram handle "casaduvidosa"

  Scenario: Keep the existing coverage flag unchanged
    Given the venue "Champagne Club" has an accepted Instagram handle "champagne_recifee" found by the "venue_website" tier with confidence 0.78
    And the venue "Bar Nunca Olhado" has no Instagram record at all
    When I list the venue inventory
    Then the row for "Champagne Club" reports the instagram cache flag as true
    And the row for "Bar Nunca Olhado" reports the instagram cache flag as false

  Scenario: Leave every other inventory field untouched
    Given the venue "Champagne Club" has an accepted Instagram handle "champagne_recifee" found by the "venue_website" tier with confidence 0.78
    When I list the venue inventory
    Then the row for "Champagne Club" still reports its venue id, name, address, coordinates, lifecycle status and business status unchanged

  Scenario: Read the Instagram records in one bulk call for the page
    Given the inventory contains 25 venues with Instagram handles
    When I list the venue inventory
    Then the Instagram records are read once in bulk for the page
    And no per-venue Instagram read is issued

  Scenario: Survive a malformed stored record
    Given the venue "Bar Corrompido" has a malformed Instagram record
    And the venue "Champagne Club" has an accepted Instagram handle "champagne_recifee" found by the "venue_website" tier with confidence 0.78
    When I list the venue inventory
    Then the listing succeeds
    And the row for "Champagne Club" still reports its handle
    And the row for "Bar Corrompido" reports no usable Instagram handle

  Scenario: Tolerate a record stored before the source field existed
    Given the venue "Bar Antigo" has an Instagram handle "barantigo" with no recorded source
    When I list the venue inventory
    Then the row for "Bar Antigo" reports the Instagram handle "barantigo"
    And the row for "Bar Antigo" reports a null Instagram source
