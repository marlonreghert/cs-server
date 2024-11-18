package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHTTPClient_Request_Success(t *testing.T) {
	// Mock server setup
	mockResponse := map[string]string{"message": "success"}
	mockServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/test-endpoint" {
			t.Errorf("Expected endpoint '/test-endpoint', got '%s'", r.URL.Path)
		}

		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(mockResponse)
	}))
	defer mockServer.Close()

	// Test setup
	client := NewHTTPClient(mockServer.URL)
	var response map[string]string

	// Act
	err := client.Request("GET", "/test-endpoint", nil, nil, &response)

	// Assert
	if err != nil {
		t.Fatalf("Expected no error, got %v", err)
	}

	if response["message"] != "success" {
		t.Errorf("Expected response message to be 'success', got '%s'", response["message"])
	}
}

func TestHTTPClient_Request_Failure(t *testing.T) {
	// Mock server setup
	mockServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"error": "bad request"}`))
	}))
	defer mockServer.Close()

	// Test setup
	client := NewHTTPClient(mockServer.URL)
	var response map[string]string

	// Act
	err := client.Request("POST", "/test-endpoint", nil, map[string]string{"key": "value"}, &response)

	// Assert
	if err == nil {
		t.Fatalf("Expected an error, got nil")
	}

	expectedError := "unexpected status code: 400 Bad Request"
	if err.Error() != expectedError {
		t.Errorf("Expected error '%s', got '%s'", expectedError, err.Error())
	}
}
