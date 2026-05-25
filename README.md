# CS Server

CS Server is a Python/FastAPI backend service for VibeSense venue discovery and
crowd signals. It reads venue and busyness data from BestTime, enriches venues
with optional Google Places, Apify, S3, and OpenAI workflows, stores geospatial
venue data and caches in Redis, and serves the mobile/API clients through HTTP.

## What It Does

- Serves nearby venues from Redis through FastAPI.
- Caches live and weekly BestTime forecasts.
- Refreshes venue catalog, live forecasts, and weekly forecasts in background
  jobs.
- Optionally enriches venues with Google Places photos/opening hours/vibe
  attributes, Instagram handles, menu photos, menu extraction, and vibe
  classification data.
- Exposes admin job triggers and Prometheus metrics for operational visibility.

## API Endpoints

### Nearby Venues

Returns venues within a radius of a latitude/longitude pair.

```http
GET /v1/venues/nearby?lat={latitude}&lon={longitude}&radius={kilometers}&verbose=false
```

- `lat`: latitude, `-90..90`
- `lon`: longitude, `-180..180`
- `radius`: radius in kilometers, greater than `0`
- `verbose`: when `true`, returns the full venue/live/weekly structure; when
  `false`, returns the minified mobile-facing venue shape

### Health And Metrics

```http
GET /health
GET /ping
GET /metrics
```

### Admin And Debug

```http
POST /admin/trigger/{job_name}
GET /admin/jobs
POST /admin/recount-discovery-points
GET /debug/*
```

Admin/debug endpoints are intended for controlled operational use.

## Tech Stack

- Python 3.13
- FastAPI and Uvicorn
- Pydantic settings and models
- Redis for geospatial venue storage and caches
- APScheduler for background jobs
- Prometheus client metrics
- Pytest for unit/integration tests
- Behave/Gherkin for BDD feature contracts
- Docker and Docker Compose for local service orchestration

## Local Development

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
```

Run Redis and the service with Docker Compose:

```bash
docker-compose up -d
```

Run the app directly against your configured Redis:

```bash
export REDIS_HOST=localhost
export REDIS_PORT=6379
export REFRESH_ON_STARTUP=false
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080
```

When running locally with startup refresh enabled, use dev mode to limit API
calls and venue count:

```bash
export DEV_MODE=true
export FETCH_VENUE_TOTAL_LIMIT=10
export REFRESH_ON_STARTUP=true
```

Dev mode uses a single Recife location by default through `DEV_LAT`, `DEV_LNG`,
and `DEV_RADIUS`.

## Configuration

Settings are loaded by `app/config.py` with this precedence:

1. Environment variables
2. JSON config file referenced by `CONFIG_FILE`
3. Defaults in `Settings`

Use these files as starting points:

- `.env.example` for environment-variable shape
- `config.example.json` for nested JSON config
- `docker-compose.yml` for containerized local wiring

Optional enrichment paths are disabled unless their feature flags and required
credentials are present.

## Enrichment Jobs

These jobs can be triggered from the admin panel or the admin trigger endpoint
when their dependencies are configured:

| Job | Description |
| --- | --- |
| `venue_catalog` | Fetch venues from BestTime API |
| `live_forecast` | Refresh live busyness data |
| `weekly_forecast` | Refresh weekly forecast data |
| `google_places` | Enrich with Google Places vibe attributes, hours, and business status |
| `photos` | Fetch venue photos from Google Places |
| `instagram` | Discover Instagram handles via Apify |
| `instagram_validate` | Check cached Instagram handles and remove invalid handles |
| `vibe_classifier` | Classify venue vibes from photos and text signals |

## Common Commands

```bash
make run-docker-compose
make request
make test-unit
make test-bdd
make test
make test-integration
make build
make push
```

Do not remove Redis volumes or run volume-removal commands in any environment
that may point at shared or cloud Redis. `make run-docker-compose` preserves
volumes.

See `tests/README.md` for test prerequisites and command details. See
`DEPLOYMENT.md` for deployment and operational validation notes.

## Repository Guide

- `main.py`: FastAPI app, lifespan, scheduled jobs, health, and metrics routes
- `app/config.py`: settings and JSON config loading
- `app/container.py`: dependency wiring
- `app/routers/`: FastAPI route definitions
- `app/handlers/`: request behavior and response shaping
- `app/services/`: refresh, enrichment, and business logic
- `app/dao/`: Redis persistence boundaries
- `app/models/`: Pydantic models and serialization compatibility
- `app/metrics.py`: Prometheus metric definitions
- `tests/`: pytest and BDD test suites
- `plans/`: approved feature plans for agentic development

Agent lifecycle instructions live in `AGENTS.md`; Claude compatibility notes
live in `CLAUDE.md`.

## Deployment Note

cs-server is built from source on EC2 via the `vibes_bot` CI/CD pipeline. A
commit message containing `[FULL-RESTART]` triggers a rebuild. Use that marker
only when a full service rebuild is intentional.
