package config

import (
	"os"
	"path/filepath"
)

// Redis Config
const REDIS_DB_ADDRESS = "localhost:6379"
const REDIS_DB_PASSWORD = ""
const REDIS_DB = 0

// Venues Refresher config
const VENUES_REFRESHER_SERVICE_SCHEDULE_MINUTES = 1


// Resources file paths
const RESOURCES_PATH_PREFIX = "resources"
const SEARCH_VENUE_RESPONSE_RESOURCE = "search_venues_response.json"
const VENUE_STATIC_RESOURCE = "venue.json"
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