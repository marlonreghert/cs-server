package models

import (
	"cs-server/models/venue"
)

type SearchVenuesResponse struct {
	Links          Link          `json:"_links"`
	BoundingBox    BoundingBox   `json:"bounding_box"`
	CollectionID   string        `json:"collection_id"`
	CountCompleted int           `json:"count_completed"`
	CountFailure   int           `json:"count_failure"`
	CountForecast  int           `json:"count_forecast"`
	CountLive      int           `json:"count_live"`
	CountTotal     int           `json:"count_total"`
	JobFinished    bool          `json:"job_finished"`
	JobID          string        `json:"job_id"`
	Status         string        `json:"status"`
	Venues         []venue.Venue `json:"venues"`
	VenuesN        int           `json:"venues_n"`
}
