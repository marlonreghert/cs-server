package server

import (
	"github.com/gorilla/mux"
	"net/http"
	"net/http/httptest"
	"testing"
)

// MockVenueHandler is a mock implementation of VenueHandler.
type MockVenueHandler struct{}

func (h *MockVenueHandler) GetVenuesNearby(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(`{"message": "venues nearby"}`))
}

func TestRouter_RegisterRoutes(t *testing.T) {
	// Setup
	mockVenueHandler := &MockVenueHandler{}
	router := mux.NewRouter()
	appRouter := NewRouter(mockVenueHandler, router)
	appRouter.RegisterRoutes()

	// Test Cases
	tests := []struct {
		name       string
		method     string
		path       string
		statusCode int
		response   string
	}{
		{
			name:       "Get Venues Nearby",
			method:     "GET",
			path:       "/v1/venues/nearby",
			statusCode: http.StatusOK,
			response:   `{"message": "venues nearby"}`,
		},
		{
			name:       "Ping Route",
			method:     "GET",
			path:       "/ping",
			statusCode: http.StatusOK,
			response:   `{"message": "venues nearby"}`,
		},
		{
			name:       "Invalid Route",
			method:     "GET",
			path:       "/invalid",
			statusCode: http.StatusNotFound,
		},
	}

	// Run tests
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(test.method, test.path, nil)
			rr := httptest.NewRecorder()

			router.ServeHTTP(rr, req)

			// Assert status code
			if rr.Code != test.statusCode {
				t.Errorf("Expected status %d, got %d", test.statusCode, rr.Code)
			}

			// Assert response body, if applicable
			if test.response != "" && rr.Body.String() != test.response {
				t.Errorf("Expected response %s, got %s", test.response, rr.Body.String())
			}
		})
	}
}
