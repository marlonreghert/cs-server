package venue

import (
	"encoding/json"
	"fmt"
)

// DayInfo represents detailed information for a single day's forecast.
type DayInfo struct {
	DayInt      int    `json:"day_int"`
	DayMax      int    `json:"day_max"`
	DayMean     int    `json:"day_mean"`
	DayRankMax  int    `json:"day_rank_max"`
	DayRankMean int    `json:"day_rank_mean"`
	DayText     string `json:"day_text"`

	VenueOpen   string `json:"venue_open"`   // Read as string.
	VenueClosed string `json:"venue_closed"` // Read as string.
}

// UnmarshalJSON custom unmarshaler to convert int fields to string.
func (d *DayInfo) UnmarshalJSON(data []byte) error {
	// Create an alias to avoid infinite recursion.
	type Alias DayInfo
	aux := &struct {
		VenueOpen   interface{} `json:"venue_open"`
		VenueClosed interface{} `json:"venue_closed"`
		*Alias
	}{
		Alias: (*Alias)(d),
	}

	// Unmarshal into the auxiliary structure.
	if err := json.Unmarshal(data, &aux); err != nil {
		return err
	}

	// Convert `VenueOpen` to string.
	if val, ok := aux.VenueOpen.(float64); ok {
		d.VenueOpen = fmt.Sprintf("%d", int(val))
	} else if val, ok := aux.VenueOpen.(string); ok {
		d.VenueOpen = val
	} else {
		d.VenueOpen = "" // Default value in case of error.
	}

	// Convert `VenueClosed` to string.
	if val, ok := aux.VenueClosed.(float64); ok {
		d.VenueClosed = fmt.Sprintf("%d", int(val))
	} else if val, ok := aux.VenueClosed.(string); ok {
		d.VenueClosed = val
	} else {
		d.VenueClosed = "" // Default value in case of error.
	}

	return nil
}
