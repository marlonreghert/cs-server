package config

import (
	"os"
	"path/filepath"
)

// Redis Config
const REDIS_DB_ADDRESS = "redis:6379"
const REDIS_DB_PASSWORD = ""
const REDIS_DB = 0

// Venues Refresher config
// 3 Days: 60*24*32
const VENUES_CATALOG_REFRESHER_SCHEDULE_MINUTES = 60
const VENUES_LIVE_FORECAST_REFRESHER_SCHEDULE_MINUTES = 30

// Best Time API Keys
const BEST_TIME_PRIVATE_KEY = "pri_aff50a71a038456db88864b16d9d6800"
const BEST_TIME_PUBLIC_KEY = "pub_4f4f184e1a5f4f50a48e945fde7ab2ea"
const BEST_TIME_ENDPOINT_BASE_V1 = "https://besttime.app/api/v1"
const BEST_TIME_SEARCH_POLLING_WAIT_SECONDS = 15

// Resources file paths
const RESOURCES_PATH_PREFIX = "resources"
const SEARCH_VENUE_RESPONSE_RESOURCE = "search_venues_response.json"
const VENUE_STATIC_RESOURCE = "venue_static.json"
const SEARCH_PROGRESS_RESPONSE_RESOURCE = "search_progress_response.json"
const LIVE_FORECAST_RESPONSE_RESOURCE  = "live_forecast_response.json" 
const VENUE_FILTER_RESPONSE_RESOURCE = "venue_filter_response.json"
const VENUES_IDS_RESOURCE = "static_venues_ids.json"

// BaseDir returns the absolute path of the project root directory
func BaseDir() string {
	// Check if PROJECT_ROOT is set
	if root := os.Getenv("PROJECT_ROOT"); root != "" {
		return root
	}

	// Default to the current working directory
	wd, err := os.Getwd()
	if err != nil {
		panic("Unable to determine working directory: " + err.Error())
	}

	return wd
}

func GetResourcePath(resource_file string) string {
	print("Base dir: " + BaseDir())
	return filepath.Join(BaseDir(), RESOURCES_PATH_PREFIX, resource_file)
} 