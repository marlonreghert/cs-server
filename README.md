# CS Server

CS Server is a Go-based backend service that periodically reads live venue busyness data from the BestTime API and provides it through a web API. The application is currently using mocked responses.

## Features

- Retrieves and stores venue information, including geolocation, using Redis.
- Caches venue data for efficient retrieval using Redis.
- Exposes venue data via a RESTful API.
- Dockerized and deployed on Kubernetes.

## API Endpoints

- **Get Nearby Venues**: 
  ```
  GET /v1/venues/nearby?lat={latitude(float)}&long={longitude(float)}&radius={radius(float)}
  ```
  Retrieves venue information within a specified radius of the given latitude and longitude.

- **Health Check**:
  ```
  GET /ping
  ```
  A simple endpoint to check if the service is running.

## Tech Stack

- **Go** (version 1.23.0) for backend logic.
- **Redis** for geolocation-based venue storage and caching of venue data.
- **Docker** for containerization.
- **Kubernetes** for deployment and orchestration.

## Development

### Requirements

- **Go version**: 1.23.0
- **Docker**
- **Kubernetes**
- **Redis**: Used as a caching layer for faster access to frequently requested data.
- **IDE**: Goland (recommended)

### Build and Run Commands

1. **Command-line Build**:
   ```bash
   go build -o cs-server .
   ```

2. **Docker Commands**:
   - **To Build**:
     ```bash
     docker build --no-cache -t johnsummit2024/cs-server:latest .
     ```

   - **To Push to Container Registry**:
     ```bash
     docker push johnsummit2024/cs-server:latest
     ```

3. **Running on Kubernetes**:
   ```bash
   kubectl apply -f deployment/deployment.yaml
   ```
---
