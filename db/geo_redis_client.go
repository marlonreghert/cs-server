package db

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/go-redis/redis/v8"
	"log"
)

// GeoRedisClient struct holds the Redis client and context
type GeoRedisClient struct {
	client *redis.Client
	ctx    context.Context
}

// NewGeoRedisClient initializes a new Redis client with default options
func NewGeoRedisClient(ctx context.Context, client *redis.Client) *GeoRedisClient {
	// Test the connection
	if _, err := client.Ping(ctx).Result(); err != nil {
		log.Fatalf("Could not connect to Redis: %v", err)
	}
	fmt.Println("Connected to Redis")

	return &GeoRedisClient{
		client: client,
		ctx:    ctx,
	}
}

// Set sets a key-value pair in Redis
func (r *GeoRedisClient) Set(key, value string) error {
	return r.client.Set(r.ctx, key, value, 0).Err()
}

// Get retrieves the value for a given key from Redis
func (r *GeoRedisClient) Get(key string) (string, error) {
	return r.client.Get(r.ctx, key).Result()
}

// AddLocationWithJSON stores geolocation along with associated JSON data.
func (r *GeoRedisClient) AddLocationWithJSON(ctx context.Context, geoKey, memberKey string, lat, lon float64, data interface{}) error {
	// Serialize the data to JSON.
	jsonData, err := json.Marshal(data)
	if err != nil {
		return fmt.Errorf("failed to marshal JSON: %v", err)
	}

	// Store the geolocation using GEOADD.
	if _, err := r.client.GeoAdd(ctx, geoKey, &redis.GeoLocation{
		Name:      memberKey,
		Latitude:  lat,
		Longitude: lon,
	}).Result(); err != nil {
		return fmt.Errorf("failed to add geolocation: %v", err)
	}

	// Store the JSON data associated with the same member.
	if err := r.client.Set(ctx, memberKey, jsonData, 0).Err(); err != nil {
		return fmt.Errorf("failed to set JSON data: %v", err)
	}

	log.Printf("Added geolocation and JSON for member: %s", memberKey)
	return nil
}

// GetLocationsWithinRadius finds all venues within the given radius and returns their JSON data.
func (r *GeoRedisClient) GetLocationsWithinRadius(key string, lat, lon, radius float64) ([]string, error) {
	ctx := r.ctx
	log.Println("Reading from radius with key: " + key)
	// Use GEORADIUS to find locations within the radius.
	results, err := r.client.GeoRadius(ctx, key, lon, lat, &redis.GeoRadiusQuery{
		Radius:      radius,
		Unit:        "km", // Radius in meters
		WithCoord:   false,
		WithDist:    false,
		WithGeoHash: false,
	}).Result()
	if err != nil {
		return nil, fmt.Errorf("failed to get nearby locations: %v", err)
	}

	var objects []string
	for _, loc := range results {
		// Fetch the JSON data for each location using its member name.
		jsonKey := loc.Name
		data, err := r.client.Get(ctx, jsonKey).Result()
		if err != nil {
			log.Printf("Skipping member %s due to error: %v", loc.Name, err)
			continue
		} else {
			log.Println("Read: " + data)
		}
		objects = append(objects, data)
	}

	return objects, nil
}

func (r *GeoRedisClient) GetContext() context.Context {
	return r.ctx
}

func (r *GeoRedisClient) Ping() error {
	_, err := r.client.Ping(r.ctx).Result()
	return err
}
