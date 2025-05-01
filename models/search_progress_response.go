// models/search_progress_response.go
package models

import "cs-server/models/venue"

type SearchProgressResponse struct {
    Links         Link           `json:"_links"`
    CountTotal    int            `json:"count_total"`
    CountCompleted int           `json:"count_completed"`
    CountForecast int            `json:"count_forecasted"`
    CountLive     int            `json:"count_live"`
    CountFailed   int            `json:"count_failed"`
    JobFinished   bool           `json:"job_finished"`
    CollectionID  string         `json:"collection_id"`
    JobID         string         `json:"job_id"`
    Status        string         `json:"status"`
    // The fields below only appear once job_finished==true
    Venues        []venue.Venue  `json:"venues,omitempty"`
    VenuesN       int            `json:"venues_n,omitempty"`
    BoundingBox   BoundingBox    `json:"bounding_box,omitempty"`
}
