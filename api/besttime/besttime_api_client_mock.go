package besttime

import (
	"cs-server/models"
	"cs-server/models/venue"
	"cs-server/util"
	"cs-server/config"
	"fmt"
)

// BestTimeApiClientMock embeds mocked logic for the best time api client
type BestTimeApiClientMock struct {
}

// NewBestTimeApiClientMock creates a new instance of BestTimeApiClientMock
func NewBestTimeApiClientMock() *BestTimeApiClientMock {
	return &BestTimeApiClientMock{}
}

// GetVenuesNearby retrieves nearby venues and decodes the response into the Response struct
func (c *BestTimeApiClientMock) GetVenuesNearby(lat float64, long float64) (*models.SearchVenuesResponse, error) {
	var response *models.SearchVenuesResponse
	response, err := util.ReadSearchVenuesResponseFromJSON(config.GetResourcePath(config.SEARCH_VENUE_RESPONSE_RESOURCE))

	if err != nil {
		fmt.Println("Could not read search venues response from json")
		return nil, err
	}

	return response, nil
}

// GetVenuesNearby retrieves a venue given a venue id
func (c *BestTimeApiClientMock) GetVenue(venueId string) (*venue.Venue, error) {
	var response *venue.Venue
	response, err := util.ReadVenueFromJSON(config.GetResourcePath(config.VENUE_STATIC_RESOURCE))

	if err != nil {
		fmt.Println("Could not read search venues response from json")
		return nil, err
	}

	return response, nil
}
