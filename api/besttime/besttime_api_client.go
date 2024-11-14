package besttime

import (
	"cs-server/api"
	"cs-server/models"
	"cs-server/models/venue"
)

// BestTimeApiClient embeds the common HTTPClient
type BestTimeApiClient struct {
	*api.HTTPClient // Embed HTTPClient to reuse its methods and properties
}

// NewBestTimeApiClient creates a new instance of BestTimeApiClient
func NewBestTimeApiClient(httpClient *api.HTTPClient) *BestTimeApiClient {
	return &BestTimeApiClient{
		HTTPClient: httpClient,
	}
}

// GetVenuesNearby retrieves nearby venues and decodes the response into the Response struct
func (c *BestTimeApiClient) GetVenuesNearby(lat float64, long float64) (*models.SearchVenuesResponse, error) {
	var response models.SearchVenuesResponse
	err := c.Request("GET", "/venues/nearby", nil, nil, &response)
	if err != nil {
		return nil, err
	}
	return &response, nil
}

// GetVenuesNearby retrieves a venue given a venue id
func (c *BestTimeApiClient) GetVenue(venueId string) (*venue.Venue, error) {
	var response venue.Venue
	err := c.Request("GET", "/venues/"+venueId, nil, nil, &response)
	if err != nil {
		return nil, err
	}
	return &response, nil
}
