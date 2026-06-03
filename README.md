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

## Connecting to RDS (DBeaver, no VPN)

The RDS Postgres (system of record) has **no public endpoint** — it lives in a
private subnet reachable only from the EC2's VPC. Engineers connect from their
laptop by opening an **SSM port-forward** through the EC2, then pointing DBeaver
at `localhost`. No VPN, no inbound DB rule, no IP allowlist. Full provisioning
runbook: `infra/rds/README.md`.

Prerequisites (one-time): AWS SSO profile configured (`aws sso login --profile
<profile>`) and the `session-manager-plugin` installed locally.

1. Get the endpoint and password (the password lives only in Secrets Manager —
   never commit it):
   ```bash
   aws rds describe-db-instances --profile vibesense --region us-east-1 \
     --query 'DBInstances[].Endpoint.Address' --output text          # RDS host
   aws secretsmanager get-secret-value --profile vibesense --region us-east-1 \
     --secret-id vibesense/rds/credentials --query SecretString --output text \
     | python3 -c "import sys,json;d=json.load(sys.stdin);print('host=',d['host']);print('user=',d['user']);print('password=',d['password'])"
   ```
2. Open the tunnel on your laptop (leave this terminal running for the whole
   session):
   ```bash
   # current values: --target i-0893fb6d283243480 (the "vibes-bot" instance)
   #                 <rds-endpoint> = vibesense.cm1ie0s6iz4a.us-east-1.rds.amazonaws.com
   aws ssm start-session --profile vibesense --region us-east-1 \
     --target i-0893fb6d283243480 \
     --document-name AWS-StartPortForwardingSessionToRemoteHost \
     --parameters '{"host":["vibesense.cm1ie0s6iz4a.us-east-1.rds.amazonaws.com"],"portNumber":["5432"],"localPortNumber":["5432"]}'
   ```
   (The cs-server EC2 is the `vibes-bot` instance. If `localhost:5432` is taken,
   use `"localPortNumber":["15432"]` and point DBeaver at `15432`.)
3. DBeaver → new PostgreSQL connection: Host `localhost`, Port `5432`, Database
   `vibesense`, Username `vibesense_admin`, Password (from step 1), **SSL tab →
   Use SSL, mode `require`**. Test Connection routes laptop → EC2 → RDS.

> Note: serving reads Redis, not RDS, so a hand-edit to a venue's `payload`
> JSONB only reaches the app after a Redis projection — see the projection jobs
> and `plans/redis_projection_decoupling_01_06_26.md`.

## Operating cs-server on the EC2 (SSM shell)

cs-server runs in a Docker container on the `vibes-bot` EC2. Get an interactive
shell on the box with SSM (no SSH key needed; same prerequisites as above —
`aws sso login` + `session-manager-plugin`):

```bash
# find the instance id (it's the "vibes-bot" instance):
aws ec2 describe-instances --profile vibesense --region us-east-1 \
  --filters "Name=tag:Name,Values=vibes-bot" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text
# then open a shell (current instance: i-0893fb6d283243480 — re-check above if the box was replaced):
aws ssm start-session --profile vibesense --region us-east-1 --target i-0893fb6d283243480
```
(Optional: `sudo su - ubuntu`)
You land as `ssm-user`; `sudo docker ...` works. Container names:
`vibes_bot-cs-server-1`, `vibes_bot-redis-1`, `vibes_bot-vibesbot-1`.

Common commands:
```bash
# logs / health / env
sudo docker logs --tail 100 vibes_bot-cs-server-1
sudo docker inspect --format '{{.State.Health.Status}}' vibes_bot-cs-server-1
sudo docker exec vibes_bot-cs-server-1 printenv | grep '^RDS_'

# redis sanity (active venue count)
sudo docker exec vibes_bot-redis-1 redis-cli ZCARD venues_geo_v1

# admin jobs that are async-safe can use the HTTP trigger:
sudo docker exec vibes_bot-cs-server-1 python -c \
  "import urllib.request as u;print(u.urlopen(u.Request('http://localhost:8080/admin/trigger/<job>',method='POST'),timeout=10).read())"
```

> ⚠️ Do NOT run `backfill_rds` or `rebuild_redis` via the HTTP trigger. They are
> **synchronous and block cs-server's event loop**, stalling `GET /v1/venues/nearby`
> and `/health` for the whole run. Run them as a one-off process instead, which
> doesn't touch the live server's loop:
> ```bash
> sudo docker exec -d vibes_bot-cs-server-1 sh -c \
>   "python -c 'from app.config import Settings; from app.container import Container; print(Container(Settings()).redis_projection_service.rebuild_redis_from_rds())' > /tmp/rebuild.out 2>&1"
> sudo docker exec vibes_bot-cs-server-1 cat /tmp/rebuild.out   # check when it finishes
> ```
> (Swap `rebuild_redis_from_rds()` for `backfill_rds_from_redis()` as needed.)
> A scheduled, off-loop projector is planned in
> `plans/redis_projection_decoupling_01_06_26.md`.
