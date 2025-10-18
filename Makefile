# Variables
IMAGE_NAME=johnsummit2024/cs-server
IMAGE_TAG=latest
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
	docker buildx build --platform=linux/amd64 --no-cache -t $(IMAGE_NAME):$(IMAGE_TAG) .

# Push the Docker image to the registry
push:
	docker push $(IMAGE_NAME):$(IMAGE_TAG)

# Create a Docker network
network:
	docker network create $(NETWORK_NAME)

# Run Redis and the server in the Docker network
run-network:
	docker run -d --name $(REDIS_CONTAINER) --network $(NETWORK_NAME) -p 6379:6379 redis
	docker run -d --name $(CS_SERVER_CONTAINER) --network $(NETWORK_NAME) -p 8080:8080 $(IMAGE_NAME)

run-docker-compose:
	docker-compose down -v
	docker-compose up -d

# Start a local Kubernetes cluster using minikube
k8s-start:
	minikube start

# Deploy the application on Kubernetes
k8s-deploy:
	kubectl apply -f $(KUBERNETES_DEPLOYMENT)

# Scale down the Kubernetes deployment to zero replicas
k8s-scale-down:
	kubectl scale deployment $(KUBERNETES_CLUSTER) --replicas=0

# Scale the Kubernetes deployment to 1 replicas
k8s-scale-one:
	kubectl scale deployment $(KUBERNETES_CLUSTER) --replicas=1

# Port-forward the Kubernetes deployment to access it locally
k8s-pf-web:
	kubectl port-forward deployment/$(KUBERNETES_CLUSTER) 8080:8080

# Port-forward the Kubernetes deployment to access it locally
k8s-pf-redis:
	kubectl port-forward deployment/$(KUBERNETES_CLUSTER) 6379:6379

# Get logs from the Kubernetes deployment
k8s-logs:
	kubectl logs $(KUBERNETES_CLUSTER)-cb5787bfb-qn85v -c cs-server

# Send a request to the server
request:
	curl -XGET "localhost:8080/v1/venues/nearby?lat=-8.1037988&lon=-34.8734516&radius=10"

# Clean up Docker containers and network
clean:
	docker rm -f $(REDIS_CONTAINER) $(CS_SERVER_CONTAINER) || true
	docker network rm $(NETWORK_NAME) || true

