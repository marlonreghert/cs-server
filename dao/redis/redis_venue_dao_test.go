package redis

import (
	"context"
	"cs-server/db"
	"cs-server/models/venue"
	"encoding/json"
	"testing"
)

func TestRedisVenueDAO_UpsertVenue_Success(t *testing.T) {
	// Setup
	mockClient := db.NewMockRedisClient(context.Background())
	dao := NewRedisVenueDAO(mockClient)

	testVenue := venue.Venue{
		VenueID:   "venue123",
		VenueLat:  40.7128,
		VenueLon:  -74.0060,
		VenueName: "Test Venue",
	}

	// Act
	err := dao.UpsertVenue(testVenue)

	// Assert
	if err != nil {
		t.Fatalf("Expected no error, got %v", err)
	}

	// Verify data stored in mock Redis
	expectedKey := "venues_geo_place_v1:venue123"
	storedValue, err := mockClient.Get(expectedKey)
	if err != nil {
		t.Fatalf("Expected data to be stored, got error: %v", err)
	}

	// Verify JSON content
	var storedVenue venue.Venue
	if err := json.Unmarshal([]byte(storedValue), &storedVenue); err != nil {
		t.Fatalf("Failed to unmarshal stored venue data: %v", err)
	}

	if storedVenue.VenueID != testVenue.VenueID {
		t.Errorf("Expected VenueID %s, got %s", testVenue.VenueID, storedVenue.VenueID)
	}
}

func TestRedisVenueDAO_GetNearbyVenues_Success(t *testing.T) {
	// Setup
	mockClient := db.NewMockRedisClient(context.Background())
	dao := NewRedisVenueDAO(mockClient)

	// Add test venues
	testVenue1 := venue.Venue{
		VenueID:   "venue123",
		VenueLat:  40.7128,
		VenueLon:  -74.0060,
		VenueName: "Test Venue 1",
	}
	testVenue2 := venue.Venue{
		VenueID:   "venue456",
		VenueLat:  40.7130,
		VenueLon:  -74.0050,
		VenueName: "Test Venue 2",
	}
	_ = dao.UpsertVenue(testVenue1)
	_ = dao.UpsertVenue(testVenue2)

	// Act
	venues, err := dao.GetNearbyVenues(40.7128, -74.0060, 1000)

	// Assert
	if err != nil {
		t.Fatalf("Expected no error, got %v", err)
	}

	if len(venues) != 2 {
		t.Errorf("Expected 2 venues, got %d", len(venues))
	}

	// Verify contents of the retrieved venues
	expectedIDs := map[string]bool{
		"venue123": true,
		"venue456": true,
	}
	for _, v := range venues {
		if !expectedIDs[v.VenueID] {
			t.Errorf("Unexpected venue ID: %s", v.VenueID)
		}
	}
}

func TestRedisVenueDAO_GetNearbyVenues_NoResults(t *testing.T) {
	// Setup
	mockClient := db.NewMockRedisClient(context.Background())
	dao := NewRedisVenueDAO(mockClient)

	// Act
	venues, err := dao.GetNearbyVenues(40.7128, -74.0060, 1000)

	// Assert
	if err != nil {
		t.Fatalf("Expected no error, got %v", err)
	}

	if len(venues) != 0 {
		t.Errorf("Expected no venues, got %d", len(venues))
	}
}