package db_test

import (
	"context"
	"cs-server/db"
	"encoding/json"
	
	"testing"
)

// Test the Set and Get methods for both MockRedisClient and GeoRedisClient
func TestRedisClient_SetAndGet(t *testing.T) {
	tests := []struct {
		name   string
		client db.RedisClient
	}{
		{"MockRedisClient", db.NewMockRedisClient(context.Background())},
		// Replace with a real Redis client configuration for integration testing
		// {"GeoRedisClient", db.NewGeoRedisClient(context.Background(), realRedisClient)},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			key := "test-key"
			value := "test-value"

			// Act
			err := test.client.Set(key, value)
			if err != nil {
				t.Fatalf("Set failed: %v", err)
			}

			retrieved, err := test.client.Get(key)
			if err != nil {
				t.Fatalf("Get failed: %v", err)
			}

			// Assert
			if retrieved != value {
				t.Errorf("Expected %s, got %s", value, retrieved)
			}
		})
	}
}

// Test AddLocationWithJSON and GetLocationsWithinRadius for MockRedisClient
func TestRedisClient_AddLocationWithJSONAndGetLocationsWithinRadius(t *testing.T) {
	mockClient := db.NewMockRedisClient(context.Background())

	tests := []struct {
		name   string
		client db.RedisClient
	}{
		{"MockRedisClient", mockClient},
		// Replace with a real Redis client configuration for integration testing
		// {"GeoRedisClient", db.NewGeoRedisClient(context.Background(), realRedisClient)},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			geoKey := "venues"
			memberKey := "venue123"
			latitude, longitude := 40.7128, -74.0060
			radius := 1000.0

			venue := map[string]string{
				"id":   "venue123",
				"name": "Test Venue",
			}

			// Act
			err := test.client.AddLocationWithJSON(context.Background(), geoKey, memberKey, latitude, longitude, venue)
			if err != nil {
				t.Fatalf("AddLocationWithJSON failed: %v", err)
			}

			results, err := test.client.GetLocationsWithinRadius(geoKey, latitude, longitude, radius)
			if err != nil {
				t.Fatalf("GetLocationsWithinRadius failed: %v", err)
			}

			// Assert
			if len(results) != 1 {
				t.Fatalf("Expected 1 result, got %d", len(results))
			}

			var retrievedVenue map[string]string
			err = json.Unmarshal([]byte(results[0]), &retrievedVenue)
			if err != nil {
				t.Fatalf("Failed to unmarshal JSON: %v", err)
			}

			if retrievedVenue["id"] != "venue123" {
				t.Errorf("Expected venue ID 'venue123', got '%s'", retrievedVenue["id"])
			}
		})
	}
}

// Test Ping for both MockRedisClient and GeoRedisClient
func TestRedisClient_Ping(t *testing.T) {
	tests := []struct {
		name   string
		client db.RedisClient
	}{
		{"MockRedisClient", db.NewMockRedisClient(context.Background())},
		// Replace with a real Redis client configuration for integration testing
		// {"GeoRedisClient", db.NewGeoRedisClient(context.Background(), realRedisClient)},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			// Act
			err := test.client.Ping()

			// Assert
			if err != nil {
				t.Errorf("Ping failed: %v", err)
			}
		})
	}
}
