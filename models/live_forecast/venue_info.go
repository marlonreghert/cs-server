package live_forecast

// VenueInfo holds the metadata about the venue and its times
type VenueInfo struct {
    VenueCurrentGMTTime   string `json:"venue_current_gmttime"`
    VenueCurrentLocalTime string `json:"venue_current_localtime"`
    VenueID               string `json:"venue_id"`
    VenueName             string `json:"venue_name"`
    VenueTimezone         string `json:"venue_timezone"`
    VenueDwellTimeMin     int    `json:"venue_dwell_time_min"`
    VenueDwellTimeMax     int    `json:"venue_dwell_time_max"`
    VenueDwellTimeAvg     int    `json:"venue_dwell_time_avg"`
}