package services

import (
    "log"
    "time"
    "cs-server/models"
	"cs-server/config"
    "cs-server/api/besttime"
    "cs-server/dao/redis"
)

// Location holds latitude and longitude for refresh jobs.
type Location struct {
    Lat float64
    Lng float64
}

// jobHandle ties together a kicked-off search with its job and collection IDs.
type jobHandle struct {
    JobID, CollectionID string
}


// defaultLocations is the constant list of coordinates to query.
// Start it empty and populate manually as needed.
var defaultLocations = []Location{
	Location { 
        // Pina
		Lat: -8.098632,  
		Lng: -34.884890416,
	},
	Location { 
        // BV
		Lat: -8.121918,
		Lng: -34.903602,
	},
	Location { 
        // Centro
		Lat: -8.059297,
		Lng: -34.880373,
	},
	Location { 
        // ZN / Cordeiro
		Lat: -8.060852,
		Lng: -34.910644,
	},
	Location { 
        // Olinda / Salgadinho
		Lat: -8.029736,
		Lng: -34.870261,
	},
	Location { 
        // Olinda / Sé
		Lat: -8.004132,
		Lng: -34.854365,
	}, 
	Location { 
        // Varzea
		Lat: -8.047251,
		Lng: -34.939524,
	},
	Location { 
        // SP Pinheiros
		Lat: -23.558037,
		Lng: -46.700183,
	},
	Location { 
        // SP - Jardim America 
		Lat: -23.567292, 
		Lng: -46.677463,
	},
	Location { 
        // SP - Augusta
		Lat: -23.556218, 
		Lng: -46.665451,
	},    
	Location { 
        // SP - Santa Cecilia
		Lat: -23.542361, 
		Lng: -46.655989,
	},    
}

// VenuesRefresherService periodically refreshes venues via BestTime API.
type VenuesRefresherService struct {
    venueDao    *redis.RedisVenueDAO
    bestTimeAPI besttime.BestTimeAPI
}

// NewVenuesRefresherService constructs a new Refresher with dependencies.
func NewVenuesRefresherService(
    venueDao *redis.RedisVenueDAO,
    bestTimeAPI besttime.BestTimeAPI,
) *VenuesRefresherService {
    return &VenuesRefresherService{
        venueDao:    venueDao,
        bestTimeAPI: bestTimeAPI,
    }
}

// StartPeriodicJob launches the background loop at the given interval.
func (vr *VenuesRefresherService) StartPeriodicJob(interval time.Duration) {
    go vr.startPeriodicJob(interval)
}

func (vr *VenuesRefresherService) startPeriodicJob(interval time.Duration) {
    ticker := time.NewTicker(interval)
    defer ticker.Stop()

    for range ticker.C {
        log.Println("[VenuesRefresherService] Running periodic venues refresher job.")
        if err := vr.RefreshVenuesData(true); err != nil {
            log.Printf("[VenuesRefresherService] RefreshVenuesData returned error: %v", err)
        } else {
            log.Println("[VenuesRefresherService] RefreshVenuesData completed successfully.")
        }
    }
}

// RefreshVenuesData orchestrates the four steps: kick-off, wait, process, live-fetch+cache.
func (vr *VenuesRefresherService) RefreshVenuesData(waitBeforePolling bool) error {
    // 1) Kick off searches
    handles := vr.collectJobHandles()
    if len(handles) == 0 {
        log.Println("[VenuesRefresherService] No successful searches to poll; exiting.")
        return nil
    }

    // 2) Should wait before polling ?
    if waitBeforePolling {
        vr.waitBeforePolling(1)
    }
    

    // 3) Poll progress, dedupe, upsert → returns unique IDs
    ids := vr.processJobHandles(handles)

    // 4) Fetch & cache live forecasts for each ID
    vr.fetchAndCacheLiveForecasts(ids)

    return nil
}

// collectJobHandles kicks off a venue search for each location and returns the job handles.
func (vr *VenuesRefresherService) collectJobHandles() []jobHandle {
    var handles []jobHandle
    log.Printf("[VenuesRefresherService] Starting searches for %d locations", len(defaultLocations))

    for _, loc := range defaultLocations {
        log.Printf("[VenuesRefresherService] Starting search at lat=%.6f, lng=%.6f", loc.Lat, loc.Lng)
        resp, err := vr.bestTimeAPI.GetVenuesNearby(loc.Lat, loc.Lng)
        if err != nil {
            log.Printf("[VenuesRefresherService] Failed to start search for %v,%v: %v", loc.Lat, loc.Lng, err)
            continue
        }
        log.Printf("[VenuesRefresherService] Search started: job_id=%s collection_id=%s",
            resp.JobID, resp.CollectionID)
        handles = append(handles, jobHandle{JobID: resp.JobID, CollectionID: resp.CollectionID})
    }
    return handles
}

// waitBeforePolling sleeps for the configured polling interval.
func (vr *VenuesRefresherService) waitBeforePolling(attemptNumber int) {
    wait := time.Duration(config.BEST_TIME_SEARCH_POLLING_WAIT_SECONDS) * attemptNumber * time.Second
    log.Printf("[VenuesRefresherService] Waiting %v before polling progress...", wait)
    time.Sleep(wait)
}

// processJobHandles polls each job handle, dedupes venues, upserts them, and returns the unique IDs.
func (vr *VenuesRefresherService) processJobHandles(handles []jobHandle) []string {
    seenIDs := make(map[string]struct{})
    seenNames := make(map[string]struct{})
    var uniqueIDs []string

    log.Printf("[VenuesRefresherService] Polling progress for %d jobs", len(handles))
    for _, h := range handles {
        log.Printf("[VenuesRefresherService] Polling job_id=%s collection_id=%s", h.JobID, h.CollectionID)

        var progResp *models.SearchProgressResponse
        var err error

        const maxRetries = 5
        for i := 0; i < maxRetries; i++ {
            progResp, err = vr.bestTimeAPI.GetVenueSearchProgress(h.JobID, h.CollectionID)
            if err != nil {
                log.Printf("[VenuesRefresherService] Failed polling job %s (attempt %d): %v", h.JobID, i+1, err)
                break // unrecoverable error, skip retries
            }

            if progResp.JobFinished {
                break
            }

            log.Printf("[VenuesRefresherService] Job %s not finished yet (attempt %d/%d), waiting to retry...", h.JobID, i+1, maxRetries)
            vr.waitBeforePolling(i + 1)
        }

        if err != nil || progResp == nil || !progResp.JobFinished {
            log.Printf("[VenuesRefresherService] Job %s did not finish after %d attempts, skipping.", h.JobID, maxRetries)
            continue
        }

        log.Printf(
            "[VenuesRefresherService] Progress: job_finished=%v total=%d completed=%d forecasted=%d live=%d failed=%d",
            progResp.JobFinished, progResp.CountTotal, progResp.CountCompleted,
            progResp.CountForecast, progResp.CountLive, progResp.CountFailed,
        )

        for _, v := range progResp.Venues {
            if _, dup := seenIDs[v.VenueID]; dup {
                log.Printf("[VenuesRefresherService] Skipping duplicate venue ID=%s", v.VenueID)
                continue
            }
            if _, dup := seenNames[v.VenueName]; dup {
                log.Printf("[VenuesRefresherService] Skipping duplicate venue Name=%q", v.VenueName)
                continue
            }

            seenIDs[v.VenueID] = struct{}{}
            seenNames[v.VenueName] = struct{}{}
            uniqueIDs = append(uniqueIDs, v.VenueID)

            log.Printf("[VenuesRefresherService] Upserting venue id=%s name=%q", v.VenueID, v.VenueName)
            if err := vr.venueDao.UpsertVenue(v); err != nil {
                log.Printf("[VenuesRefresherService] Upsert failed for %s: %v", v.VenueID, err)
            } else {
                log.Printf("[VenuesRefresherService] Successfully upserted venue %s", v.VenueID)
            }
        }
    }

    return uniqueIDs
}

// services/venues_refresher_service.go
func (vr *VenuesRefresherService) fetchAndCacheLiveForecasts(ids []string) {
    log.Printf("[VenuesRefresherService] Fetching live forecasts for %d venues", len(ids))
    for _, vid := range ids {
        log.Printf("[VenuesRefresherService] Fetching live forecast for venue_id=%s", vid)
        lf, err := vr.bestTimeAPI.GetLiveForecast(vid, "", "")
        if err != nil {
            log.Printf("[VenuesRefresherService] GetLiveForecast failed for %s: %v", vid, err)
            continue
        }

        // if status not OK, delete stale cache entry
        if lf.Status != "OK" {
            log.Printf("[VenuesRefresherService] LiveForecast status=%q for %s, removing cache", lf.Status, vid)
            if err := vr.venueDao.DeleteLiveForecast(vid); err != nil {
                log.Printf("[VenuesRefresherService] Failed to delete stale live forecast for %s: %v", vid, err)
            }
            continue
        }

        log.Printf("[VenuesRefresherService] Caching live forecast for venue_id=%s", vid)
        if err := vr.venueDao.SetLiveForecast(lf); err != nil {
            log.Printf("[VenuesRefresherService] SetLiveForecast failed for %s: %v", vid, err)
        } else {
            log.Printf("[VenuesRefresherService] Live forecast cached for venue_id=%s", vid)
        }
    }
}

func (vr *VenuesRefresherService) RefreshCachedLiveForecasts() error {
    ids, err := vr.venueDao.ListCachedLiveForecastVenueIDs()
    if err != nil {
        log.Printf("[VenuesRefresherService] Error listing cached live-forecast IDs: %v", err)
        return err
    }
    log.Printf("[VenuesRefresherService] Found %d cached live-forecast entries", len(ids))

    vr.fetchAndCacheLiveForecasts(ids)
    return nil
}
