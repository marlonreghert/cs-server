package db

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sync"
)

// MockRedisClient simulates a Redis client for testing purposes.
type MockRedisClient struct {
	data        map[string]string            // Key-value store
	geoData     map[string]map[string]GeoLoc // Geolocation data
	mu          sync.RWMutex                 // Mutex for thread-safe operations
	context     context.Context
}

// GeoLoc represents a geolocation with latitude and longitude.
type GeoLoc struct {
	Latitude  float64
	Longitude float64
}

// NewMockRedisClient initializes a new MockRedisClient.
func NewMockRedisClient(ctx context.Context) *MockRedisClient {
	return &MockRedisClient{
		data:    make(map[string]string),
		geoData: make(map[string]map[string]GeoLoc),
		context: ctx,
	}
}

// Set stores a key-value pair in the mock Redis.
func (m *MockRedisClient) Set(key, value string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.data[key] = value
	return nil
}

// Get retrieves a value for a given key from the mock Redis.
func (m *MockRedisClient) Get(key string) (string, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	value, exists := m.data[key]
	if !exists {
		return "", fmt.Errorf("key not found: %s", key)
	}
	return value, nil
}

// AddLocationWithJSON adds geolocation with JSON data in the mock Redis.
func (m *MockRedisClient) AddLocationWithJSON(ctx context.Context, geoKey, memberKey string, lat, lon float64, data interface{}) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Serialize the data to JSON.
	jsonData, err := json.Marshal(data)
	if err != nil {
		return fmt.Errorf("failed to marshal JSON: %v", err)
	}

	// Add to geolocation data.
	if _, exists := m.geoData[geoKey]; !exists {
		m.geoData[geoKey] = make(map[string]GeoLoc)
	}
	m.geoData[geoKey][memberKey] = GeoLoc{Latitude: lat, Longitude: lon}

	// Add JSON data.
	m.data[memberKey] = string(jsonData)
	return nil
}

// GetLocationsWithinRadius retrieves JSON data for members within a given radius.
func (m *MockRedisClient) GetLocationsWithinRadius(key string, lat, lon, radius float64) ([]string, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	geoMembers, exists := m.geoData[key]
	if !exists {
		return nil, nil // No geolocation data for this key.
	}

	// Mock logic: Return all JSON data for simplicity.
	var results []string
	for memberKey := range geoMembers {
		if data, exists := m.data[memberKey]; exists {
			results = append(results, data)
		}
	}
	return results, nil
}

// GetContext returns the mock Redis client's context.
func (m *MockRedisClient) GetContext() context.Context {
	return m.context
}

// Ping simulates a Redis Ping operation.
func (m *MockRedisClient) Ping() error {
	// Always return nil (indicating Redis is "reachable").
	log.Println("MockRedisClient: Ping successful")
	return nil
}


func (m *MockRedisClient) Keys(pattern string) ([]string, error) {

	return []string{}, nil
}

func (m *MockRedisClient) Del(key string) error   {
	return nil
}


