@persistence
Feature: Soft-delete permanently closed venues without losing Redis troubleshooting data
  Operators must retain every venue record that cs-server has learned about,
  even when Google Places reports the venue as permanently closed. Deprecated
  venues must disappear from normal serving and enrichment work, but they must
  remain inspectable through the admin surface for troubleshooting and
  vibes_bot administration. Temporarily closed venues must remain active so
  cs-server can still fetch live busyness and show them to users when data is
  available.

  Background:
    Given Redis contains an active venue "venue_active" in the geo index
    And Redis contains an active venue "venue_closed" in the geo index
    And "venue_closed" has cached live forecast, weekly forecast, vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe profile records
    And Google Places closure handling is enabled

  Scenario: Permanently closed Google Places venues are soft-deleted
    Given Google Places details for "venue_closed" return business status "CLOSED_PERMANENTLY"
    When the Google Places enrichment job force-refreshes "venue_closed"
    Then "venue_closed" must still exist under its existing Redis venue key
    And "venue_closed" must still remain a member of the existing Redis geo index
    And "venue_closed" must be marked as deprecated with reason "google_places_closed_permanently"
    And the deprecated metadata must include source "google_places" and business status "CLOSED_PERMANENTLY"
    And the cached live forecast, weekly forecast, vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe profile records for "venue_closed" must not be deleted
    And the metric "venues_soft_deleted_total{reason=\"google_places_closed_permanently\",source=\"google_places\"}" must be incremented
    And the metric "venues_permanently_closed_removed_total" must not be incremented

  Scenario: Temporarily closed Google Places venues remain active
    Given Google Places details for "venue_closed" return business status "CLOSED_TEMPORARILY"
    And "venue_closed" has available cached live busyness
    When the Google Places enrichment job force-refreshes "venue_closed"
    Then "venue_closed" must still exist under its existing Redis venue key
    And "venue_closed" must still remain a member of the existing Redis geo index
    And "venue_closed" must not be marked as deprecated
    And "venue_closed" must remain eligible for future live forecast refreshes
    And the cached live forecast, weekly forecast, vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe profile records for "venue_closed" must not be deleted
    And the public nearby response must include "venue_closed" when live busyness is available
    And the metric "venues_soft_deleted_total{reason=\"google_places_closed_temporarily\",source=\"google_places\"}" must not be incremented
    And the metric "venues_temporarily_closed_removed_total" must not be incremented

  Scenario: Deprecated venues are hidden from public nearby results
    Given "venue_closed" is already marked as deprecated
    When a client requests venues nearby a point that includes "venue_active" and "venue_closed"
    Then the public nearby response must include "venue_active"
    And the public nearby response must not include "venue_closed"
    And the Redis record for "venue_closed" must remain available for direct admin lookup

  Scenario: Enrichment and refresh jobs skip deprecated venues
    Given "venue_closed" is already marked as deprecated
    When live forecast refresh, weekly forecast refresh, Google Places enrichment, photo enrichment, Instagram discovery, Instagram posts, menu photo enrichment, menu extraction, and vibe classification jobs run
    Then those jobs must process "venue_active"
    And those jobs must not call external enrichment or refresh clients for "venue_closed"
    And those jobs must log how many deprecated venues were skipped

  Scenario: Admin inventory exposes deprecated venues for vibes_bot
    Given "venue_closed" is already marked as deprecated
    When the admin client requests the venue inventory with status "deprecated"
    Then the response must include "venue_closed"
    And the response item must include venue id, name, address, latitude, longitude, lifecycle status, deprecated reason, deprecated source, deprecated timestamp, and Google business status
    And the response item must include cache flags for live forecast, weekly forecast, vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe profile
    And the response must include separate active and deprecated venue counts

  Scenario: Redis deployment keeps legacy records active without a reset
    Given Redis already contains legacy venue records with no lifecycle metadata
    When cs-server starts after the soft-delete feature is deployed
    Then legacy venue records must be treated as active
    And cs-server must not flush Redis
    And cs-server must not rename or rebuild the existing venue geo key
    And cs-server must not require a backfill migration before serving existing venues

  Scenario: Redis upserts do not reactivate deprecated venues
    Given "venue_closed" is already marked as deprecated
    When inventory sync or discovery refresh upserts a venue with id "venue_closed"
    Then the existing deprecated lifecycle metadata for "venue_closed" must be preserved
    And "venue_closed" must remain hidden from public nearby results
    And "venue_closed" must remain visible through the admin deprecated inventory
