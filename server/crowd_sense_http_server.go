package server

import (
	"context"
	"fmt"
	"github.com/gorilla/mux"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

type CrowdSenseHttpServer struct {
	router    *Router
	muxRouter *mux.Router
}

func NewCrowdSenseHttpServer(router *Router, muxRouter *mux.Router) *CrowdSenseHttpServer {
	return &CrowdSenseHttpServer{
		router:    router,
		muxRouter: muxRouter,
	}
}

func (s *CrowdSenseHttpServer) Start() {
	s.router.RegisterRoutes()

	http.ListenAndServe(":8080", s.muxRouter)

	// Define your HTTP server
	srv := &http.Server{
		Addr:    ":8080",
		Handler: s.muxRouter,
	}

	// Channel to listen for interrupt or termination signals
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	// Start the server in a goroutine so it doesn't block
	go func() {
		fmt.Println("Starting server on :8080")
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("ListenAndServe(): %v", err)
		}
	}()

	// Wait for a signal to shut down
	<-stop
	fmt.Println("\nShutting down the server...")

	// Create a deadline for the shutdown (e.g., 5 seconds)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Attempt graceful shutdown
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatalf("Server forced to shutdown: %v", err)
	}

	fmt.Println("Server exiting")
}
