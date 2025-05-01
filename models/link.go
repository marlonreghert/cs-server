// models/link.go
package models

type Link struct {
    VenueSearchProgress    string `json:"venue_search_progress"`     // ← new
    BackgroundProgressTool string `json:"background_progress_tool"`
    RadarTool              string `json:"radar_tool"`
    VenueFilterAPI         string `json:"venue_filter_api"`
}
