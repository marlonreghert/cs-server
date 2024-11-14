package services

import (
	"cs-server/dao/redis"
	"log"
	"time"
)

type VenuesRefresherService struct {
	venueDao      *redis.RedisVenueDAO
	venuesService *VenueService
}

// NewVenueService constructs a new VenueService with Redis dependency injection.
func NewVenuesRefresherService(
	venueDao *redis.RedisVenueDAO,
	venuesService *VenueService) *VenuesRefresherService {

	return &VenuesRefresherService{
		venueDao:      venueDao,
		venuesService: venuesService,
	}
}

// StartPeriodicJob run a periodic job that fetches all venues ids currently stored and upsert their data (fire & forget call)
func (vr *VenuesRefresherService) StartPeriodicJob(interval time.Duration) {
	go vr.startPeriodicJob(interval)
}

func (vr *VenuesRefresherService) startPeriodicJob(interval time.Duration) {
	ticker := time.NewTicker(interval) // Change interval as needed
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			log.Println("Running periodic venues refresher job.")
			vr.RefreshVenuesData()
		}
	}
}

func (vr *VenuesRefresherService) RefreshVenuesData() error {
	venuesIds, err := vr.venuesService.GetAllVenuesIds()

	if err != nil {
		log.Fatal("Could not fetch all venues ids " + err.Error())
		return err
	} else {
		for _, venueId := range venuesIds {
			/*
					TODO: Think about:
					- 1 fails vs if all fails
					- maximum time a venue can be stale with no refresh
					- telemetry / icm management
				currently: fire & forget
			*/
			err := vr.handleVenueRefresh(venueId)
			if err != nil {
				log.Print("An error happened while handling venue refresh for = " + venueId + " - error: " + err.Error())
			}
		}
	}

	return nil
}

func (vr *VenuesRefresherService) handleVenueRefresh(venueId string) error {
	venue, err := vr.venuesService.GetVenue(venueId)
	if err != nil {
		log.Print("Could not fetch latest data for venue with id = " + venueId + " - error: " + err.Error())
		return err
	}

	err = vr.venuesService.venueDao.UpsertVenue(*venue)
	if err != nil {
		log.Print("Could not update = " + venueId + " - error: " + err.Error())
		return err
	}

	return nil
}
