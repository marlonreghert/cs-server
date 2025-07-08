package handlers

import (
    "encoding/json"
    "log"
    "net/http"
    "net/url"
    "sort"
    "strconv"

    "cs-server/dao/redis"
    "cs-server/models/live_forecast"
    "cs-server/models/venue"
)

const (
    LAT_QUERY_ARG     = "lat"
    LON_QUERY_ARG     = "lon"
    RADIUS_QUERY_ARG  = "radius"
    VERBOSE_QUERY_ARG = "verbose"
)

// VenueWithLive pairs a Venue with its cached LiveForecast.
type VenueWithLive struct {
    Venue venue.Venue                       `json:"venue"`
    Live  live_forecast.LiveForecastResponse `json:"live_forecast"`
}

// MinifiedVenue is the small form returned when verbose=false.
type MinifiedVenue struct {
    Forecast                 bool     `json:"forecast"`
    Processed                bool     `json:"processed"`
    VenueAddress             string   `json:"venue_address"`
    VenueFootTrafficForecast []string `json:"venue_foot_traffic_forecast"`
    VenueLiveBusyness        int      `json:"venue_live_busyness"`
    VenueName                string   `json:"venue_name"`
}

type VenueHandler struct {
    redisVenueDao *redis.RedisVenueDAO
}

func NewVenueHandler(redisVenueDao *redis.RedisVenueDAO) *VenueHandler {
    return &VenueHandler{redisVenueDao: redisVenueDao}
}

func (h *VenueHandler) GetVenuesNearby(w http.ResponseWriter, r *http.Request) {
    // 1) Parse query args
    lat, lon, radius, verbose, ok := h.parseArgs(r.URL.Query(), w)
    if !ok {
        return // error already written
    }

    // 2) Load geo-indexed venues
    venues, err := h.loadNearby(lat, lon, radius)
    if err != nil {
        log.Println("Error loading nearby venues:", err)
        http.Error(w, "Internal server error", http.StatusInternalServerError)
        return
    }

    // 3) Merge with cached live forecasts
    merged := h.mergeLive(venues)

    // 4) Transform according to verbose flag
    result := h.transform(merged, verbose)

    // 5) Write JSON
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(http.StatusOK)
    if err := json.NewEncoder(w).Encode(result); err != nil {
        log.Println("Error encoding response:", err)
    }
}

func (h *VenueHandler) parseArgs(vals url.Values, w http.ResponseWriter) (
    lat, lon, radius float64, verbose bool, ok bool,
) {
    var err error

    lat, err = parseArgFloat64(vals, LAT_QUERY_ARG)
    if err != nil {
        http.Error(w, "Invalid argument "+LAT_QUERY_ARG, http.StatusBadRequest)
        return
    }
    lon, err = parseArgFloat64(vals, LON_QUERY_ARG)
    if err != nil {
        http.Error(w, "Invalid argument "+LON_QUERY_ARG, http.StatusBadRequest)
        return
    }
    radius, err = parseArgFloat64(vals, RADIUS_QUERY_ARG)
    if err != nil {
        http.Error(w, "Invalid argument "+RADIUS_QUERY_ARG, http.StatusBadRequest)
        return
    }
    verbose = false
    if v := vals.Get(VERBOSE_QUERY_ARG); v != "" {
        verbose, _ = strconv.ParseBool(v)
    }
    ok = true
    return
}

func (h *VenueHandler) loadNearby(lat, lon, radius float64) ([]venue.Venue, error) {
    return h.redisVenueDao.GetNearbyVenues(lat, lon, radius)
}

func (h *VenueHandler) mergeLive(venues []venue.Venue) []VenueWithLive {
    var out []VenueWithLive
    for _, v := range venues {
        lf, err := h.redisVenueDao.GetLiveForecast(v.VenueID)
        if err != nil {
            log.Printf("No live forecast for venue_id=%s, skipping", v.VenueID)
            continue
        }
        out = append(out, VenueWithLive{Venue: v, Live: *lf})
    }
    // sort by live busyness desc
    sort.Slice(out, func(i, j int) bool {
        return out[i].Live.Analysis.VenueLiveBusyness >
            out[j].Live.Analysis.VenueLiveBusyness
    })
    return out
}

func (h *VenueHandler) transform(merged []VenueWithLive, verbose bool) interface{} {
    if verbose {
        return merged
    }
    // minify
    min := make([]MinifiedVenue, 0, len(merged))
    for _, m := range merged {
        // pull metadata strings from the FootTrafficForecast DayInfo.DayText
        meta := []string{}
        if m.Venue.VenueFootTrafficForecast != nil {
            for _, f := range *m.Venue.VenueFootTrafficForecast {
                if f.DayInfo != nil {
                    meta = append(meta, f.DayInfo.DayText)
                }
            }
        }
        min = append(min, MinifiedVenue{
            Forecast:                 m.Venue.Forecast,
            Processed:                m.Venue.Processed,
            VenueAddress:             m.Venue.VenueAddress,
            VenueFootTrafficForecast: meta,
            VenueLiveBusyness:        m.Live.Analysis.VenueLiveBusyness,
            VenueName:                m.Venue.VenueName,
        })
    }
    return min
}

func parseArgFloat64(vals url.Values, name string) (float64, error) {
    s := vals.Get(name)
    return strconv.ParseFloat(s, 64)
}



// Ping handles GET /ping
func (h *VenueHandler) Ping(w http.ResponseWriter, r *http.Request) {
    log.Println("Pinging server")
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(http.StatusOK)
    json.NewEncoder(w).Encode(map[string]string{"status": "pong"})
}
