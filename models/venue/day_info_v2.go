// models/venue/day_info.go (or similar file in your venue package)

package venue

// DayInfoV2 matches the 'venue_open_close_v2' structure.
type DayInfoV2 struct {
	Open24H         bool              `json:"open_24h"`
	CrossesMidnight bool              `json:"crosses_midnight"`
	DayText         string            `json:"day_text"`
	SpecialDay      interface{}       `json:"special_day"` // Can be null or string/object
	H24             []OpenCloseDetail `json:"24h"`         // Using H24 because 24h is an invalid struct field name
	H12             []string          `json:"12h"`
}

// OpenCloseDetail matches one element in the '24h' array.
type OpenCloseDetail struct {
	Opens          int `json:"opens"`
	Closes         int `json:"closes"`
	OpensMinutes   int `json:"opens_minutes"`
	ClosesMinutes  int `json:"closes_minutes"`
}