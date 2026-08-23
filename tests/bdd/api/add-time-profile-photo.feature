@wip
Feature: Capture a venue's Instagram profile photo when the venue is added

  As an operator adding a venue to the catalog
  I want the venue's Instagram profile photo captured at add time
  So that the venue shows a real avatar immediately instead of an emoji
  placeholder for up to a day, without re-buying a photo the scheduled job
  already tried and failed to get, without leaving an orphaned upload behind
  on a slow request, and without spending on a venue nobody will ever serve

  Background:
    Given the add-venue endpoint is configured
    And add-time profile-photo capture is enabled and configured

  Scenario: Capture and store the profile photo when a venue is added
    Given BestTime creates the venue "Bar do Retrato" successfully
    And the venue's Instagram profile has a picture
    When I add the venue "Bar do Retrato"
    Then the response status is 201
    And the response "profile_photo.status" is "stored"
    And the venue's profile photo is persisted

  Scenario: Record a negative-cache attempt when add-time capture finds no picture
    Given BestTime creates the venue "Bar Sem Retrato" successfully
    And the venue's Instagram profile has no picture
    When I add the venue "Bar Sem Retrato"
    Then the response status is 201
    And the response "profile_photo.status" is "no_pic"
    And a profile-photo attempt is recorded for the venue

  Scenario: A slow media upload never times out or orphans the photo
    Given BestTime creates the venue "Bar Upload Lento" successfully
    And the venue's Instagram profile has a picture
    And the add-time profile-photo deadline is very short
    And the media upload is slower than that deadline
    When I add the venue "Bar Upload Lento"
    Then the response status is 201
    And the response "profile_photo.status" is "stored"
    And the venue's profile photo is persisted

  Scenario: Create the venue when photo capture exceeds its deadline fetching from Apify
    Given BestTime creates the venue "Bar Apify Lento" successfully
    And the venue's Instagram profile has a picture
    And the add-time profile-photo deadline is very short
    And the Apify profile fetch is slower than that deadline
    When I add the venue "Bar Apify Lento"
    Then the response status is 201
    And the venue is persisted in the catalog
    And the response "profile_photo.status" is "timeout"
    And a profile-photo attempt is recorded for the venue

  Scenario: Skip add-time capture for a venue blocked by the eligibility gate
    Given BestTime creates the venue "Farmácia Popular do Recife" successfully
    And the venue's Instagram profile has a picture
    When I add the venue "Farmácia Popular do Recife"
    Then the response status is 201
    And the response "profile_photo.status" is "skipped_ineligible"
    And no Apify profile fetch is attempted

  Scenario: Capture the profile photo for a newly geo-linked venue
    Given BestTime rejects the address for "Empório Retratado"
    And the geo fallback newly links the venue to a BestTime venue
    And the venue's Instagram profile has a picture
    When I add the venue "Empório Retratado"
    Then the response "status" is "matched_via_geo_fallback"
    And the response "newly_linked" is true
    And the response "profile_photo.status" is "stored"

  Scenario: Create the venue when photo capture raises an error
    Given BestTime creates the venue "Bar Quebrado de Foto" successfully
    And add-time photo capture raises an error
    When I add the venue "Bar Quebrado de Foto"
    Then the response status is 201
    And the venue is persisted in the catalog
    And the response "profile_photo.status" is "error"

  Scenario: Create the venue when the profile-photo service is not configured
    Given the profile-photo capture service is not configured
    And BestTime creates the venue "Bar Sem Servico De Foto" successfully
    When I add the venue "Bar Sem Servico De Foto"
    Then the response status is 201
    And the venue is persisted in the catalog
    And the response "profile_photo.status" is "skipped"
