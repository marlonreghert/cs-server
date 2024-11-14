package models

type Link struct {
	BackgroundProgressAPI  string `json:"background_progress_api"`
	BackgroundProgressTool string `json:"background_progress_tool"`
	RadarTool              string `json:"radar_tool"`
	VenueFilterAPI         string `json:"venue_filter_api"`
}
