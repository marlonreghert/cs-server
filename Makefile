# Variables
IMAGE_NAME=johnsummit2024/cs-server
IMAGE_TAG=2026_02_09_18_09_py
NETWORK_NAME=cs-server-docker-network
REDIS_CONTAINER=redis-container-2
CS_SERVER_CONTAINER=cs-server-2
KUBERNETES_DEPLOYMENT=deployment/deployment.yaml
KUBERNETES_CLUSTER=cs-server-cluster
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

setup-root:
	export PROJECT_ROOT=`pwd`

# Phony targets to avoid conflicts with files of the same name
.PHONY: build push network run-network run-docker-compose request clean test-unit test-integration test-bdd test-feature test

test-unit:
	$(PYTHON) -m pytest \
		tests/test_models.py \
		tests/test_redis_dao_unit.py \
		tests/test_besttime_client.py \
		tests/test_services.py \
		tests/test_handlers.py \
		tests/test_google_places_soft_delete.py \
		tests/test_google_places_instagram_validation.py \
		tests/test_google_places_search_place_id.py \
		tests/test_job_lock.py \
		tests/test_batch_add_service.py \
		tests/test_admin_venue_inventory.py \
		tests/test_instagram_enrichment_service.py \
		tests/test_instagram_validator.py \
		tests/test_venue_budget.py \
		tests/test_priority_bounded_refresh.py \
		tests/test_rds_venue_store.py \
		tests/test_add_venue_handler.py \
		tests/test_besttime_inventory_sync.py \
		tests/test_venue_eligibility.py \
		tests/test_venue_category.py \
		tests/test_venue_type_override.py \
		tests/test_rds_repository.py \
		tests/test_rds_store_contract.py \
		tests/test_admin_config.py \
		tests/test_force_update_validator.py \
		tests/test_vibe_modes_config.py \
		tests/test_config_validation.py \
		tests/test_redis_projection.py \
		tests/test_eligibility_serving_view_parity.py \
		tests/test_reactivation_migration.py \
		tests/test_widen_alembic_version.py \
		tests/test_hot_like_event_idempotency_migration.py \
		tests/test_venue_row.py \
		tests/test_equivalence_verify.py \
		tests/test_address_table.py \
		tests/test_eligibility_rules.py \
		tests/test_log_redaction.py \
		tests/test_refresh_interval_watch.py \
		tests/test_user_activity_tracking.py \
		tests/test_price_signal.py \
		tests/test_google_places_review_signal_backfill.py \
		tests/test_live_freshness.py \
		tests/test_photo_resolve.py \
		tests/test_projector_and_serving_bulk_reads.py \
		tests/test_datalake_writer.py \
		tests/test_datalake_redaction.py \
		tests/test_besttime_client_datalake_tap.py \
		tests/test_datalake_client_credentials.py \
		tests/test_venue_photo_archive.py \
		tests/test_instagram_cascade.py \
		tests/test_pipeline_run_registry.py \
		tests/test_website_uri_persistence.py \
		tests/test_photo_archive_pipeline_v2.py \
		tests/test_archive_sources.py \
		tests/test_apify_instagram_media.py \
		tests/test_menu_extraction_from_archive.py \
		tests/test_photo_metadata_fidelity.py \
		tests/test_apify_profile_parsing.py \
		tests/test_instagram_cascade_run_scope.py \
		tests/test_instagram_probe_fail_open.py \
		tests/test_cascade_scoring_without_probe.py \
		tests/test_venue_website_source.py \
		tests/test_venue_name_matching.py \
		tests/test_judge_adjudication_band.py \
		tests/test_container_judge_wiring.py \
		tests/test_openai_call_shape.py \
		tests/test_google_search_cannot_self_accept.py \
		tests/test_container_google_search_wiring.py \
		tests/test_events_schema_migration.py \
		tests/test_event_caption_matcher.py \
		tests/test_event_venue_targeting.py \
		tests/test_event_date_resolver.py \
		tests/test_event_table_migration.py \
		tests/test_event_extraction_service.py \
		tests/test_promoter_accounts_migration.py \
		tests/test_event_venue_resolution.py \
		tests/test_promoter_registry_service.py \
		tests/test_promoter_crawl_service.py \
		tests/test_event_identity.py \
		tests/test_multi_event_extraction.py \
		tests/test_multi_event_posts_migration.py \
		tests/test_event_cover_presign.py \
		tests/test_event_source_media.py \
		tests/test_event_reconciliation.py \
		tests/test_event_merge.py \
		tests/test_event_merge_handle_identity.py \
		tests/test_event_dedup.py \
		tests/test_event_dedup_merge.py \
		tests/test_backfill_event_venue_links.py \
		tests/test_backfill_source_provenance.py \
		tests/test_repair_event_dates.py \
		tests/test_event_sources_migration.py \
		tests/test_post_items_migration.py \
		tests/test_post_category.py \
		tests/test_photo_classification.py \
		tests/test_classification_batch_retry.py \
		tests/test_photo_classifier_error_handling.py \
		tests/test_review_queue_completeness.py \
		tests/test_operator_edited_fields_migration.py \
		tests/test_operator_edited_fields_patch.py \
		tests/test_event_ticket_info_and_attractions_migration.py \
		tests/test_blocked_venues_migration.py \
		tests/test_engagement_blocked_venues.py \
		tests/test_crawl_target_migration.py \
		tests/test_crawl_target_seed_cap_migration.py \
		tests/test_crawl_target_reels_caps_migration.py \
		tests/test_crawl_target_reels_overlap_migration.py \
		tests/test_crawl_target_posts_dormant_migration.py \
		tests/test_crawl_target_dao.py \
		tests/test_instagram_crawl_service.py \
		tests/test_extract_by_handle.py \
		tests/test_crawl_schedule_sync.py \
		tests/test_admin_crawl_router.py \
		tests/test_scheduler.py \
		tests/test_time_known_migration.py \
		tests/test_expose_time_known.py \
		tests/test_menu_item_lifecycle.py \
		tests/test_source_media_type_migration.py \
		tests/test_promoter_event_visibility.py \
		-v

test-integration:
	$(PYTHON) -m pytest tests/test_redis_dao.py -v

test-bdd:
	@if ! find tests/bdd -name '*.feature' -print -quit | grep -q .; then \
		echo "No feature files found under tests/bdd/. Skipping BDD suite."; \
	else \
		if ! $(PYTHON) -c "import behave" >/dev/null 2>&1; then \
			echo "behave is not installed. Run: .venv/bin/python -m pip install -r requirements-dev.txt"; \
			exit 1; \
		fi; \
		$(PYTHON) -m behave; \
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
	$(PYTHON) -m behave "$(FEATURE)"

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
