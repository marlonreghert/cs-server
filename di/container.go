package di

import (
	"context"
	"cs-server/api/besttime"
	"cs-server/config"
	"cs-server/dao/redis"
	"cs-server/db"
	"cs-server/server"
	"cs-server/server/handlers"
	"cs-server/api"
	"log"
	services "cs-server/service"
	"fmt"
	goredis "github.com/go-redis/redis/v8"
	"github.com/gorilla/mux"
)

// Container holds all application dependencies.
type Container struct {
	RedisClient            db.RedisClient
	RedisVenueDao          *redis.RedisVenueDAO
	VenueService           *services.VenueService
	BestTimeAPI            besttime.BestTimeAPI
	VenueHandler           *handlers.VenueHandler
	MuxRouter              *mux.Router
	Router                 *server.Router
	CrowdSenseHttpServer   *server.CrowdSenseHttpServer
	VenuesRefresherService *services.VenuesRefresherService
}

// NewContainer initializes and wires up all dependencies.
func NewContainer(env string) *Container {
	log.Printf("initializing container - env: %s", env)
	// Initialize Redis Client internals
	ctx := context.Background()

	redisInternalClient := goredis.NewClient(&goredis.Options{
		Addr:     config.REDIS_DB_ADDRESS,
		Password: config.REDIS_DB_PASSWORD,
		DB:       config.REDIS_DB,
	})
	// defer redisInternalClient.Close() // Ensure client is closed when the program exits

	// Initialize Redis client
	redisClient := db.NewGeoRedisClient(ctx, redisInternalClient)
	if err := redisClient.Ping(); err != nil {
		panic(fmt.Sprintf("Failed to connect to Redis: %v", err))
	}

	// Initialize Redis Venue DAO
	redisVenueDao := redis.NewRedisVenueDAO(redisClient)

	// Initialize BestTimeApi - using mock for now
	var bestTimeApiClient besttime.BestTimeAPI
	if env != "prod" {
		bestTimeApiClient = besttime.NewBestTimeApiClientMock()
		log.Printf("Using mock best time api")
	} else {

		log.Printf("Using prod best time api")
		httpClient := api.NewHTTPClient(config.BEST_TIME_ENDPOINT_BASE_V1)

		bestTimeApiClient = besttime.NewBestTimeApiClient(httpClient)
		bestTimeApiClient.SetCredentials(config.BEST_TIME_PUBLIC_KEY, config.BEST_TIME_PRIVATE_KEY)
	}
	

	// Initialize service layer with Redis client dependency
	venueService := services.NewVenueService(redisVenueDao, bestTimeApiClient)

	// Initialize venue handler
	venueHandler := handlers.NewVenueHandler(redisVenueDao)

	// Initialize mux router
	muxRouter := mux.NewRouter()

	// Initialize router
	router := server.NewRouter(venueHandler, muxRouter)

	// initialize crowd sense server
	crowdSenseHttpServer := server.NewCrowdSenseHttpServer(router, muxRouter)

	venuesRefresherService := services.NewVenuesRefresherService(redisVenueDao, bestTimeApiClient)

	return &Container{
		RedisClient:            redisClient,
		RedisVenueDao:          redisVenueDao,
		VenueService:           venueService,
		BestTimeAPI:            bestTimeApiClient,
		VenueHandler:           venueHandler,
		MuxRouter:              muxRouter,
		Router:                 router,
		CrowdSenseHttpServer:   crowdSenseHttpServer,
		VenuesRefresherService: venuesRefresherService,
	}
}
