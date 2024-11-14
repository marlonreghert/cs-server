package venue

// FootTrafficForecast represents the forecast data for a specific day.
type FootTrafficForecast struct {
	DayInfo *DayInfo `json:"day_info,omitempty"`
	DayInt  int      `json:"day_int"`
	DayRaw  []int    `json:"day_raw"`
}
