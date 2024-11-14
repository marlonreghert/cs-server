package main

import (
	"context"
	"cs-server/api/besttime"
	"cs-server/config"
	"cs-server/dao/redis"
	"cs-server/db"
	"cs-server/di"
	"cs-server/models"
	"cs-server/util"
	"fmt"
	"log"
	"net/http"
	"time"
)

const lat = 45.5204001
const lon = -73.5540803

func handler(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintf(w, "Hello, World!")
}

func testRedisClient(redisClient *db.RedisClient) *db.RedisClient {
	// Set a key-value pair
	if err := redisClient.Set("mykey", "myvalue"); err != nil {
		log.Fatalf("Failed to set key: %v", err)
	}

	// Get the value for the key
	val, err := redisClient.Get("mykey")
	if err != nil {
		log.Fatalf("Failed to get key: %v", err)
	}
	fmt.Printf("mykey: %s\n", val)

	return redisClient
}

// printVenues takes any implementation of BestTimeAPI
func printVenues(apiClient besttime.BestTimeAPI) {
	response, err := apiClient.GetVenuesNearby(-50.33, -69.33)
	if err != nil {
		log.Println("Error:", err)
		return
	}

	fmt.Printf("Job ID: %s\n", response.JobID)
	fmt.Printf("Status: %s\n", response.Status)
	fmt.Printf("Number of Venues: %d\n", response.VenuesN)

	if len(response.Venues) > 0 {
		firstVenue := response.Venues[0]
		fmt.Printf("First Venue: %s at %s\n", firstVenue.VenueName, firstVenue.VenueAddress)
	}
}

func plotBoundingBox(response *models.SearchVenuesResponse) {
	util.PlotBoundingBox(*response)
}

func testMockedBestTimeAPIClient(bestTimeApiClient besttime.BestTimeAPI) {
	log.Println("Running: testMockedBestTimeAPIClient")
	response, err := bestTimeApiClient.GetVenuesNearby(-43.3122, -60.535)
	if err != nil {
		log.Println("Error while running testMockedBestTimeAPIClient: ", err)
	}

	util.PrintSearchVenuesResponsePartially(response)

	plotBoundingBox(response)
}

func testRedisGeoLoc(redisClient *db.RedisClient, ctx context.Context) {
	log.Println("[MAIN] Testing redis geo loc")
	// Example data to store.
	data := map[string]interface{}{
		"venue_name": "Maloneys Grocer",
		"address":    "4/490 Crown St, Surry Hills NSW 2010, Australia",
	}

	// Add geolocation and associated JSON data.
	if err := redisClient.AddLocationWithJSON(ctx, "test_key_v1", "maloneys", -1, -1, data); err != nil {
		log.Fatalf("Failed to add location and JSON: %v", err)
	}

	// Retrieve and print the JSON data.
	jsonData, err := redisClient.GetLocationsWithinRadius("test_key_v1", -1, -1, 1000)
	if err != nil {
		log.Fatalf("Failed to get JSON data: %v", err)
	}
	log.Printf("Retrieved JSON: %s", jsonData)
}

func testVenueDao(venuesDao *redis.RedisVenueDAO, addVenues bool) {
	log.Println("Testing venues dao")

	venuesResponse, err := util.ReadSearchVenuesResponseFromJSON("./resources/search_venues_response.json")
	if err != nil {
		log.Fatalf("Failed to read venues response: %v", err)
		return
	}

	v := venuesResponse.Venues[0]

	fmt.Printf("Venues response: %s\n", v.ToString())
	if addVenues {
		err = venuesDao.UpsertVenue(v)
		if err != nil {
			log.Fatalf("[MAIN] Failed to add venues: %v", err)
			return
		}
	}

	venues, err := venuesDao.GetNearbyVenues(lat, lon, 1000)
	if err != nil {
		log.Fatalf("[MAIN] Failed to get venues: %v", err)
		return
	}
	log.Println("Found venues:")
	log.Println(len(venues))
	for _, v := range venues {
		fmt.Printf("Venue name: %s\n", v.VenueName)
	}
}

func main() {
	container := di.NewContainer()

	testMockedBestTimeAPIClient(container.BestTimeAPI)
	testRedisClient(container.RedisClient)
	testVenueDao(container.RedisVenueDao, false)

	container.VenuesRefresherService.RefreshVenuesData()
	container.VenuesRefresherService.StartPeriodicJob(config.VENUES_REFRESHER_SERVICE_SCHEDULE_MINUTES * time.Minute)

	container.CrowdSenseHttpServer.Start()
}
