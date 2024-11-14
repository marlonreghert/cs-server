package besttime

import (
	"cs-server/models"
	"cs-server/models/venue"
	"cs-server/util"
	"fmt"
)

const SEARCH_VENUES_RESPONSE_PATH = "./resources/search_venues_response.json"
const VENUES_RESPONSE_PATH = "./resources/venue.json"

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
	response, err := util.ReadSearchVenuesResponseFromJSON(SEARCH_VENUES_RESPONSE_PATH)

	if err != nil {
		fmt.Println("Could not read search venues response from json")
		return nil, err
	}

	return response, nil
}

// GetVenuesNearby retrieves a venue given a venue id
func (c *BestTimeApiClientMock) GetVenue(venueId string) (*venue.Venue, error) {
	var response *venue.Venue
	response, err := util.ReadVenueFromJSON(VENUES_RESPONSE_PATH)

	if err != nil {
		fmt.Println("Could not read search venues response from json")
		return nil, err
	}

	return response, nil
}
