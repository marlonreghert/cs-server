package db

import "context"

// RedisClientInterface defines the methods available in the RedisClient
type RedisClient interface {
	Set(key, value string) error
	Get(key string) (string, error)
	AddLocationWithJSON(ctx context.Context, geoKey, memberKey string, lat, lon float64, data interface{}) error
	GetLocationsWithinRadius(key string, lat, lon, radius float64) ([]string, error)
	GetContext() context.Context
	Ping() error
    Keys(pattern string) ([]string, error)
	Del(key string) error  
}
