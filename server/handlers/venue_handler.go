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
// Live is now a *pointer* so we can omit it when there is no live data.
type VenueWithLive struct {
    Venue venue.Venue                         `json:"venue"`
    Live  *live_forecast.LiveForecastResponse `json:"live_forecast,omitempty"`
}

// MinifiedVenue is the small form returned when verbose=false.
type MinifiedVenue struct {
    Forecast                 bool                          `json:"forecast"`
    Processed                bool                          `json:"processed"`
    VenueAddress             string                        `json:"venue_address"`
    VenueFootTrafficForecast *[]venue.FootTrafficForecast  `json:"venue_foot_traffic_forecast,omitempty"`
    // Pointer so it's omitted when there is no live data
    VenueLiveBusyness        *int                          `json:"venue_live_busyness,omitempty"`
    VenueLat                 float64                       `json:"venue_lat"`
    VenueLng                 float64                       `json:"venue_lon"`
    VenueName                string                        `json:"venue_name"`
    PriceLevel               int                           `json:"price_level,omitempty"`
    Rating                   float64                       `json:"rating,omitempty"`
    Reviews                  int                           `json:"reviews,omitempty"`
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

    // 3) Merge with cached live forecasts (no longer skipping venues without live)
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

// mergeLive now **does not skip** venues without live data.
// It always appends the venue, and sets Live to nil when not found.
// Sorting: venues with live data come first (by busyness desc), then venues without live data.
func (h *VenueHandler) mergeLive(venues []venue.Venue) []VenueWithLive {
    out := make([]VenueWithLive, 0, len(venues))

    for _, v := range venues {
        lf, err := h.redisVenueDao.GetLiveForecast(v.VenueID)
        if err != nil {
            // No live forecast (or other error) – keep the venue, but Live=nil
            log.Printf("No live forecast for venue_id=%s: %v", v.VenueID, err)
            out = append(out, VenueWithLive{
                Venue: v,
                Live:  nil,
            })
            continue
        }

        out = append(out, VenueWithLive{
            Venue: v,
            Live:  lf,
        })
    }

    // sort: venues with live first (desc by busyness), then without live
    sort.SliceStable(out, func(i, j int) bool {
        li := out[i].Live
        lj := out[j].Live

        // Only i has live -> i first
        if li != nil && lj == nil {
            return true
        }
        // Only j has live -> j first
        if li == nil && lj != nil {
            return false
        }
        // Both have no live: keep original order
        if li == nil && lj == nil {
            return false
        }
        // Both have live: sort by live busyness desc
        return li.Analysis.VenueLiveBusyness > lj.Analysis.VenueLiveBusyness
    })

    return out
}

func (h *VenueHandler) transform(merged []VenueWithLive, verbose bool) interface{} {
    if verbose {
        // In verbose mode, you get the full Venue + optional Live.
        return merged
    }

    min := make([]MinifiedVenue, 0, len(merged))
    for _, m := range merged {
        var busyness *int
        if m.Live != nil && m.Live.Analysis.VenueLiveBusynessAvailable {
            v := m.Live.Analysis.VenueLiveBusyness
            busyness = &v
        }

        min = append(min, MinifiedVenue{
            Forecast:                 m.Venue.Forecast,
            Processed:                m.Venue.Processed,
            VenueAddress:             m.Venue.VenueAddress,
            VenueFootTrafficForecast: m.Venue.VenueFootTrafficForecast,
            VenueLiveBusyness:        busyness, // nil when no live => omitted in JSON
            VenueLat:                 m.Venue.VenueLat,
            VenueLng:                 m.Venue.VenueLon,
            VenueName:                m.Venue.VenueName,
            PriceLevel:               m.Venue.PriceLevel,
            Rating:                   m.Venue.Rating,
            Reviews:                  m.Venue.Reviews,
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
