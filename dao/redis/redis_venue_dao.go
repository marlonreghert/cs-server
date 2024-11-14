package redis

import (
	"cs-server/db"
	"cs-server/models/venue"
	"encoding/json"
	"fmt"
	"log"
)

const VENUES_GEO_KEY_V1 = "venues_geo_v1"
const VENUES_GEO_PLACE_MEMBER_FORMAT_V1 = "venues_geo_place_v1:%s"

// RedisVenueDAO handles venue operations using Redis.
type RedisVenueDAO struct {
	client *db.RedisClient
}

// NewRedisVenueDAO initializes a RedisVenueDAO with the Redis client.
func NewRedisVenueDAO(client *db.RedisClient) *RedisVenueDAO {
	return &RedisVenueDAO{
		client: client,
	}
}

// UpsertVenue stores the venue as a geolocation with the venue's JSON data.
func (dao *RedisVenueDAO) UpsertVenue(v venue.Venue) error {
	ctx := dao.client.GetContext()

	venueKey := fmt.Sprintf(VENUES_GEO_PLACE_MEMBER_FORMAT_V1, v.VenueID)

	return dao.client.AddLocationWithJSON(ctx, VENUES_GEO_KEY_V1, venueKey, v.VenueLat, v.VenueLon, v)
}

// GetNearbyVenues retrieves nearby venues within a given radius (in meters).
func (dao *RedisVenueDAO) GetNearbyVenues(lat, lon float64, radius float64) ([]venue.Venue, error) {
	log.Println("Getting nearby venues")
	venuesJSON, err := dao.client.GetLocationsWithinRadius(VENUES_GEO_KEY_V1, lat, lon, radius)
	if err != nil {
		return nil, fmt.Errorf("[RedisVenueDAO] failed to get venues: %v", err)
	}
	venues := make([]venue.Venue, len(venuesJSON)) // Preallocate memory

	for i, venueJSON := range venuesJSON {
		log.Println("Unmarshing json")
		if err := json.Unmarshal([]byte(venueJSON), &venues[i]); err != nil {
			return nil, fmt.Errorf("failed to unmarshal venue JSON: %v", err)
		}
	}
	log.Println("Finished getting nearby venues")
	return venues, nil
}
