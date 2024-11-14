package server

import (
	"cs-server/server/handlers"
	"github.com/gorilla/mux"
)

type Router struct {
	venueHandler *handlers.VenueHandler
	router       *mux.Router
}

// NewRouter creates a router with the app’s routes.
func NewRouter(
	venueHandler *handlers.VenueHandler,
	router *mux.Router) *Router {
	return &Router{
		venueHandler: venueHandler,
		router:       router,
	}
}

func (r *Router) RegisterRoutes() {
	// expects ?lat={latitude(float)}&long={longitude(float)}&radius={radius(float)}
	r.router.HandleFunc("/v1/venues/nearby", r.venueHandler.GetVenuesNearby).Methods("GET")

	r.router.HandleFunc("/ping", r.venueHandler.GetVenuesNearby).Methods("GET")
}
