@wip
Feature: The Instagram probe fails open
  As the operator of the Instagram handle pipeline
  I want a probe that cannot see an answer to say so
  So that being blocked is never mistaken for a venue having no Instagram

  Scenario: Report a real profile as present
    Given Instagram serves a genuine profile page
    When the profile is probed
    Then the probe reports the profile as present
    And the probe reports the display name from the page

  Scenario: Report a login wall as unknown
    Given Instagram serves its login wall instead of the profile
    When the profile is probed
    Then the probe reports the profile existence as unknown

  Scenario: Report a challenge page as unknown
    Given Instagram serves a challenge page
    When the profile is probed
    Then the probe reports the profile existence as unknown

  Scenario: Report a redirect away from the profile as unknown
    Given Instagram redirects the request to its login page
    When the profile is probed
    Then the probe reports the profile existence as unknown

  Scenario: Report an empty response as unknown
    Given Instagram serves an empty response
    When the profile is probed
    Then the probe reports the profile existence as unknown

  Scenario: Report a genuine missing profile as absent
    Given Instagram serves its page for a handle that has no profile
    When the profile is probed
    Then the probe reports the profile as absent

  Scenario: Count a blocked probe apart from an absent one
    Given Instagram serves its login wall instead of the profile
    When the profile is probed
    Then a blocked probe is counted

  Scenario: Keep a candidate the probe could not verify
    Given the venue's Google listing links an Instagram profile
    And the probe cannot verify whether that profile exists
    When the cascade discovers the venue's handle
    Then the cascade accepts the handle
    And the stored record carries the handle

  Scenario: Discard a candidate only when the profile is confirmed absent
    Given the venue's Google listing links an Instagram profile
    And the probe confirms that profile does not exist
    When the cascade discovers the venue's handle
    Then the cascade does not accept a handle
