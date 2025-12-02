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

    VenueOpen   string `json:"venue_open"`   
    VenueClosed string `json:"venue_closed"` 
    
    // NEW FIELD: Make it a pointer so it's omitted if not present in the JSON payload
    VenueOpenCloseV2 *DayInfoV2 `json:"venue_open_close_v2,omitempty"`
}

// UnmarshalJSON custom unmarshaler to convert int fields to string.
// NOTE: This custom unmarshaler must be updated to handle the new field.
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
    
    // --- Existing logic for VenueOpen/Closed conversion ---
    if val, ok := aux.VenueOpen.(float64); ok {
        d.VenueOpen = fmt.Sprintf("%d", int(val))
    } else if val, ok := aux.VenueOpen.(string); ok {
        d.VenueOpen = val
    } else {
        d.VenueOpen = ""
    }

    if val, ok := aux.VenueClosed.(float64); ok {
        d.VenueClosed = fmt.Sprintf("%d", int(val))
    } else if val, ok := aux.VenueClosed.(string); ok {
        d.VenueClosed = val
    } else {
        d.VenueClosed = ""
    }
    
    // The new field d.VenueOpenCloseV2 is handled automatically by the struct *Alias
    // as long as the JSON field name matches the struct field name's JSON tag.
    
    return nil
}