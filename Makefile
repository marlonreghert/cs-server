# Variables
IMAGE_NAME=johnsummit2024/cs-server
IMAGE_TAG=latest
NETWORK_NAME=cs-server-docker-network
REDIS_CONTAINER=redis-container-2
CS_SERVER_CONTAINER=cs-server-2
KUBERNETES_DEPLOYMENT=deployment/deployment.yaml
KUBERNETES_CLUSTER=cs-server-cluster

# Phony targets to avoid conflicts with files of the same name
.PHONY: build image network run-network k8s-deploy k8s-scale-down k8s-port-forward k8s-logs request clean

# Build the Docker image
build:
	docker build --no-cache -t $(IMAGE_NAME):$(IMAGE_TAG) .

# Push the Docker image to the registry
image:
	docker push $(IMAGE_NAME):$(IMAGE_TAG)

# Create a Docker network
network:
	docker network create $(NETWORK_NAME)

# Run Redis and the server in the Docker network
run-network:
	docker run -d --name $(REDIS_CONTAINER) --network $(NETWORK_NAME) -p 6379:6379 redis
	docker run -d --name $(CS_SERVER_CONTAINER) --network $(NETWORK_NAME) -p 8080:8080 $(IMAGE_NAME)

# Start a local Kubernetes cluster using minikube
k8s-start:
	minikube start

# Deploy the application on Kubernetes
k8s-deploy:
	kubectl apply -f $(KUBERNETES_DEPLOYMENT)

# Scale down the Kubernetes deployment to zero replicas
k8s-scale-down:
	kubectl scale deployment $(KUBERNETES_CLUSTER) --replicas=0

# Scale down the Kubernetes deployment to zero replicas
k8s-scale-one:
	kubectl scale deployment $(KUBERNETES_CLUSTER) --replicas=1

# Port-forward the Kubernetes deployment to access it locally
k8s-port-forward:
	kubectl port-forward deployment/$(KUBERNETES_CLUSTER) 8080:8080

# Get logs from the Kubernetes deployment
k8s-logs:
	kubectl logs $(KUBERNETES_CLUSTER)-cb5787bfb-qn85v -c cs-server

# Send a request to the server
request:
	curl -XGET "localhost:8080/v1/venues/nearby?lat=45.5204001&lon=-73.5540803&radius=1"

# Clean up Docker containers and network
clean:
	docker rm -f $(REDIS_CONTAINER) $(CS_SERVER_CONTAINER) || true
	docker network rm $(NETWORK_NAME) || true

