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

func testRedisClient(redisClient db.RedisClient) db.RedisClient {
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

func testBestTimeAPIClient(bestTimeApiClient besttime.BestTimeAPI) {
    log.Println("Running: testBestTimeAPIClient")
    resp, err := bestTimeApiClient.GetVenuesNearby(-43.3122, -60.535)
    if err != nil {
        log.Println("Error starting venue search:", err)
        return
    }

    util.PrintSearchVenuesResponsePartially(resp)
    plotBoundingBox(resp)

    // wait before polling the background job
    log.Println("Waiting 15 seconds before polling search progress...")
    time.Sleep(15 * time.Second)

    // now fetch the progress
    prog, err := bestTimeApiClient.GetVenueSearchProgress(resp.JobID, resp.CollectionID)
    if err != nil {
        log.Println("Error fetching search progress:", err)
        return
    }

    util.PrintSearchProgressResponsePartially(prog)
}

func testRedisGeoLoc(redisClient db.RedisClient, ctx context.Context) {
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

// testProgressDrivenUpsert reads a SearchProgressResponse JSON and optionally upserts into Redis.
func testVenueDao(venuesDao *redis.RedisVenueDAO, addVenues bool) {
    log.Println("Testing venues dao with SearchProgressResponse")

    // Load progress response from JSON fixture
    progressResp, err := util.ReadSearchProgressResponseFromJSON("./resources/search_progress_response.json")
    if err != nil {
        log.Fatalf("Failed to read search progress response: %v", err)
        return
    }

    // Iterate through all venues in the progress response
    for _, v := range progressResp.Venues {
        fmt.Printf("Processed Venue: %s at %s (%.6f, %.6f)\n", v.VenueName, v.VenueAddress, v.VenueLat, v.VenueLon)
        if addVenues {
            if err := venuesDao.UpsertVenue(v); err != nil {
                log.Printf("[MAIN] Failed to upsert venue %s: %v", v.VenueID, err)
            }
        }
    }

    // Optionally, verify nearby retrieval
    lat, lon := 0.0, 0.0 // adjust as needed
    nearby, err := venuesDao.GetNearbyVenues(lat, lon, 1000)
    if err != nil {
        log.Fatalf("[MAIN] Failed to get nearby venues: %v", err)
        return
    }
    log.Printf("Found %d venues nearby\n", len(nearby))
    for _, v := range nearby {
        fmt.Printf("Nearby Venue: %s at %s\n", v.VenueName, v.VenueAddress)
    }
}


func main() {
	container := di.NewContainer("prod")

	// testBestTimeAPIClient(container.BestTimeAPI)
	// testRedisClient(container.RedisClient)
	// testVenueDao(container.RedisVenueDao, false)

	fmt.Println("refreshing!")
	container.VenuesRefresherService.RefreshVenuesData(true)
	fmt.Println("starting periodic job!")
	container.VenuesRefresherService.StartPeriodicJob(config.VENUES_REFRESHER_SERVICE_SCHEDULE_MINUTES * time.Minute)
	fmt.Println("next step!")
	_ = time.Minute *  3
	
	fmt.Println("starting server!")
	container.CrowdSenseHttpServer.Start()
	fmt.Println(" server started!")
}
