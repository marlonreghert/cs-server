package util

import (
	"cs-server/models"
	"cs-server/models/venue"
	"io/ioutil"
	"os"
	"testing"
)

func createTempFile(t *testing.T, content string) string {
	t.Helper()
	tempFile, err := ioutil.TempFile("", "test*.json")
	if err != nil {
		t.Fatalf("Failed to create temp file: %v", err)
	}
	_, err = tempFile.Write([]byte(content))
	if err != nil {
		t.Fatalf("Failed to write to temp file: %v", err)
	}
	tempFile.Close()
	return tempFile.Name()
}

func TestReadSearchVenuesResponseFromJSON(t *testing.T) {
	// Arrange
	content := `{
		"job_id": "12345",
		"status": "completed",
		"venues_n": 1,
		"venues": [
			{
				"venue_id": "1",
				"venue_name": "Test Venue",
				"venue_address": "123 Test Street"
			}
		]
	}`
	tempFile := createTempFile(t, content)
	defer os.Remove(tempFile)

	// Act
	response, err := ReadSearchVenuesResponseFromJSON(tempFile)

	// Assert
	if err != nil {
		t.Fatalf("Expected no error, got %v", err)
	}

	if response.JobID != "12345" {
		t.Errorf("Expected JobID '12345', got %s", response.JobID)
	}
	if response.Status != "completed" {
		t.Errorf("Expected Status 'completed', got %s", response.Status)
	}
	if len(response.Venues) != 1 {
		t.Fatalf("Expected 1 venue, got %d", len(response.Venues))
	}
	if response.Venues[0].VenueName != "Test Venue" {
		t.Errorf("Expected VenueName 'Test Venue', got %s", response.Venues[0].VenueName)
	}
}

func TestReadVenueFromJSON(t *testing.T) {
	// Arrange
	content := `{
		"venue_id": "1",
		"venue_name": "Test Venue",
		"venue_lat": 40.7128,
		"venue_lon": -74.0060
	}`
	tempFile := createTempFile(t, content)
	defer os.Remove(tempFile)

	// Act
	response, err := ReadVenueFromJSON(tempFile)

	// Assert
	if err != nil {
		t.Fatalf("Expected no error, got %v", err)
	}

	if response.VenueID != "1" {
		t.Errorf("Expected VenueID '1', got %s", response.VenueID)
	}
	if response.VenueName != "Test Venue" {
		t.Errorf("Expected VenueName 'Test Venue', got %s", response.VenueName)
	}
	if response.VenueLat != 40.7128 {
		t.Errorf("Expected VenueLat 40.7128, got %f", response.VenueLat)
	}
	if response.VenueLon != -74.0060 {
		t.Errorf("Expected VenueLon -74.0060, got %f", response.VenueLon)
	}
}

func TestReadVenuesIds(t *testing.T) {
	// Arrange
	content := `["1", "2", "3"]`
	tempFile := createTempFile(t, content)
	defer os.Remove(tempFile)

	// Act
	ids, err := ReadVenuesIds(tempFile)

	// Assert
	if err != nil {
		t.Fatalf("Expected no error, got %v", err)
	}

	if len(ids) != 3 {
		t.Fatalf("Expected 3 IDs, got %d", len(ids))
	}
	expected := []string{"1", "2", "3"}
	for i, id := range expected {
		if ids[i] != id {
			t.Errorf("Expected ID '%s', got '%s'", id, ids[i])
		}
	}
}

func TestPrintSearchVenuesResponsePartially(t *testing.T) {
	// Arrange
	response := &models.SearchVenuesResponse{
		JobID:   "12345",
		Status:  "completed",
		VenuesN: 1,
		Venues: []venue.Venue{
			{
				VenueID:      "1",
				VenueName:    "Test Venue",
				VenueAddress: "123 Test Street",
			},
		},
	}

	// Act
	PrintSearchVenuesResponsePartially(response)

	// This test validates that the function doesn't panic.
	// You can manually check the output or use an output capturing library for advanced testing.
}
