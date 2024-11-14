package services

import (
	"cs-server/api/besttime"
	"cs-server/dao/redis"
	"cs-server/models/venue"
	"cs-server/util"
)

const VENUES_IDS_PATH = "./resources/static_venues_ids.json"

type VenueService struct {
	venueDao    *redis.RedisVenueDAO
	besttimeApi besttime.BestTimeAPI
}

// NewVenueService constructs a new VenueService with Redis dependency injection.
func NewVenueService(
	venueDao *redis.RedisVenueDAO,
	bestTimeApi besttime.BestTimeAPI) *VenueService {

	return &VenueService{
		venueDao:    venueDao,
		besttimeApi: bestTimeApi,
	}
}

func (vs *VenueService) GetVenuesNearby(lat, lon, radius float64) ([]venue.Venue, error) {
	return vs.venueDao.GetNearbyVenues(lat, lon, radius)
}

func (vs *VenueService) GetAllVenuesIds() ([]string, error) {
	return util.ReadVenuesIds(VENUES_IDS_PATH)
}

func (vs *VenueService) GetVenue(venueId string) (*venue.Venue, error) {
	return vs.besttimeApi.GetVenue(venueId)
}
