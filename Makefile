# Variables
IMAGE_NAME=johnsummit2024/cs-server
IMAGE_TAG=2026_02_09_18_09_py
NETWORK_NAME=cs-server-docker-network
REDIS_CONTAINER=redis-container-2
CS_SERVER_CONTAINER=cs-server-2
KUBERNETES_DEPLOYMENT=deployment/deployment.yaml
KUBERNETES_CLUSTER=cs-server-cluster
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

# Test reporting convention (standard across the three VibeSense repos):
# every test target writes its full verbose output to tests/reports/<target>.txt
# (gitignored, overwritten per run) so failures are analyzed from the report
# instead of rerunning the suite. pipefail keeps exit codes intact through tee.
SHELL := /bin/bash
REPORTS := tests/reports
BEHAVE_FLAGS := -f plain --no-capture --no-capture-stderr --no-logcapture
FEATURE_SLUG = $(basename $(notdir $(FEATURE)))
TAGS_SLUG = $(shell echo '$(TAGS)' | tr -c 'A-Za-z0-9\n' '-' | sed -e 's/^-*//' -e 's/-*$$//')

setup-root:
	export PROJECT_ROOT=`pwd`

# Phony targets to avoid conflicts with files of the same name
.PHONY: build push network run-network run-docker-compose request clean test-unit test-integration test-bdd test-feature test-tags test

test-unit:
	@mkdir -p $(REPORTS)
	@set -o pipefail; $(PYTHON) -m pytest \
		tests/test_models.py \
		tests/test_redis_dao_unit.py \
		tests/test_besttime_client.py \
		tests/test_services.py \
		tests/test_handlers.py \
		tests/test_google_places_soft_delete.py \
		tests/test_admin_venue_inventory.py \
		tests/test_instagram_enrichment_service.py \
		tests/test_instagram_validator.py \
		tests/test_venue_budget.py \
		tests/test_priority_bounded_refresh.py \
		tests/test_add_venue_handler.py \
		tests/test_besttime_inventory_sync.py \
		tests/test_venue_eligibility.py \
		tests/test_rds_repository.py \
		tests/test_rds_store_contract.py \
		tests/test_admin_config.py \
		tests/test_redis_projection.py \
		tests/test_venue_row.py \
		tests/test_equivalence_verify.py \
		tests/test_address_table.py \
		tests/test_eligibility_rules.py \
		tests/test_log_redaction.py \
		-v -ra 2>&1 | tee $(REPORTS)/test-unit.txt

test-integration:
	@mkdir -p $(REPORTS)
	@set -o pipefail; $(PYTHON) -m pytest tests/test_redis_dao.py -v -ra 2>&1 | tee $(REPORTS)/test-integration.txt

test-bdd:
	@if ! find tests/bdd -name '*.feature' -print -quit | grep -q .; then \
		echo "No feature files found under tests/bdd/. Skipping BDD suite."; \
	else \
		if ! $(PYTHON) -c "import behave" >/dev/null 2>&1; then \
			echo "behave is not installed. Run: .venv/bin/python -m pip install -r requirements-dev.txt"; \
			exit 1; \
		fi; \
		mkdir -p $(REPORTS); \
		set -o pipefail; \
		$(PYTHON) -m behave $(BEHAVE_FLAGS) --tags=-@wip 2>&1 | tee $(REPORTS)/test-bdd.txt; \
	fi

test-feature:
	@if [ -z "$(FEATURE)" ]; then \
		echo "FEATURE is required. Usage: make test-feature FEATURE=tests/bdd/<domain>/<slug>.feature"; \
		exit 2; \
	fi
	@if [ ! -f "$(FEATURE)" ]; then \
		echo "Feature file not found: $(FEATURE)"; \
		exit 2; \
	fi
	@if ! $(PYTHON) -c "import behave" >/dev/null 2>&1; then \
		echo "behave is not installed. Run: .venv/bin/python -m pip install -r requirements-dev.txt"; \
		exit 1; \
	fi
	@mkdir -p $(REPORTS)
	@set -o pipefail; $(PYTHON) -m behave $(BEHAVE_FLAGS) "$(FEATURE)" 2>&1 | tee "$(REPORTS)/test-feature-$(FEATURE_SLUG).txt"

# Tag-filtered BDD run (behave tag expression), e.g.:
#   make test-tags TAGS=@persistence
#   make test-tags TAGS=@api,@refresh
test-tags:
	@if [ -z "$(TAGS)" ]; then \
		echo "TAGS is required. Usage: make test-tags TAGS=@<tag>[,@<tag>]"; \
		exit 2; \
	fi
	@if ! $(PYTHON) -c "import behave" >/dev/null 2>&1; then \
		echo "behave is not installed. Run: .venv/bin/python -m pip install -r requirements-dev.txt"; \
		exit 1; \
	fi
	@mkdir -p $(REPORTS)
	@set -o pipefail; $(PYTHON) -m behave $(BEHAVE_FLAGS) --tags="$(TAGS)" 2>&1 | tee "$(REPORTS)/test-tags-$(TAGS_SLUG).txt"

test: test-unit test-bdd

# Build the Docker image
build:
	docker buildx build --platform=linux/amd64,linux/arm64 --no-cache -t $(IMAGE_NAME):$(IMAGE_TAG) . 

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
	docker-compose up -d

# Send a request to the server
request:
	curl -XGET "localhost:8080/v1/venues/nearby?lat=-8.1037988&lon=-34.8734516&radius=10" | grep -v curl | jq .

# Clean up Docker containers and network
clean:
	docker rm -f $(REDIS_CONTAINER) $(CS_SERVER_CONTAINER) || true
	docker network rm $(NETWORK_NAME) || true
