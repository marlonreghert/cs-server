Feature: Find a venue's Instagram handle from the cheapest source that has it
  As the venue platform
  I must try the sources I already paid for before buying a search, verify that
  the handle I found is a real profile belonging to this venue, and record how I
  decided,
  so the catalog gains Instagram handles without spending on venues whose handle
  was already sitting in data we own.

  Background:
    Given the Instagram handle cascade is enabled
    And a venue that has no Instagram handle yet

  # 1 — Cheapest source first
  Scenario: Take the handle from the venue's own Google website
    Given the venue's Google listing has the website "https://instagram.com/barvibes"
    When the cascade runs for that venue
    Then the handle "barvibes" is accepted
    And the handle is recorded with the source "google_website"
    And no paid search is performed

  Scenario: Take the handle from the archived Google Maps payload
    Given the venue's Google listing has no website
    And the archived Google Maps payload has the website "https://instagram.com/barvibes"
    When the cascade runs for that venue
    Then the handle "barvibes" is accepted
    And the handle is recorded with the source "archived_gmaps_website"
    And no paid search is performed

  Scenario: Fall through to the paid search only when both free sources are empty
    Given the venue's Google listing has no website
    And the venue has no archived Google Maps payload
    And the paid search returns a strong candidate
    When the cascade runs for that venue
    Then the paid search is performed exactly once
    And the handle is recorded with the source "apify_search"

  Scenario: Stop at the first source that clears the high-confidence bar
    Given the venue's Google listing has the website "https://instagram.com/barvibes"
    And the archived Google Maps payload has a different Instagram website
    When the cascade runs for that venue
    Then only the first source is consulted
    And the handle "barvibes" is accepted

  Scenario: Skip a venue whose handle is still fresh
    Given the venue already has a recently checked handle
    When the cascade runs for that venue
    Then no source is consulted at all
    And no paid search is performed

  # 2 — Extraction must not invent handles
  Scenario: Reject an Instagram outbound link wrapper
    Given the venue's Google listing has the website "https://l.instagram.com/?u=https%3A%2F%2Fwww.ifood.com.br%2Fdelivery"
    When the cascade runs for that venue
    Then no handle is extracted from that website
    And the rejection is counted with the reason "link_shim"

  Scenario Outline: Reject Instagram URLs that are not profiles
    Given the venue's Google listing has the website "<url>"
    When the cascade runs for that venue
    Then no handle is extracted from that website
    And the rejection is counted with the reason "non_profile_path"

    Examples:
      | url                                        |
      | https://instagram.com/p/Cabc123            |
      | https://instagram.com/reel/Cxyz789         |
      | https://instagram.com/explore/tags/recife  |

  Scenario: Strip tracking parameters from a valid profile URL
    Given the venue's Google listing has the website "https://instagram.com/barvibes?igshid=abc123"
    When the cascade runs for that venue
    Then the handle "barvibes" is accepted

  # 3 — Verify the profile actually exists
  Scenario: Confirm a profile from its public metadata
    Given the venue's Google listing has the website "https://instagram.com/barvibes"
    And the profile "barvibes" publishes the display name "Bar Vibes"
    When the cascade runs for that venue
    Then the profile is confirmed to exist
    And the recorded evidence names the display name "Bar Vibes"

  Scenario: Treat a handle with no profile metadata as not found
    Given the paid search returns the candidate "ghosthandle"
    And the profile "ghosthandle" publishes no profile metadata
    When the cascade runs for that venue
    Then the handle is not accepted
    And the venue is recorded as not found

  Scenario: Treat a failed lookup as unknown rather than as proof of absence
    Given the venue's Google listing has the website "https://instagram.com/barvibes"
    And looking up the profile "barvibes" fails
    When the cascade runs for that venue
    Then the profile existence is recorded as unknown
    And the handle is not rejected merely because the lookup failed

  # 4 — Deciding when the signals disagree
  Scenario: Accept a strong name match without consulting the judge
    Given the venue is named "Bar Vibes"
    And the paid search returns the candidate "barvibes" with the display name "Bar Vibes"
    When the cascade runs for that venue
    Then the handle is accepted
    And the judge is not consulted

  Scenario: Consult the judge when the name match is ambiguous
    Given the venue is named "Bercy Boa Viagem"
    And the paid search returns the candidate "bercyvillage" with the display name "Bercy Village"
    When the cascade runs for that venue
    Then the judge is consulted
    And the judge's verdict decides the outcome

  # 5 — The judge must work with whatever it has, including nothing
  Scenario: Judge with both the profile picture and the venue's photos
    Given the candidate is ambiguous
    And the profile publishes a usable profile picture
    And the venue has archived photos
    When the judge is consulted
    Then the judge runs in the mode "vision_both"
    And a verdict is returned

  Scenario: Judge when the venue has no archived photos
    Given the candidate is ambiguous
    And the profile publishes a usable profile picture
    And the venue has no archived photos
    When the judge is consulted
    Then the judge runs in the mode "vision_partial"
    And a verdict is returned

  Scenario: Judge when the profile picture is unusable
    Given the candidate is ambiguous
    And the profile publishes no usable profile picture
    And the venue has archived photos
    When the judge is consulted
    Then the judge runs in the mode "vision_partial"
    And a verdict is returned

  Scenario: Judge on text alone when no images exist on either side
    Given the candidate is ambiguous
    And the profile publishes no usable profile picture
    And the venue has no archived photos
    When the judge is consulted
    Then the judge runs in the mode "text_only"
    And a verdict is returned
    And the recorded confidence does not exceed the text-only ceiling

  Scenario: Fall back to the cheap signals when the judge is unavailable
    Given the candidate is ambiguous
    And the judge is not configured
    When the cascade runs for that venue
    Then the outcome is decided by the cheap signals alone
    And the judge is recorded as unavailable
    And the venue is not failed

  # 6 — Degrading instead of failing
  Scenario: Continue the cascade when one source raises
    Given the archived payload source raises an error
    And the paid search returns a strong candidate
    When the cascade runs for that venue
    Then the failing source is counted as an error
    And the cascade still reaches the paid search
    And the cascade run completes successfully

  Scenario: Skip the archived source when the archive cannot be read
    Given reading the archived payload is not permitted
    When the cascade runs for that venue
    Then the archived source is reported as unavailable
    And the cascade continues to the remaining sources

  Scenario: Run the whole catalog with the paid source disabled
    Given the paid source is disabled
    When the cascade runs for every venue needing a handle
    Then no paid search is performed
    And the handles found come only from the free sources

  # 7 — Knowing what it did and what it cost
  Scenario: Record the evidence behind an accepted handle
    When a handle is accepted
    Then the stored record names the source it came from
    And the stored record carries the confidence and the signals behind it
    And the source is queryable without reading the payload

  Scenario: Expose the cost split as metrics
    When the cascade has run over several venues
    Then the attempts are exposed per source
    And the number of paid calls is exposed
    And the handle rejections are exposed by reason
