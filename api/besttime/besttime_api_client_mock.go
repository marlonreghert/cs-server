package besttime

import (
    "fmt"

    "cs-server/config"
    "cs-server/models"
    "cs-server/models/live_forecast"
    "cs-server/models/venue"
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

// SetCredentials is a no-op for the mock.
func (c *BestTimeApiClientMock) SetCredentials(apiKeyPublic, apiKeyPrivate string) {}

// GetLiveForecast returns a simple dummy live-forecast so callers don’t panic.
func (c *BestTimeApiClientMock) GetLiveForecast(
    venueID, venueName, venueAddress string,
) (*live_forecast.LiveForecastResponse, error) {
    lf := &live_forecast.LiveForecastResponse{
        Status: "OK",
        Analysis: live_forecast.Analysis{
            VenueForecastedBusyness:        50,
            VenueLiveBusyness:              50,
            VenueLiveBusynessAvailable:     true,
            VenueForecastBusynessAvailable: true,
            VenueLiveForecastedDelta:       0,
        },
        VenueInfo: live_forecast.VenueInfo{
            VenueID:   venueID,
            VenueName: venueName,
        },
    }
    return lf, nil
}

// VenueFilter reads a VenueFilterResponse JSON fixture.
// If the fixture is missing or invalid, it falls back to an empty OK response.
func (c *BestTimeApiClientMock) VenueFilter(params models.VenueFilterParams) (*models.VenueFilterResponse, error) {
    // Try dedicated venue-filter fixture first (if you have one configured).
    if path := config.GetResourcePath(config.VENUE_FILTER_RESPONSE_RESOURCE); path != "" {
        if resp, err := util.ReadVenueFilterResponseFromJSON(path); err == nil && resp != nil {
            return resp, nil
        }
    }

    // Fallback: empty but valid response
    return &models.VenueFilterResponse{
        Status:  "OK",
        Venues:  []models.VenueFilterVenue{},
        VenuesN: 0,
        Window:  nil,
    }, nil
}
func (c *BestTimeApiClientMock) GetWeekRawForecast(venueID string) (*models.WeekRawResponse, error) {
	// Simple mock response
	return &models.WeekRawResponse{
		Status: "OK",
        // Initialize Analysis using the NAMED struct
		Analysis: models.WeekRawAnalysis{
			WeekRaw: []models.WeekRawDay{
				// Return a dummy Monday entry for testing
				{DayInt: 0, DayRaw: []int{10, 20, 30, 40}, DayInfo: nil}, 
			},
		},
		VenueID: venueID,
	}, nil
}