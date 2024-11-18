package besttime

import (
	// "os"
	"testing"
	"cs-server/config"
	"github.com/stretchr/testify/assert"
	"cs-server/util"
)

var (
	SEARCH_VENUES_RESPONSE_PATH = config.GetResourcePath(config.SEARCH_VENUE_RESPONSE_RESOURCE)
	VENUES_RESPONSE_PATH = config.GetResourcePath(config.VENUE_STATIC_RESOURCE)
)

func TestGetVenuesNearby_Success(t *testing.T) {
	// Arrange
	client := NewBestTimeApiClientMock()

	// Create a valid mock JSON file
	expected_response, err := util.ReadSearchVenuesResponseFromJSON(config.GetResourcePath(config.SEARCH_VENUE_RESPONSE_RESOURCE))

	if err != nil {
		t.Errorf("expected no error when reading expected response, got %v", err)
	}

	// Act
	response, err := client.GetVenuesNearby(1.23, 4.56)

	// Assert
	if err != nil {
		t.Errorf("expected no error, got %v", err)
	}
	
	assert.Equal(t, expected_response, response, "Responses dont match")
}

func TestGetVenue_Success(t *testing.T) {
	// Arrange
	client := NewBestTimeApiClientMock()

	// Create a valid mock JSON file
	expected_response, err := util.ReadVenueFromJSON(config.GetResourcePath(config.VENUE_STATIC_RESOURCE))

	if err != nil {
		t.Errorf("expected no error when reading expected response, got %v", err)
	}

	// Act
	response, err := client.GetVenue("124")

	// Assert
	if err != nil {
		t.Errorf("expected no error, got %v", err)
	}
	
	assert.Equal(t, expected_response, response, "Responses dont match")
}

// func TestGetVenuesNearby_MalformedJSON(t *testing.T) {
// 	// Arrange
// 	client := NewBestTimeApiClientMock()

// 	// Create a malformed JSON file
// 	mockData := `{"invalid_json`
// 	err := os.WriteFile(SEARCH_VENUES_RESPONSE_PATH, []byte(mockData), 0644)
// 	if err != nil {
// 		t.Fatalf("failed to create mock file: %v", err)
// 	}
// 	defer os.Remove(SEARCH_VENUES_RESPONSE_PATH)

// 	// Act
// 	response, err := client.GetVenuesNearby(1.23, 4.56)

// 	// Assert
// 	if err == nil {
// 		t.Errorf("expected an error, got nil")
// 	}

// 	if response != nil {
// 		t.Errorf("expected response to be nil, got %v", response)
// 	}
// }

// func TestGetVenue_MalformedJSON(t *testing.T) {
// 	// Arrange
// 	client := NewBestTimeApiClientMock()

// 	// Create a malformed JSON file
// 	mockData := `{"invalid_json`
// 	err := os.WriteFile(VENUES_RESPONSE_PATH, []byte(mockData), 0644)
// 	if err != nil {
// 		t.Fatalf("failed to create mock file: %v", err)
// 	}
// 	defer os.Remove(VENUES_RESPONSE_PATH)

// 	// Act
// 	response, err := client.GetVenue("1")

// 	// Assert
// 	if err == nil {
// 		t.Errorf("expected an error, got nil")
// 	}

// 	if response != nil {
// 		t.Errorf("expected response to be nil, got %v", response)
// 	}
// }
