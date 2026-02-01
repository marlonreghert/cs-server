# CS-Server Tests

## Test Structure

- **test_models.py** - Unit tests for Pydantic data models (11 tests)
- **test_redis_dao_unit.py** - Unit tests for Redis DAO (mocked, no Redis required) (8 tests)
- **test_besttime_client.py** - Unit tests for BestTime API client (mocked, no API required) (11 tests)
- **test_services.py** - Unit tests for business logic services (mocked, no dependencies) (13 tests)
- **test_handlers.py** - Unit tests for HTTP request handlers (mocked, no dependencies) (13 tests)
- **test_redis_dao.py** - Integration tests for Redis DAO (requires running Redis) (10 tests)

## Running Tests

### Unit Tests (No dependencies required)

```bash
source venv/bin/activate
pytest tests/test_models.py tests/test_redis_dao_unit.py tests/test_besttime_client.py tests/test_services.py tests/test_handlers.py -v
# 56 tests total
```

### Integration Tests (Requires Redis)

1. Start Redis using Docker Compose:
   ```bash
   docker-compose up -d redis
   ```

2. Run integration tests:
   ```bash
   pytest tests/test_redis_dao.py -v
   ```

3. Stop Redis when done:
   ```bash
   docker-compose down
   ```

### Run All Tests

```bash
pytest tests/ -v
```

## Test Database

Integration tests use Redis database 15 to avoid conflicts with production data. The test database is automatically flushed after each test run.

## Key Testing Points

### Redis Key Compatibility

All Redis key formats are tested for exact compatibility with the Go implementation:

- **Geo index**: `venues_geo_v1`
- **Venue data**: `venues_geo_place_v1:{venue_id}`
- **Live forecast**: `live_forecast_v1:{venue_id}`
- **Weekly forecast**: `weekly_forecast_v1:{venue_id}_{day_int}`

### Critical Business Logic

Tests verify:
- JSON serialization/deserialization matches Go
- Field aliases work correctly (`venue_lng`, `24h`, `12h`)
- Custom validators (int/string conversion for `venue_open`/`venue_closed`)
- Geospatial queries return correct results
- Cache operations maintain data integrity

### BestTime API Client

Tests verify:
- Async HTTP client with proper connection pooling
- Query parameter construction matches Go implementation
- API key injection (public vs private keys)
- Error handling for HTTP errors and network failures
- Proper use of venue_filter (preferred), get_live_forecast, and get_week_raw_forecast endpoints
- Parameter validation (e.g., venue_id or venue_name+venue_address required)

### Business Logic Services

Tests verify **CRITICAL** business logic preservation:
- **Deduplication algorithm**: By `venue_id` first, then by `venue_name` (exact Go logic from lines 374-417)
- **Live forecast filtering**: Only cache when `status == "OK"` AND `venue_live_busyness_available == True` (lines 254-265)
- **Venue mapping**: VenueFilterVenue → Venue conversion preserves all fields correctly (lines 432-469)
- **Default locations**: Exact lat/lng/radius/limit values for 3 Recife locations (lines 39-41)
- **Nightlife venue types**: Exact 11-type list matching Go implementation (lines 60-96)
- **Weekly forecast caching**: All 7 days cached per venue, non-OK status skipped (lines 538-581)
- Empty venue ID and name handling (skipped during deduplication)

### HTTP Request Handlers

Tests verify **CRITICAL** handler logic preservation:
- **Day index conversion**: Python weekday (0=Mon, 6=Sun) matches BestTime day_int directly (no conversion needed)
- **Venue sorting**: Venues with live data first, sorted by busyness descending; then venues without live data
- **Verbose mode**: Returns full VenueWithLive structure with nested venue, live_forecast, and weekly_forecast
- **Minified mode**: Returns MinifiedVenue with essential fields only
- **Live busyness extraction**: Only included when available (venue_live_busyness_available == True)
- **Weekly forecast inclusion**: WeekRawDay for current day (Recife timezone) included when available
- **Error handling**: Missing live/weekly forecasts don't crash, set to None
- **Health check**: Ping endpoint returns {"status": "pong"}
