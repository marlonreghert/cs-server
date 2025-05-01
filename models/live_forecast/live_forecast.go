package live_forecast

// LiveForecastResponse is the top-level JSON returned by POST /forecasts/live
type LiveForecastResponse struct {
    Analysis  Analysis  `json:"analysis"`
    Status    string    `json:"status"`
    VenueInfo VenueInfo `json:"venue_info"`
}
