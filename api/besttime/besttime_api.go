package besttime

import (
	"cs-server/models"
	"cs-server/models/venue"
	"cs-server/models/live_forecast"
)

// BestTimeAPI defines the interface for interacting with the BestTime API
type BestTimeAPI interface {
	GetVenuesNearby(lat float64, long float64) (*models.SearchVenuesResponse, error)
	GetVenue(venueId string) (*venue.Venue, error)
	GetVenueSearchProgress(jobID, collectionID string) (*models.SearchProgressResponse, error) 
	SetCredentials(apiKeyPublic string, apiKeyPrivate string) 
	GetLiveForecast(venueID, venueName, venueAddress string) (*live_forecast.LiveForecastResponse, error)
	VenueFilter(params models.VenueFilterParams) (*models.VenueFilterResponse, error)
}


