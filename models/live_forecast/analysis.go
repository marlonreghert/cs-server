package live_forecast

// Analysis holds the busyness numbers and availability flags
type Analysis struct {
    VenueForecastedBusyness        int  `json:"venue_forecasted_busyness"`
    VenueLiveBusyness              int  `json:"venue_live_busyness"`
    VenueLiveBusynessAvailable     bool `json:"venue_live_busyness_available"`
    VenueForecastBusynessAvailable bool `json:"venue_forecast_busyness_available"`
    VenueLiveForecastedDelta       int  `json:"venue_live_forecasted_delta"`
}
