package util

import (
	"cs-server/models"
	"cs-server/models/venue"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"os"
)

func ReadSearchVenuesResponseFromJSON(filePath string) (*models.SearchVenuesResponse, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	data, err := ioutil.ReadAll(file)
	if err != nil {
		return nil, fmt.Errorf("failed to read file: %w", err)
	}

	var response models.SearchVenuesResponse
	if err := json.Unmarshal(data, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal JSON: %w", err)
	}

	return &response, nil
}

func ReadVenueFromJSON(filePath string) (*venue.Venue, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	data, err := ioutil.ReadAll(file)
	if err != nil {
		return nil, fmt.Errorf("failed to read file: %w", err)
	}

	var response venue.Venue
	if err := json.Unmarshal(data, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal JSON: %w", err)
	}

	return &response, nil
}

func ReadVenuesIds(filePath string) ([]string, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	data, err := ioutil.ReadAll(file)
	if err != nil {
		return nil, fmt.Errorf("failed to read file: %w", err)
	}

	var response []string
	if err := json.Unmarshal(data, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal JSON: %w", err)
	}

	return response, nil
}

// printVenues takes any implementation of BestTimeAPI
func PrintSearchVenuesResponsePartially(response *models.SearchVenuesResponse) {
	fmt.Printf("Job ID: %s\n", response.JobID)
	fmt.Printf("Status: %s\n", response.Status)
	fmt.Printf("Number of Venues: %d\n", response.VenuesN)

	if len(response.Venues) > 0 {
		firstVenue := response.Venues[0]
		fmt.Printf("First Venue: %s at %s\n", firstVenue.VenueName, firstVenue.VenueAddress)
	}
}
