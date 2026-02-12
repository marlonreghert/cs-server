# Variables
IMAGE_NAME=johnsummit2024/cs-server
IMAGE_TAG=2026_02_09_18_09_py
NETWORK_NAME=cs-server-docker-network
REDIS_CONTAINER=redis-container-2
CS_SERVER_CONTAINER=cs-server-2
KUBERNETES_DEPLOYMENT=deployment/deployment.yaml
KUBERNETES_CLUSTER=cs-server-cluster

setup-root:
	export PROJECT_ROOT=`pwd`

# Phony targets to avoid conflicts with files of the same name
.PHONY: build image network run-network k8s-deploy k8s-scale-down k8s-port-forward k8s-logs request clean

# Build the Docker image
build:
	docker buildx build --platform=linux/amd64,linux/arm64 --no-cache -t $(IMAGE_NAME):$(IMAGE_TAG) .  --push

# Push the Docker image to the registry
push:
	docker push $(IMAGE_NAME):$(IMAGE_TAG)

# Create a Docker network
network:
	docker network create $(NETWORK_NAME)

# Run Redis and the server in the Docker network
run-network:
	docker run -d --network $(NETWORK_NAME) -p 6379:6379 redis
	docker run -d --network $(NETWORK_NAME) -p 8080:8080 $(IMAGE_NAME)

run-docker-compose:
	docker-compose down -v
	docker-compose up -d

# Send a request to the server
request:
	curl -XGET "localhost:8080/v1/venues/nearby?lat=-8.1037988&lon=-34.8734516&radius=10" | grep -v curl | jq .

# Clean up Docker containers and network
clean:
	docker rm -f $(REDIS_CONTAINER) $(CS_SERVER_CONTAINER) || true
	docker network rm $(NETWORK_NAME) || true

