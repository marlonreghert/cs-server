package venue

import "fmt"

// Venue represents a venue with forecast data.
type Venue struct {
	Forecast                 bool                   `json:"forecast"`
	Processed                bool                   `json:"processed"`
	VenueAddress             string                 `json:"venue_address"`
	VenueLat                 float64                `json:"venue_lat"`
	VenueLon                 float64                `json:"venue_lon"`
	VenueName                string                 `json:"venue_name"`
	VenueID                  string                 `json:"venue_id"`
	VenueFootTrafficForecast *[]FootTrafficForecast `json:"venue_foot_traffic_forecast,omitempty"`
}

func (v *Venue) ToString() string {
	return fmt.Sprintf("Vanue(name=%s, address=%s, lat=%f, lon=%f)",
		v.VenueName, v.VenueAddress, v.VenueLat, v.VenueLon)
}
