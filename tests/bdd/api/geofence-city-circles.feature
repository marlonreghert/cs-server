Feature: Geo-fence as capital-city circles inside serving eligibility
  The geo restriction must be a set of state-capital circles (capital +
  radius km) managed through the admin geofence contract, enforced only at
  the serving.eligible_venue view level, fail-open, and never soft-deleting.

  Background:
    Given the admin geo-fence is enabled with the city "recife" at radius 40 km

  Scenario: Capitals catalog lists every Brazilian state capital
    When the admin requests the geo-fence capitals catalog
    Then the response lists 27 capitals sorted by name
    And every capital has a unique slug, a name, and coordinates within valid ranges

  Scenario: Reading the fence returns the enabled flag and resolved circles
    When the admin reads the geo-fence config
    Then the response has enabled true
    And the response lists one city with slug "recife", its catalog coordinates, and radius_km 40

  Scenario: Replacing the city list resolves coordinates and mirrors to Redis
    When the admin writes a geo-fence with cities "recife" at 30 km and "salvador" at 25 km
    Then the response lists both cities with their catalog coordinates
    And reading the geo-fence config returns the same two circles
    And the Redis geo-fence mirror holds the same two circles

  Scenario: An unknown capital slug is rejected and the fence is unchanged
    When the admin writes a geo-fence with the city "caruaru" at 30 km
    Then the write is rejected as invalid
    And reading the geo-fence config still returns the city "recife" at radius_km 40

  Scenario: A duplicate capital slug is rejected
    When the admin writes a geo-fence listing the city "recife" twice
    Then the write is rejected as invalid

  Scenario Outline: An out-of-range radius is rejected
    When the admin writes a geo-fence with the city "recife" at <radius> km
    Then the write is rejected as invalid
    And reading the geo-fence config still returns the city "recife" at radius_km 40

    Examples:
      | radius |
      | 0      |
      | 201    |

  Scenario: Enabling the fence with no cities is rejected
    When the admin writes an enabled geo-fence with no cities
    Then the write is rejected as invalid

  Scenario: Disabling the fence with no cities is accepted
    When the admin writes a disabled geo-fence with no cities
    Then the write succeeds
    And reading the geo-fence config returns enabled false

  Scenario: A legacy bounding-box payload is rejected with the new shape named
    When the admin writes a geo-fence using min/max lat/lng box fields
    Then the write is rejected as invalid
    And the rejection message names the cities-based payload

  Scenario: A venue inside one of the configured circles is served
    Given the fence has cities "recife" at 30 km and "salvador" at 25 km
    And an active venue with coordinates 10 km from the "salvador" center
    When the serving projection runs
    Then the venue is included in the serving set

  Scenario: A venue outside every configured circle is excluded from serving
    Given the fence has the city "recife" at 30 km
    And an active venue with coordinates 100 km from the "recife" center
    When the serving projection runs
    Then the venue is excluded from the serving set
    And the venue remains active and is not soft-deleted

  Scenario: A venue without coordinates is always served
    Given an active venue with no stored coordinates
    When the serving projection runs
    Then the venue is included in the serving set

  Scenario: Disabling the fence re-serves previously geo-excluded venues
    Given the fence has the city "recife" at 30 km
    And an active venue with coordinates 100 km from the "recife" center
    When the admin disables the geo-fence
    And the serving projection runs
    Then the venue is included in the serving set

  Scenario: The fence reports how many active venues sit outside its circles
    Given the fence has the city "recife" at 30 km
    And an active venue with coordinates 100 km from the "recife" center
    And an active venue with coordinates 10 km from the "recife" center
    When the admin reads the geo-fence config
    Then the response reports 1 venue outside the circles

  Scenario: The outside-circles count is reported even while the fence is off
    Given the fence has the city "recife" at 30 km
    And an active venue with coordinates 100 km from the "recife" center
    When the admin disables the geo-fence
    And the admin reads the geo-fence config
    Then the response has enabled false
    And the response reports 1 venue outside the circles
