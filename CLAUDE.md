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

### Docker (recommended)

```bash
docker-compose up -d       # Starts cs-server + Redis, all env vars pre-configured
curl http://localhost:8080/ping
```

### Manual (without Docker)

```bash
# 1. Set env vars (required — Docker sets these automatically, manual does not)
export REDIS_HOST=localhost
export REDIS_PORT=6379
# Optional but useful:
export LOG_LEVEL=DEBUG
export REFRESH_ON_STARTUP=false     # Skip venue refresh on startup for faster dev cycles

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start Redis
redis-server                        # in a separate terminal

# 4. Run the app
uvicorn main:app --host 0.0.0.0 --port 8080

# 5. Health check
curl http://localhost:8080/ping
```

**Note**: API keys (`BESTTIME_PRIVATE_KEY`, `BESTTIME_PUBLIC_KEY`, `GOOGLE_PLACES_API_KEY`, `OPENAI_API_KEY`, `APIFY_API_TOKEN`) have embedded defaults or are optional. See `app/config.py` for the full list, or copy from `.env.example`.

### Dev Mode

When running locally for development, always use dev mode to limit API calls and venue count:

```bash
export DEV_MODE=true
export FETCH_VENUE_TOTAL_LIMIT=10   # Only fetch ~10 venues (saves BestTime API quota)
export REFRESH_ON_STARTUP=true      # Fetch venues on startup so there's data to work with
```

Dev mode uses a single location (Recife ZS/ZN by default: `DEV_LAT=-8.07834`, `DEV_LNG=-34.90938`, `DEV_RADIUS=6000`) instead of cycling through all discovery points.

## Testing

```bash
# All tests
pytest tests/ -v

# Unit tests only (no Redis needed)
pytest tests/ -v -m "not integration"
```

- pytest with `asyncio_mode = auto` (see pytest.ini)
- Markers: `unit`, `integration`
- **Functional tests are the primary testing strategy.** Focus on testing real behavior through the API (FastAPI TestClient + real or test Redis) rather than unit-testing individual functions in isolation. This validates that the system works end-to-end and catches integration issues that unit tests miss.
- Unit tests are complementary — use them for complex pure logic (scoring, transformations, config parsing) where functional tests would be overkill.
- **Write only the most important tests.** Over-testing leads to a verbose, brittle suite that is hard to maintain and troubleshoot. Prioritize: critical paths (venue query, refresh pipeline), edge cases that have caused bugs, and non-obvious business rules. Skip trivial getters, simple CRUD wrappers, and obvious pass-throughs.
- Use `unittest.mock` (Mock, AsyncMock, patch) sparingly — match the existing test patterns in `tests/`. Prefer real dependencies (TestClient, test Redis) over heavy mocking.
- Mark tests with `@pytest.mark.integration` if they need Redis; all others should run without external deps.
- Run the full test suite before considering work done: `pytest tests/ -v`

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

### Dev Mode
- **When running locally, always use `DEV_MODE=true` with `FETCH_VENUE_TOTAL_LIMIT=10`.** This limits venue fetching to ~10 venues in the Recife dev region, saving BestTime API quota and keeping Redis lightweight.
- Dev mode coordinates (`DEV_LAT`, `DEV_LNG`, `DEV_RADIUS`) should match the region the mobile app is configured for.
- All enrichment services (photos, Instagram, menu, vibe classifier) should be disabled in dev unless actively testing them.

### Testing
- **Functional tests are the primary testing strategy.** Focus on testing real behavior through the API (FastAPI TestClient + real or test Redis) rather than unit-testing individual functions in isolation.
- Unit tests are complementary — use them for complex pure logic where functional tests would be overkill.
- **Write only the most important tests.** Prioritize critical paths, edge cases that have caused bugs, and non-obvious business rules.
- Run the full test suite before considering work done: `pytest tests/ -v`

### Documentation
- **Always update the README** when making important changes: new endpoints, new env vars, changed setup steps, new dependencies, architectural changes, or anything that affects how someone runs or understands the project. The README is the entry point for anyone working on cs-server — keep it accurate and current.

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
