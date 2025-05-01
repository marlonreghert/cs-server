package redis

import (
    "cs-server/db"
    "cs-server/models/live_forecast"
    "cs-server/models/venue"
    "encoding/json"
    "fmt"
    "log"
    "strings"	
)

const VENUES_GEO_KEY_V1 = "venues_geo_v1"
const VENUES_GEO_PLACE_MEMBER_FORMAT_V1 = "venues_geo_place_v1:%s"

// LIVE_FORECAST_KEY_FORMAT is used to cache live forecasts per venue.
const LIVE_FORECAST_KEY_FORMAT = "live_forecast_v1:%s"

// RedisVenueDAO handles venue operations using Redis.
type RedisVenueDAO struct {
    client db.RedisClient
}

// NewRedisVenueDAO initializes a RedisVenueDAO with the Redis client.
func NewRedisVenueDAO(client db.RedisClient) *RedisVenueDAO {
    return &RedisVenueDAO{client: client}
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

    venues := make([]venue.Venue, len(venuesJSON))
    for i, venueJSON := range venuesJSON {
        if err := json.Unmarshal([]byte(venueJSON), &venues[i]); err != nil {
            return nil, fmt.Errorf("failed to unmarshal venue JSON: %v", err)
        }
    }
    log.Println("Finished getting nearby venues")
    return venues, nil
}

// SetLiveForecast caches the live forecast for a venue by its ID.
func (dao *RedisVenueDAO) SetLiveForecast(f *live_forecast.LiveForecastResponse) error {
    key := fmt.Sprintf(LIVE_FORECAST_KEY_FORMAT, f.VenueInfo.VenueID)
    data, err := json.Marshal(f)
    if err != nil {
        return fmt.Errorf("failed to marshal live forecast for venue %s: %w", f.VenueInfo.VenueID, err)
    }
    if err := dao.client.Set(key, string(data)); err != nil {
        return fmt.Errorf("failed to set live forecast in redis: %w", err)
    }
    return nil
}

// GetLiveForecast retrieves the cached live forecast for a venue by its ID.
func (dao *RedisVenueDAO) GetLiveForecast(venueID string) (*live_forecast.LiveForecastResponse, error) {
    key := fmt.Sprintf(LIVE_FORECAST_KEY_FORMAT, venueID)
    str, err := dao.client.Get(key)
    if err != nil {
        return nil, fmt.Errorf("failed to get live forecast from redis: %w", err)
    }
    var f live_forecast.LiveForecastResponse
    if err := json.Unmarshal([]byte(str), &f); err != nil {
        return nil, fmt.Errorf("failed to unmarshal live forecast JSON: %w", err)
    }
    return &f, nil
}

// ListCachedLiveForecastVenueIDs returns the venue‐IDs for all cached live forecasts.
func (dao *RedisVenueDAO) ListCachedLiveForecastVenueIDs() ([]string, error) {
    // pattern matches the prefix used in SetLiveForecast
    pattern := "live_forecast_v1:*"
    keys, err := dao.client.Keys(pattern)
    if err != nil {
        return nil, fmt.Errorf("failed to list live‐forecast keys: %w", err)
    }

    ids := make([]string, 0, len(keys))
    for _, k := range keys {
        // strip the prefix to get the raw venueID
        ids = append(ids, strings.TrimPrefix(k, "live_forecast_v1:"))
    }
    return ids, nil
}

func (dao *RedisVenueDAO) DeleteLiveForecast(venueID string) error {
    key := fmt.Sprintf(LIVE_FORECAST_KEY_FORMAT, venueID)
    if err := dao.client.Del(key); err != nil {
        return fmt.Errorf("failed to delete live forecast key %s: %w", key, err)
    }
    log.Printf("[RedisVenueDAO] Deleted live forecast cache for %s", venueID)
    return nil
}