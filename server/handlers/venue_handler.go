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

const LAT_QUERY_ARG = "lat"
const LON_QUERY_ARG = "lon"
const RADIUS_QUERY_ARG = "radius"

// VenueWithLive pairs a Venue with its cached LiveForecast.
type VenueWithLive struct {
    Venue venue.Venue                 `json:"venue"`
    Live  live_forecast.LiveForecastResponse `json:"live_forecast"`
}

type VenueHandler struct {
    redisVenueDao *redis.RedisVenueDAO
}

func NewVenueHandler(redisVenueDao *redis.RedisVenueDAO) *VenueHandler {
    return &VenueHandler{redisVenueDao: redisVenueDao}
}

func (h *VenueHandler) GetVenuesNearby(w http.ResponseWriter, r *http.Request) {
    q := r.URL.Query()

    lat, err := parseArgFloat64(q, LAT_QUERY_ARG, w)
    if err != nil {
        return
    }
    lon, err := parseArgFloat64(q, LON_QUERY_ARG, w)
    if err != nil {
        return
    }
    radius, err := parseArgFloat64(q, RADIUS_QUERY_ARG, w)
    if err != nil {
        return
    }

    // 1) load geo-indexed venues
    venues, err := h.redisVenueDao.GetNearbyVenues(lat, lon, radius)
    if err != nil {
        log.Println("Error looking up venues:", err)
        http.Error(w, "Internal server error", http.StatusInternalServerError)
        return
    }

    // 2) for each venue, get cached live forecast; skip if missing
    var merged []VenueWithLive
    for _, v := range venues {
        lf, err := h.redisVenueDao.GetLiveForecast(v.VenueID)
        if err != nil {
            log.Printf("No live forecast for venue_id=%s, skipping", v.VenueID)
            continue
        }
        merged = append(merged, VenueWithLive{
            Venue: v,
            Live:  *lf,
        })
    }

    // 3) sort by live busyness desc
    sort.Slice(merged, func(i, j int) bool {
        return merged[i].Live.Analysis.VenueLiveBusyness >
               merged[j].Live.Analysis.VenueLiveBusyness
    })

    // 4) write JSON
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(http.StatusOK)
    if err := json.NewEncoder(w).Encode(merged); err != nil {
        log.Println("Error encoding response:", err)
    }
}

func (h *VenueHandler) Ping(w http.ResponseWriter, r *http.Request) {
    log.Println("Pinging server")
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(http.StatusOK)
    json.NewEncoder(w).Encode(map[string]string{"status": "pong"})
}

func parseArgFloat64(vals url.Values, name string, w http.ResponseWriter) (float64, error) {
    s := vals.Get(name)
    f, err := strconv.ParseFloat(s, 64)
    if err != nil {
        log.Printf("Invalid %s: %v", name, err)
        http.Error(w, "Invalid argument "+name, http.StatusBadRequest)
        return 0, err
    }
    return f, nil
}
