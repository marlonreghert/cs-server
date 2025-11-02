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

// GetLiveForecast reads a LiveForecastResponse JSON fixture (fallback: minimal OK struct).
func (c *BestTimeApiClientMock) GetLiveForecast(
    venueID, venueName, venueAddress string,
) (*live_forecast.LiveForecastResponse, error) {
    path := config.GetResourcePath(config.LIVE_FORECAST_RESPONSE_RESOURCE)
    if resp, err := util.ReadLiveForecastResponseFromJSON(path); err == nil {
        return resp, nil
    }
    // Fallback minimal mock if the fixture is missing.
    return &live_forecast.LiveForecastResponse{
        Status: "OK",
        Analysis: live_forecast.Analysis{
            VenueForecastedBusyness:        60,
            VenueLiveBusyness:              20,
            VenueLiveBusynessAvailable:     true,
            VenueForecastBusynessAvailable: true,
            VenueLiveForecastedDelta:       -40,
        },
        VenueInfo: live_forecast.VenueInfo{
            VenueID:           venueID,
            VenueName:         venueName,
            VenueTimezone:     "America/Recife",
            VenueDwellTimeMin: 15,
            VenueDwellTimeMax: 60,
            VenueDwellTimeAvg: 30,
        },
    }, nil
}


// VenueFilter reads a VenueFilterResponse JSON fixture.
// If the fixture is missing, it falls back to building one from SearchProgressResponse.
func (c *BestTimeApiClientMock) VenueFilter(params models.VenueFilterParams) (*models.VenueFilterResponse, error) {
    // 1) Try dedicated venue-filter fixture first
    if path := config.GetResourcePath(config.VENUE_FILTER_RESPONSE_RESOURCE); path != "" {
        if resp, err := util.ReadVenueFilterResponseFromJSON(path); err == nil && resp != nil {
            return resp, nil
        }
    }

    // 2) Fallback: synthesize from search-progress fixture
    if progPath := config.GetResourcePath(config.SEARCH_PROGRESS_RESPONSE_RESOURCE); progPath != "" {
        if prog, err := util.ReadSearchProgressResponseFromJSON(progPath); err == nil && prog != nil {
            return &models.VenueFilterResponse{
                Status:  "OK",
                Venues:  prog.Venues,            // uses []venue.Venue
                VenuesN: len(prog.Venues),       // <- fix: count, not a slice literal
                Window:  nil,                    // omitted in fallback
            }, nil
        }
    }

    // 3) Last resort: empty OK response
    return &models.VenueFilterResponse{
        Status:  "OK",
        Venues:  []venue.Venue{},
        VenuesN: 0,
        Window:  nil,
    }, nil
}
