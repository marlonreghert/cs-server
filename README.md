# CS Server

CS Server is a Go-based backend service that periodically reads live venue busyness data from the BestTime API and provides it through a web API. 

---

## Features

- Retrieves and stores venue information, including geolocation, using Redis.
- Caches venue data for efficient retrieval using Redis.
- Exposes venue data via a RESTful API.
- Dockerized and deployed on Kubernetes.

---

## API Endpoints

### Get Nearby Venues
Retrieves venue information within a specified radius of the given latitude and longitude.
```
GET /v1/venues/nearby?lat={latitude(float)}&long={longitude(float)}&radius={radius(float)}
```

### Health Check
A simple endpoint to check if the service is running.
```
GET /ping
```

---

## Tech Stack

- **Go** (version 1.23.0) for backend logic.
- **Redis** for geolocation-based venue storage and caching of venue data.
- **Docker** for containerization.
- **Kubernetes** for deployment and orchestration.

---

## Development

### Requirements

- **Go version**: 1.23.0
- **Docker**
- **Kubernetes**
- **Redis**: Used as a caching layer for faster access to frequently requested data.
- **IDE**: Goland (recommended)

---

### Makefile Commands

The `Makefile` provides commands to build, run, and deploy the application using Docker and Kubernetes. Below are the available commands:

#### Variables

- **`IMAGE_NAME`**: The name of the Docker image (`johnsummit2024/cs-server`).
- **`IMAGE_TAG`**: The tag for the Docker image (`latest`).
- **`NETWORK_NAME`**: The name of the Docker network (`cs-server-docker-network`).
- **`REDIS_CONTAINER`**: The name of the Redis container (`redis-container-2`).
- **`CS_SERVER_CONTAINER`**: The name of the CS Server container (`cs-server-2`).
- **`KUBERNETES_DEPLOYMENT`**: Path to the Kubernetes deployment YAML file (`deployment/deployment.yaml`).
- **`KUBERNETES_CLUSTER`**: The name of the Kubernetes cluster (`cs-server-cluster`).

#### Commands

1. **Build the Docker Image**
   ```bash
   make build
   ```
   Builds the Docker image using the provided `Dockerfile`.

2. **Push the Docker Image**
   ```bash
   make image
   ```
   Pushes the built Docker image to the container registry.

3. **Create Docker Network**
   ```bash
   make network
   ```
   Creates a custom Docker network for inter-container communication.

4. **Run Containers in Docker Network**
   ```bash
   make run-network
   ```
   Starts Redis and CS Server containers in the custom Docker network.

5. **Start Kubernetes Cluster**
   ```bash
   make k8s-start
   ```
   Starts a local Kubernetes cluster using `minikube`.

6. **Deploy to Kubernetes**
   ```bash
   make k8s-deploy
   ```
   Applies the Kubernetes deployment configuration and deploys the application.

7. **Scale Down Kubernetes Deployment**
   ```bash
   make k8s-scale-down
   ```
   Scales the Kubernetes deployment down to zero replicas.

8. **Scale Up Kubernetes Deployment**
   ```bash
   make k8s-scale-one
   ```
   Scales the Kubernetes deployment up to one replica.

9. **Port-Forward Kubernetes Deployment**
   ```bash
   make k8s-port-forward
   ```
   Forwards port 8080 on the local machine to the Kubernetes deployment for local testing.

10. **Get Logs from Kubernetes Deployment**
    ```bash
    make k8s-logs
    ```
    Fetches logs from the Kubernetes deployment for debugging purposes.

11. **Send a Test Request**
    ```bash
    make request
    ```
    Sends a `GET` request to the `/v1/venues/nearby` endpoint to test the application.

12. **Clean Docker Environment**
    ```bash
    make clean
    ```
    Cleans up Docker containers and the custom network.

---

### Manual Build and Run Commands

1. **Build the Application**:
   ```bash
   go build -o cs-server .
   ```

2. **Build the Docker Image**:
   ```bash
   docker build --no-cache -t johnsummit2024/cs-server:latest .
   ```

3. **Push the Docker Image**:
   ```bash
   docker push johnsummit2024/cs-server:latest
   ```

4. **Run the Application Locally**:
   ```bash
   kubectl apply -f deployment/deployment.yaml
   ```

---
