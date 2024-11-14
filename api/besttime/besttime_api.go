package besttime

import (
	"cs-server/models"
	"cs-server/models/venue"
)

// BestTimeAPI defines the interface for interacting with the BestTime API
type BestTimeAPI interface {
	GetVenuesNearby(lat float64, long float64) (*models.SearchVenuesResponse, error)
	GetVenue(venueId string) (*venue.Venue, error)
}
