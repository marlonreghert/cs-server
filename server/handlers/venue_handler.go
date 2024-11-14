package handlers

import (
	"cs-server/dao/redis"
	"encoding/json"
	"log"
	"net/http"
	"net/url"
	"strconv"
)

const LAT_QUERY_ARG = "lat"
const LON_QUERY_ARG = "lon"
const RADIUS_QUERY_ARG = "radius"

type VenueHandler struct {
	redisVenueDao *redis.RedisVenueDAO
}

func NewVenueHandler(redisVenueDao *redis.RedisVenueDAO) *VenueHandler {
	return &VenueHandler{
		redisVenueDao: redisVenueDao,
	}
}

// GetVenue handles GET requests to retrieve a venue by ID.
func (h *VenueHandler) GetVenuesNearby(w http.ResponseWriter, r *http.Request) {
	queryArguments := r.URL.Query()

	lat, _ := parseArgFloat64(queryArguments, LAT_QUERY_ARG, w)

	lon, _ := parseArgFloat64(queryArguments, LON_QUERY_ARG, w)

	radius, _ := parseArgFloat64(queryArguments, RADIUS_QUERY_ARG, w)

	venues, err := h.redisVenueDao.GetNearbyVenues(lat, lon, radius)
	if err != nil {
		log.Println("An error happened while looking up for venues.")
		http.Error(w, "Venue not found", http.StatusInternalServerError)
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(venues)
}

// Ping handles GET requests to simply ping the server
func (h *VenueHandler) Ping(w http.ResponseWriter, r *http.Request) {
	log.Println("Pinging server")
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode("{\"status\": \"pong\"}")

}

func parseArgFloat64(values url.Values, argName string, w http.ResponseWriter) (float64, error) {
	valStr := values.Get(argName)
	val, err := strconv.ParseFloat(valStr, 64)
	if err != nil {
		log.Println("An error happened while parsing: " + argName)
		http.Error(w, "Invalid argument "+argName, http.StatusBadRequest)
		return -1, err
	}

	return val, nil
}
