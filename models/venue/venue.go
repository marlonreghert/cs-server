package venue

import "fmt"

// Venue represents a venue with forecast data.
type Venue struct {
    Forecast     bool    `json:"forecast"`
    Processed    bool    `json:"processed"`

    VenueAddress string  `json:"venue_address"`
    VenueLat     float64 `json:"venue_lat"`
    VenueLon     float64 `json:"venue_lng"`
    VenueName    string  `json:"venue_name"`
    VenueID      string  `json:"venue_id"`

    // Extra details coming from Venue Filter / Forecast APIs:
    VenueType         string   `json:"venue_type,omitempty"`
    VenueDwellTimeMin int      `json:"venue_dwell_time_min,omitempty"`
    VenueDwellTimeMax int      `json:"venue_dwell_time_max,omitempty"`
    PriceLevel        int      `json:"price_level,omitempty"`
    Rating            float64  `json:"rating,omitempty"`
    Reviews           int      `json:"reviews,omitempty"`

    VenueFootTrafficForecast *[]FootTrafficForecast `json:"venue_foot_traffic_forecast,omitempty"`
}

func (v *Venue) ToString() string {
    return fmt.Sprintf("Venue(name=%s, address=%s, lat=%f, lon=%f)",
        v.VenueName, v.VenueAddress, v.VenueLat, v.VenueLon)
}
