// models/venue_filter_response.go
package models

import "cs-server/models/venue"

// VenueFilterResponse matches the BestTime /venues/filter API response.
type VenueFilterResponse struct {
    Status  string        `json:"status"`
    Venues  []venue.Venue `json:"venues"`
    VenuesN int           `json:"venues_n"`
    Window  *FilterWindow `json:"window,omitempty"`
}

type FilterWindow struct {
    DayWindow          string `json:"day_window"`
    DayWindowEndInt    int    `json:"day_window_end_int"`
    DayWindowEndTxt    string `json:"day_window_end_txt"`
    DayWindowStartInt  int    `json:"day_window_start_int"`
    DayWindowStartTxt  string `json:"day_window_start_txt"`
    TimeLocal          int    `json:"time_local"`
    TimeLocal12        string `json:"time_local_12"`
    TimeLocalIndex     int    `json:"time_local_index"`
    TimeWindowEnd      int    `json:"time_window_end"`
    TimeWindowEnd12    string `json:"time_window_end_12"`
    TimeWindowEndIx    int    `json:"time_window_end_ix"`
    TimeWindowStart    int    `json:"time_window_start"`
    TimeWindowStart12  string `json:"time_window_start_12"`
    TimeWindowStartIx  int    `json:"time_window_start_ix"`
}
