// models/venue_filter_venue.go
package models

import "cs-server/models/venue"

// VenueFilterVenue matches a single "venues[N]" in /venues/filter response.
type VenueFilterVenue struct {
    // Foot traffic fields
    DayInt      int          `json:"day_int"`
    DayRaw      []int        `json:"day_raw"`
    DayRawWhole []int        `json:"day_raw_whole,omitempty"`
    DayInfo     *venue.DayInfo `json:"day_info,omitempty"`

    // Core venue info
    VenueAddress string  `json:"venue_address"`
    VenueLat     float64 `json:"venue_lat"`
    VenueLng     float64 `json:"venue_lng"`
    VenueID      string  `json:"venue_id"`
    VenueName    string  `json:"venue_name"`

    // Extra fields
    VenueType         string  `json:"venue_type,omitempty"`
    VenueDwellTimeMin int     `json:"venue_dwell_time_min,omitempty"`
    VenueDwellTimeMax int     `json:"venue_dwell_time_max,omitempty"`
    PriceLevel        *int    `json:"price_level,omitempty"`
    Rating            float64 `json:"rating,omitempty"`
    Reviews           int     `json:"reviews,omitempty"`
}
