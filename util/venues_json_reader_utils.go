package util

import (
    "encoding/json"
    "fmt"
    "io/ioutil"

    "cs-server/models"
    "cs-server/models/venue"
    "cs-server/models/live_forecast"
)

// ReadSearchVenuesResponseFromJSON loads a SearchVenuesResponse from JSON on disk.
func ReadSearchVenuesResponseFromJSON(filePath string) (*models.SearchVenuesResponse, error) {
    data, err := ioutil.ReadFile(filePath)
    if err != nil {
        return nil, fmt.Errorf("failed to read file %q: %w", filePath, err)
    }
    var resp models.SearchVenuesResponse
    if err := json.Unmarshal(data, &resp); err != nil {
        return nil, fmt.Errorf("failed to unmarshal SearchVenuesResponse: %w", err)
    }
    return &resp, nil
}

// ReadSearchProgressResponseFromJSON loads a SearchProgressResponse from JSON on disk.
func ReadSearchProgressResponseFromJSON(filePath string) (*models.SearchProgressResponse, error) {
    data, err := ioutil.ReadFile(filePath)
    if err != nil {
        return nil, fmt.Errorf("failed to read file %q: %w", filePath, err)
    }
    var resp models.SearchProgressResponse
    if err := json.Unmarshal(data, &resp); err != nil {
        return nil, fmt.Errorf("failed to unmarshal SearchProgressResponse: %w", err)
    }
    return &resp, nil
}

// ReadVenueFromJSON loads a single Venue from JSON on disk.
func ReadVenueFromJSON(filePath string) (*venue.Venue, error) {
    data, err := ioutil.ReadFile(filePath)
    if err != nil {
        return nil, fmt.Errorf("failed to read file %q: %w", filePath, err)
    }
    var v venue.Venue
    if err := json.Unmarshal(data, &v); err != nil {
        return nil, fmt.Errorf("failed to unmarshal Venue: %w", err)
    }
    return &v, nil
}

// ReadVenuesIds loads a slice of venue IDs from JSON on disk.
func ReadVenuesIds(filePath string) ([]string, error) {
    data, err := ioutil.ReadFile(filePath)
    if err != nil {
        return nil, fmt.Errorf("failed to read file %q: %w", filePath, err)
    }
    var ids []string
    if err := json.Unmarshal(data, &ids); err != nil {
        return nil, fmt.Errorf("failed to unmarshal venue IDs: %w", err)
    }
    return ids, nil
}

// PrintSearchVenuesResponsePartially prints key fields of SearchVenuesResponse.
func PrintSearchVenuesResponsePartially(resp *models.SearchVenuesResponse) {
    fmt.Printf("Job ID: %s\n", resp.JobID)
    fmt.Printf("Status: %s\n", resp.Status)
    fmt.Printf("Collection ID: %s\n", resp.CollectionID)
    fmt.Printf("Bounding Box: %+v\n", resp.BoundingBox)
    fmt.Printf("Progress link: %s\n", resp.Links.VenueSearchProgress)
}

// PrintSearchProgressResponsePartially prints key fields of SearchProgressResponse.
func PrintSearchProgressResponsePartially(resp *models.SearchProgressResponse) {
    fmt.Printf("Progress Job ID: %s\n", resp.JobID)
    fmt.Printf("Status: %s\n", resp.Status)
    fmt.Printf("Processed: %d/%d\n", resp.CountCompleted, resp.CountTotal)
    fmt.Printf("Forecasted: %d, Live: %d, Failed: %d\n", resp.CountForecast, resp.CountLive, resp.CountFailed)
    if resp.JobFinished {
        fmt.Printf("Search completed (Collection ID: %s)\n", resp.CollectionID)
        fmt.Printf("Venues returned: %d\n", resp.VenuesN)
    }
    if len(resp.Venues) > 0 {
        v := resp.Venues[0]
        fmt.Printf("First venue: %s at %s (%.6f, %.6f)\n", v.VenueName, v.VenueAddress, v.VenueLat, v.VenueLon)
    }
}

func ReadLiveForecastResponseFromJSON(filePath string) (*live_forecast.LiveForecastResponse, error) {
    data, err := ioutil.ReadFile(filePath)
    if err != nil {
        return nil, fmt.Errorf("failed to read file %q: %w", filePath, err)
    }
    var resp live_forecast.LiveForecastResponse
    if err := json.Unmarshal(data, &resp); err != nil {
        return nil, fmt.Errorf("failed to unmarshal LiveForecastResponse: %w", err)
    }
    return &resp, nil
}

// ReadVenueFilterResponseFromJSON reads a VenueFilterResponse struct from a JSON file.
func ReadVenueFilterResponseFromJSON(filePath string) (*models.VenueFilterResponse, error) {
    data, err := ioutil.ReadFile(filePath)
    if err != nil {
        return nil, fmt.Errorf("failed to read VenueFilterResponse file %q: %w", filePath, err)
    }
    var resp models.VenueFilterResponse
    if err := json.Unmarshal(data, &resp); err != nil {
        return nil, fmt.Errorf("failed to unmarshal VenueFilterResponse: %w", err)
    }
    return &resp, nil
}