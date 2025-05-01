// api/besttime/besttime_api_client_mock.go
package besttime

import (
    "fmt"

    "cs-server/config"
    "cs-server/models"
    "cs-server/models/venue"
	"cs-server/models/live_forecast"
    "cs-server/util"
)

// BestTimeApiClientMock provides mocked responses from JSON fixtures.
type BestTimeApiClientMock struct{}

// NewBestTimeApiClientMock creates a new instance of the mock client.
func NewBestTimeApiClientMock() *BestTimeApiClientMock {
    return &BestTimeApiClientMock{}
}

// GetVenuesNearby reads a SearchVenuesResponse JSON fixture.
func (c *BestTimeApiClientMock) GetVenuesNearby(lat float64, lng float64) (*models.SearchVenuesResponse, error) {
    path := config.GetResourcePath(config.SEARCH_VENUE_RESPONSE_RESOURCE)
    resp, err := util.ReadSearchVenuesResponseFromJSON(path)
    if err != nil {
        fmt.Println("Could not read SearchVenuesResponse JSON:", err)
        return nil, err
    }
    return resp, nil
}

// GetVenue reads a Venue JSON fixture.
func (c *BestTimeApiClientMock) GetVenue(venueID string) (*venue.Venue, error) {
    path := config.GetResourcePath(config.VENUE_STATIC_RESOURCE)
    resp, err := util.ReadVenueFromJSON(path)
    if err != nil {
        fmt.Println("Could not read Venue JSON:", err)
        return nil, err
    }
    return resp, nil
}

// SetCredentials is a no-op for the mock.
func (c *BestTimeApiClientMock) SetCredentials(apiKeyPublic, apiKeyPrivate string) {}

// GetVenueSearchProgress reads a SearchProgressResponse JSON fixture.
func (c *BestTimeApiClientMock) GetVenueSearchProgress(jobID, collectionID string) (*models.SearchProgressResponse, error) {
    path := config.GetResourcePath(config.SEARCH_PROGRESS_RESPONSE_RESOURCE)
    resp, err := util.ReadSearchProgressResponseFromJSON(path)
    if err != nil {
        fmt.Println("Could not read SearchProgressResponse JSON:", err)
        return nil, err
    }
    return resp, nil
}


func (c *BestTimeApiClientMock) GetLiveForecast(
    venueID, venueName, venueAddress string,
) (*live_forecast.LiveForecastResponse, error) {

	return nil, nil
}