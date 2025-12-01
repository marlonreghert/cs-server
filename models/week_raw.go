// models/week_raw.go

package models

import "cs-server/models/venue"

// WeekRawDay matches one element in the 'week_raw' array.
// This contains the full hourly busyness data (day_raw) and summary data (day_info).
type WeekRawDay struct {
	DayRaw   []int          `json:"day_raw"`
	DayInt   int            `json:"day_int"`
	DayInfo  *venue.DayInfo `json:"day_info"`
}

// WeekRawAnalysis is the explicit definition of the 'analysis' block.
type WeekRawAnalysis struct {
	WeekRaw []WeekRawDay `json:"week_raw"`
}

// RawWindow matches the 'window' block, describing the time scope of the forecast.
type RawWindow struct {
	TimeWindowStart    int    `json:"time_window_start"`
	TimeWindowStart12H string `json:"time_window_start_12h"`
	DayWindowStartInt  int    `json:"day_window_start_int"`
	DayWindowStartTxt  string `json:"day_window_start_txt"`
	DayWindowEndInt    int    `json:"day_window_end_int"`
	DayWindowEndTxt    string `json:"day_window_end_txt"`
	TimeWindowEnd      int    `json:"time_window_end"`
	TimeWindowEnd12H   string `json:"time_window_end_12h"`
	WeekWindow         string `json:"week_window"`
}

// WeekRawResponse is the top-level JSON returned by GET /forecasts/week/raw2
type WeekRawResponse struct {
	VenueAddress string          `json:"venue_address"`
	Window       RawWindow       `json:"window"`
	Status       string          `json:"status"`
	Analysis     WeekRawAnalysis `json:"analysis"` // Using the NAMED struct to avoid mock errors
	VenueName    string          `json:"venue_name"`
	VenueID      string          `json:"venue_id"`
}