# CS-Server

Venue data aggregation and caching service. Fetches venue data from BestTime API, enriches with Google Places/OpenAI/Apify, caches in Redis with geospatial indexing.

## Tech Stack

- Python 3.13, FastAPI 0.115, Uvicorn
- Redis (geospatial queries via GEORADIUS, persistence via AOF)
- Pydantic v2 + pydantic-settings for config and models
- httpx (async HTTP), APScheduler (background jobs)
- Prometheus metrics

## Project Layout

```
app/
  api/          # External API clients (BestTime, Google Places, OpenAI, Apify)
  dao/          # Redis data access (RedisVenueDAO)
  db/           # GeoRedisClient
  handlers/     # Request handlers (business logic)
  models/       # Pydantic models
  routers/      # FastAPI route definitions
  services/     # Business logic (venue refresh, enrichment, discovery points)
  config.py     # Settings (pydantic-settings, env > JSON > defaults)
  container.py  # Dependency injection container
main.py         # Entry point
```

## Running

```bash
# Docker (recommended)
docker-compose up -d

# Manual
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080

# Health check
curl http://localhost:8080/ping
```

## Testing

```bash
# All tests
pytest tests/ -v

# Unit tests only (no Redis needed)
pytest tests/ -v -m "not integration"
```

- pytest with `asyncio_mode = auto` (see pytest.ini)
- Markers: `unit`, `integration`
- Mocking: `unittest.mock` (Mock, AsyncMock, patch)

## Configuration

Priority: **env vars > JSON config (`config/`) > defaults** (see `app/config.py`)

**Gotcha**: pydantic-settings treats `__init__` kwargs as highest priority (above env vars). JSON keys that have env var overrides must be removed before passing to `super().__init__`.

## Key API Endpoints

- `GET /v1/venues/nearby?lat=...&lon=...&radius=...` — main venue query
- `GET /ping` — health check
- `POST /admin/trigger/refresh` — trigger venue refresh
- `POST /admin/trigger/photos` — trigger photo enrichment
- `POST /admin/recount-discovery-points` — recount venue density per discovery point

## Code Style

- Formatter: Black (default 88 char line length)
- Linter: flake8
- Type checker: mypy
- Imports: stdlib → third-party → local (`from app.xxx import`)
- Naming: PascalCase classes, snake_case functions, UPPER_SNAKE constants
- Private: leading underscore (`_method`, `_attribute`)
- Async throughout: `async def`, `httpx.AsyncClient`, `asynccontextmanager`

## Redis Keys

- `venues_geo_v1` — geospatial index (GEOADD/GEORADIUS)
- `live_forecast_v1:{venue_id}` — live busyness data
- `weekly_forecast_v1:{venue_id}_{day}` — weekly forecast cache

## Docker

- Image: `python:3.13-slim`
- Port: 8080
- `docker-compose.yml` runs cs-server + Redis with custom `redis.conf`

## Development Guidelines

### Design & Architecture
- Follow existing patterns (layered architecture: routers → handlers → services → dao). Don't add unnecessary abstractions.
- Use dependency injection via `container.py` — don't instantiate services directly in routers.
- Keep business logic in `services/`, HTTP concerns in `routers/` and `handlers/`.
- Prefer simple, readable code over clever solutions. Minimize complexity.

### Testing
- Every new feature or bug fix must include unit tests. Update existing tests when behavior changes.
- Use `unittest.mock` (Mock, AsyncMock, patch) — match the existing test patterns in `tests/`.
- Mark tests with `@pytest.mark.integration` if they need Redis; all others should run without external deps.
- Run the full test suite before considering work done: `pytest tests/ -v`

### CI/CD
- cs-server is built from source on EC2 via vibes_bot's CI/CD. If you change the build process (Dockerfile, dependencies), verify it still works with `[FULL-RESTART]`.
- Keep `requirements.txt` up to date when adding dependencies.

### Security
- Never log or expose API keys, tokens, or secrets. Use env vars for all credentials.
- Validate and sanitize all external input (query params, request bodies) via Pydantic models.
- Be cautious with Redis commands that accept user input — avoid injection via key patterns.
- Don't expose admin/debug endpoints without authentication in production.

### Resource Awareness
- This runs on a single EC2 instance with limited memory. Be mindful of:
  - Redis memory usage (TTLs on all cached data, avoid unbounded key growth)
  - Background job frequency (APScheduler intervals, API rate limits on BestTime/Google)
  - httpx connection pooling — reuse clients, don't create new ones per request
  - Large GEORADIUS queries — use reasonable radius limits
- Avoid loading large datasets into memory at once; prefer streaming/pagination.

### Observability
- Expose Prometheus metrics for new background jobs and critical paths (use `app/metrics.py`).
- Add counters for API calls to external services (success/error/latency).
- Add gauges for resource-related metrics (cache size, queue depth).
- Log meaningful events at appropriate levels: `info` for lifecycle, `warning` for degraded state, `error` for failures.

## Important Notes

- cs-server does NOT have its own CI/CD — it's built from source on EC2 via vibes_bot's CI/CD pipeline when commit message contains `[FULL-RESTART]`
- Discovery points rotation: configurable via admin panel, stored in Redis as `admin_config:discovery_points`
- DEFAULT_LOCATIONS radius is 15000m, limit 500

