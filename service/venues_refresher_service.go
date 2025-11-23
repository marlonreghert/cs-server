package services

import (
	"log"
	"time"

	"cs-server/api/besttime"
	"cs-server/config"
	"cs-server/dao/redis"
	"cs-server/models"
	"cs-server/models/venue"
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

// -----------------------------------------------------------------------------
// Default locations (edit manually as needed)
// -----------------------------------------------------------------------------

var defaultLocations = []Location{
	// Example enabled:
	{
		// Centro (Recife)
		Lat: -8.059297,
		Lng: -34.880373,
	},
	{ Lat: -8.098632,  Lng: -34.884890416 }, // Pina
	{ Lat: -8.121918,  Lng: -34.903602    }, // Boa Viagem
	{ Lat: -8.060852,  Lng: -34.910644    }, // ZN / Cordeiro
	{ Lat: -8.004132,  Lng: -34.854365    }, // Olinda / Sé
    { Lat: -8.029736,  Lng: -34.870261    }, // Olinda / Salgadinho
	{ Lat: -8.047251,  Lng: -34.939524    }, // Várzea
    // Examples left commented for convenience:
	// { Lat: -23.558037, Lng: -46.700183    }, // SP / Pinheiros
	// { Lat: -23.567292, Lng: -46.677463    }, // SP / Jardim América
	// { Lat: -23.556218, Lng: -46.665451    }, // SP / Augusta
	// { Lat: -23.542361, Lng: -46.655989    }, // SP / Santa Cecília
}

// -----------------------------------------------------------------------------
// Service
// -----------------------------------------------------------------------------

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

// -----------------------------------------------------------------------------
// Legacy search-based flow (Venue Search + Progress) — split into 3 steps
// -----------------------------------------------------------------------------

// RefreshVenuesData orchestrates the four steps: kick-off, wait, process, live-fetch+cache.
func (vr *VenuesRefresherService) RefreshVenuesData(waitBeforePolling bool) error {
	// 1) Kick off searches
	handles := vr.collectJobHandles()
	if len(handles) == 0 {
		log.Println("[VenuesRefresherService] No successful searches to poll; exiting.")
		return nil
	}

	// 2) Optional wait
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

// waitBeforePolling sleeps for the configured polling interval (growing by attempt).
func (vr *VenuesRefresherService) waitBeforePolling(attemptNumber int) {
	wait := time.Duration(config.BEST_TIME_SEARCH_POLLING_WAIT_SECONDS*attemptNumber) * time.Second
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

			log.Printf("[VenuesRefresherService] Job %s not finished yet (attempt %d/%d), waiting to retry...",
				h.JobID, i+1, maxRetries)
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

// -----------------------------------------------------------------------------
// Live forecast helpers
// -----------------------------------------------------------------------------

func (vr *VenuesRefresherService) fetchAndCacheLiveForecasts(ids []string) {
	log.Printf("[VenuesRefresherService] Fetching live forecasts for %d venues", len(ids))
	for _, vid := range ids {
		log.Printf("[VenuesRefresherService] Fetching live forecast for venue_id=%s", vid)
		lf, err := vr.bestTimeAPI.GetLiveForecast(vid, "", "")
		if err != nil {
			log.Printf("[VenuesRefresherService] GetLiveForecast failed for %s: %v", vid, err)
			continue
		}

		// if status not OK or live data is not avialable (perharps venue is closed) delete stale cache entry
		if lf.Status != "OK" || !lf.Analysis.VenueLiveBusynessAvailable {
            if lf.Status != "OK" {
                log.Printf("[VenuesRefresherService] Error LiveForecast status=%q for %s, removing cache", lf.Status, vid)
            } else {
                log.Printf("[VenuesRefresherService] No error but LiveForecast not available, maybe vneue is closed, for %s, removing cache", vid)
            }
			
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

// RefreshVenueCatalog performs steps 1–3: kick-off, wait (optional), poll+upsert.
// It returns the unique venue IDs discovered/processed during this run.
func (vr *VenuesRefresherService) RefreshVenueCatalog(waitBeforePolling bool) ([]string, error) {
	handles := vr.collectJobHandles()
	if len(handles) == 0 {
		log.Println("[VenuesRefresherService] No successful searches to poll; exiting.")
		return nil, nil
	}

	if waitBeforePolling {
		vr.waitBeforePolling(1)
	}

	ids := vr.processJobHandles(handles)
	return ids, nil
}

// RefreshLiveForecastsForAllVenues loads all known venue IDs from Redis and refreshes their live forecasts.
func (vr *VenuesRefresherService) RefreshLiveForecastsForAllVenues() error {
	ids, err := vr.venueDao.ListAllVenueIDs()
	if err != nil {
		log.Printf("[VenuesRefresherService] ListAllVenueIDs failed: %v", err)
		return err
	}
	log.Printf("[VenuesRefresherService] Found %d venues in geo cache; refreshing live forecasts.", len(ids))
	vr.fetchAndCacheLiveForecasts(ids)
	return nil
}

// Starts the discovery/catalog job (steps 1–3) on its own schedule.
func (vr *VenuesRefresherService) StartVenueDiscoveryJob(interval time.Duration, waitBeforePolling bool) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			log.Println("[VenuesRefresherService] Running VenueDiscovery job.")
			if _, err := vr.RefreshVenueCatalog(waitBeforePolling); err != nil {
				log.Printf("[VenuesRefresherService] VenueDiscovery error: %v", err)
			} else {
				log.Println("[VenuesRefresherService] VenueDiscovery finished.")
			}
		}
	}()
}

// Starts the live-forecast refresh job (step 4) on its own schedule.
func (vr *VenuesRefresherService) StartLiveForecastRefreshJob(interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			log.Println("[VenuesRefresherService] Running LiveForecastRefresh job.")
			if err := vr.RefreshLiveForecastsForAllVenues(); err != nil {
				log.Printf("[VenuesRefresherService] LiveForecastRefresh error: %v", err)
			} else {
				log.Println("[VenuesRefresherService] LiveForecastRefresh finished.")
			}
		}
	}()
}

// -----------------------------------------------------------------------------
// Venue Filter flow (new endpoint) — single-shot refresh
// -----------------------------------------------------------------------------

// RefreshVenuesDataByVenuesFilter queries BestTime /venues/filter using the given
// parameters, de-duplicates venues by ID/Name, upserts them into Redis, and
// optionally fetches/caches live forecasts for the unique IDs.
// Returns the unique venue IDs processed in this run.
func (vr *VenuesRefresherService) RefreshVenuesDataByVenuesFilter(
	params models.VenueFilterParams,
	fetchAndCacheLive bool,
) ([]string, error) {

	log.Printf("[VenuesRefresherService] VenueFilter start: params=%+v", params)
	resp, err := vr.bestTimeAPI.VenueFilter(params)
	if err != nil {
		log.Printf("[VenuesRefresherService] VenueFilter error: %v", err)
		return nil, err
	}
	log.Printf("[VenuesRefresherService] VenueFilter status=%s venues_n=%d", resp.Status, resp.VenuesN)

	// Guard: non-OK stops gracefully
	if resp.Status != "OK" {
		log.Printf("[VenuesRefresherService] VenueFilter returned non-OK status=%s; aborting upsert.", resp.Status)
		return nil, nil
	}

	// De-duplication maps
	seenIDs := make(map[string]struct{})
	seenNames := make(map[string]struct{})
	uniqueIDs := make([]string, 0, len(resp.Venues))

	// Upsert all venues
	for _, vf := range resp.Venues { // vf is venue.Venue
		if vf.VenueID == "" && vf.VenueName == "" {
			log.Printf("[VenuesRefresherService] Skipping venue with no id and no name: %+v", vf)
			continue
		}

		if vf.VenueID != "" {
			if _, dup := seenIDs[vf.VenueID]; dup {
				log.Printf("[VenuesRefresherService] Skipping duplicate venue ID=%s", vf.VenueID)
				continue
			}
		}
		if vf.VenueName != "" {
			if _, dup := seenNames[vf.VenueName]; dup {
				log.Printf("[VenuesRefresherService] Skipping duplicate venue Name=%q", vf.VenueName)
				continue
			}
		}

		// Convert (currently a pass-through)
		v := mapVenueFilterVenueToVenue(vf)

		log.Printf("[VenuesRefresherService] Upserting venue id=%s name=%q lat=%.6f lng=%.6f",
			v.VenueID, v.VenueName, v.VenueLat, v.VenueLon)
		if err := vr.venueDao.UpsertVenue(v); err != nil {
			log.Printf("[VenuesRefresherService] Upsert failed for %s: %v", v.VenueID, err)
			continue
		}

		if v.VenueID != "" {
			seenIDs[v.VenueID] = struct{}{}
			uniqueIDs = append(uniqueIDs, v.VenueID)
		}
		if v.VenueName != "" {
			seenNames[v.VenueName] = struct{}{}
		}
	}

	log.Printf("[VenuesRefresherService] Upserted %d unique venues via VenueFilter", len(uniqueIDs))

	// Optionally fetch and cache live forecasts
	if fetchAndCacheLive && len(uniqueIDs) > 0 {
		log.Println("[VenuesRefresherService] Fetching and caching venues live forecasts.")
		vr.fetchAndCacheLiveForecasts(uniqueIDs)
	} else {
		log.Println("[VenuesRefresherService] Skipping live forecast fetch (disabled or no venues).")
	}

	return uniqueIDs, nil
}

// mapVenueFilterVenueToVenue converts a venue.Venue coming from VenueFilter
// into your persisted venue.Venue struct used by Redis (currently the same shape).
func mapVenueFilterVenueToVenue(vf venue.Venue) venue.Venue {
	return venue.Venue{
		Forecast:                 true,
		Processed:                true,
		VenueAddress:             vf.VenueAddress,
		VenueLat:                 vf.VenueLat,
		VenueLon:                 vf.VenueLon,
		VenueName:                vf.VenueName,
		VenueID:                  vf.VenueID,
		VenueFootTrafficForecast: nil,
	}
}

// -----------------------------------------------------------------------------
// Multi-location Venue Filter runner
// -----------------------------------------------------------------------------

// StartVenueFilterMultiLocationJob runs VenueFilter for the default locations on a schedule.
func (vr *VenuesRefresherService) StartVenueFilterMultiLocationJob(interval time.Duration, fetchLive bool) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			log.Println("[VenuesRefresherService] Running multi-location VenueFilter job.")
			vr.RefreshVenuesByFilterForDefaultLocations(fetchLive)
		}
	}()
}

// RefreshVenuesByFilterForDefaultLocations iterates through all default locations,
// calls RefreshVenuesDataByVenuesFilter() for each one with fixed parameters,
// and logs results for each region.
func (vr *VenuesRefresherService) RefreshVenuesByFilterForDefaultLocations(fetchAndCacheLive bool) {
	log.Printf("[VenuesRefresherService] Starting VenueFilter refresh for %d default locations", len(defaultLocations))

	min := 1
	live := true
    // now := false
	limit := 20   // let client-side limit; API warns busy_* filters apply after limit
	radius := 10000 // meters

	totalInserted := 0

	for _, loc := range defaultLocations {
		log.Printf("[VenuesRefresherService] VenueFilter refresh at lat=%.6f, lng=%.6f", loc.Lat, loc.Lng)

		lat := loc.Lat
		lng := loc.Lng

		params := models.VenueFilterParams{
			// BusyMin:     &min,
			Live:        &live,
			Lat:         &lat,
			Lng:         &lng,
			Radius:      &radius,
			FootTraffic: "both",
			Limit:       &limit,
            // Now:         &now,
			// Types removed to increase response accuracy per BestTime API
		}

		ids, err := vr.RefreshVenuesDataByVenuesFilter(params, fetchAndCacheLive)
		if err != nil {
			log.Printf("[VenuesRefresherService] VenueFilter refresh failed for lat=%.6f, lng=%.6f: %v",
				loc.Lat, loc.Lng, err)
			continue
		}

		log.Printf("[VenuesRefresherService] Successfully upserted %d venues for lat=%.6f, lng=%.6f",
			len(ids), loc.Lat, loc.Lng)
		totalInserted += len(ids)
	}

	log.Printf("[VenuesRefresherService] Finished VenueFilter refresh for all locations; total venues upserted=%d",
		totalInserted)
}
