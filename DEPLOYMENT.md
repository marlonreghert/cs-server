# CS-Server Deployment Guide

## Overview

This guide covers deploying the Python-based cs-server application using Docker and docker-compose.

## Prerequisites

- Docker (20.10+)
- Docker Compose (2.0+)
- Python 3.13 (for local development)

## Docker Deployment

### Building the Image

Build the Docker image locally:

```bash
docker-compose build cs-server
```

### Starting the Services

Start all services (cs-server + Redis):

```bash
docker-compose up -d
```

This will:
1. Start Redis on port 6379
2. Start cs-server on port 8080
3. Run the initial data load sequence
4. Start background jobs (venue catalog, live forecast, weekly forecast)

### Viewing Logs

```bash
# All services
docker-compose logs -f

# CS-Server only
docker-compose logs -f cs-server

# Redis only
docker-compose logs -f redis
```

### Stopping the Services

```bash
docker-compose down
```

To remove volumes (Redis data):

```bash
docker-compose down -v
```

## Environment Variables

Configure the application using environment variables in `docker-compose.yml` or `.env` file:

### Redis Configuration
- `REDIS_HOST` - Redis hostname (default: `redis`)
- `REDIS_PORT` - Redis port (default: `6379`)
- `REDIS_PASSWORD` - Redis password (default: empty)
- `REDIS_DB` - Redis database number (default: `0`)

### BestTime API Configuration
- `BESTTIME_PRIVATE_KEY` - BestTime private API key
- `BESTTIME_PUBLIC_KEY` - BestTime public API key
- `BESTTIME_ENDPOINT_BASE_V1` - BestTime API base URL

### Scheduler Configuration
- `VENUES_CATALOG_REFRESH_MINUTES` - Venue catalog refresh interval (default: `43200` = 30 days)
- `VENUES_LIVE_REFRESH_MINUTES` - Live forecast refresh interval (default: `5` minutes)
- `WEEKLY_FORECAST_CRON` - Weekly forecast cron schedule (default: `0 0 * * 0` = Sundays at 00:00)

### Server Configuration
- `SERVER_PORT` - HTTP server port (default: `8080`)
- `LOG_LEVEL` - Logging level (default: `INFO`)

## Health Checks

### Application Health

```bash
curl http://localhost:8080/health
# Expected: {"status":"healthy"}
```

### Ping Endpoint

```bash
curl http://localhost:8080/ping
# Expected: {"status":"pong"}
```

### Venues Nearby API

```bash
curl "http://localhost:8080/v1/venues/nearby?lat=-8.07834&lon=-34.90938&radius=5&verbose=false"
# Expected: JSON array of venues
```

## Validation Steps

After deployment, verify the application is working correctly:

### 1. Container Status

```bash
docker-compose ps
```

Expected output:
- `cs-server` - Up and healthy
- `redis` - Up

### 2. Health Check

```bash
curl http://localhost:8080/health
```

Expected: `{"status":"healthy"}`

### 3. Redis Connection

Check cs-server logs for Redis connection success:

```bash
docker-compose logs cs-server | grep Redis
```

Expected: `[Container] Redis connection successful`

### 4. Initial Data Load

Check logs for initial venue loading:

```bash
docker-compose logs cs-server | grep "Initial"
```

Expected:
- `[Main] Refreshing venues data (initial load)`
- `[Main] Initial venue refresh completed`
- `[Main] Initial live forecast refresh completed`
- `[Main] Initial weekly forecast refresh completed`

### 5. Background Jobs

Check logs for scheduler startup:

```bash
docker-compose logs cs-server | grep Scheduler
```

Expected:
- `[Scheduler] Scheduled venue catalog refresh every 43200 minutes`
- `[Scheduler] Scheduled live forecast refresh every 5 minutes`
- `[Scheduler] Scheduled weekly forecast refresh with cron: 0 0 * * 0`
- `[Scheduler] Background jobs started`

### 6. API Endpoints

Test the main API endpoint:

```bash
curl "http://localhost:8080/v1/venues/nearby?lat=-8.07834&lon=-34.90938&radius=5&verbose=false"
```

Expected: JSON array with venue data

## Troubleshooting

### Container Won't Start

Check logs:

```bash
docker-compose logs cs-server
```

Common issues:
- Redis not running: `docker-compose up -d redis`
- Port 8080 already in use: Change `SERVER_PORT` in environment
- Missing dependencies: Rebuild image with `docker-compose build --no-cache cs-server`

### Redis Connection Failed

Verify Redis is running:

```bash
docker-compose ps redis
docker-compose logs redis
```

Test Redis connection:

```bash
docker-compose exec redis redis-cli ping
# Expected: PONG
```

### Application Crashes on Startup

Check for Python errors in logs:

```bash
docker-compose logs cs-server | grep -i error
```

Common issues:
- Missing environment variables
- Invalid configuration values
- BestTime API key issues

### No Venues Returned

Check if initial data load completed:

```bash
docker-compose logs cs-server | grep "Initial venue refresh completed"
```

Verify Redis has data:

```bash
docker-compose exec redis redis-cli
> KEYS venues_geo*
> ZCARD venues_geo_v1
```

## Production Deployment

### Security Considerations

1. **Secure Redis**: Use Redis password authentication
   ```yaml
   environment:
     - REDIS_PASSWORD=your-secure-password
   ```

2. **API Keys**: Store BestTime API keys securely (use secrets management)

3. **Network**: Use internal networks for Redis communication

4. **Resource Limits**: Add resource constraints in docker-compose.yml
   ```yaml
   deploy:
     resources:
       limits:
         cpus: '2'
         memory: 2G
       reservations:
         cpus: '1'
         memory: 1G
   ```

### Monitoring

1. **Application Logs**: Centralized logging (e.g., ELK stack)
2. **Health Checks**: Monitoring system pinging `/health` endpoint
3. **Redis Metrics**: Monitor Redis memory usage and connection count
4. **API Performance**: Track response times for `/v1/venues/nearby`

### Backup and Recovery

1. **Redis Persistence**: Configured via `redis.conf`
2. **Data Volume**: `redis_data` volume persists Redis data
3. **Backup**: Use `docker-compose exec redis redis-cli SAVE` for manual backup
4. **Restore**: Copy RDB file to volume before starting

## Migration from Go to Python

### Backward Compatibility

The Python implementation maintains **strict backward compatibility** with the Go version:

- **Redis keys**: Exact same format
  - `venues_geo_v1` - Geospatial set
  - `venues_geo_place_v1:{venue_id}` - Venue JSON
  - `live_forecast_v1:{venue_id}` - Live forecast
  - `weekly_forecast_v1:{venue_id}_{day_int}` - Weekly forecast

- **API endpoints**: Same URLs and response formats
  - `GET /v1/venues/nearby`
  - `GET /ping`

- **Business logic**: Exact 1:1 preservation
  - Deduplication algorithm
  - Live forecast filtering
  - Day index conversion
  - Venue sorting

### Rollback Plan

If issues arise, you can rollback to the Go version:

1. Stop Python service:
   ```bash
   docker-compose stop cs-server
   ```

2. Update docker-compose.yml to use Go image:
   ```yaml
   cs-server:
     image: johnsummit2024/cs-server:2025_12_02_16_01
   ```

3. Restart service:
   ```bash
   docker-compose up -d cs-server
   ```

**Note**: Redis data remains compatible with both versions due to key naming preservation.

### Verification After Rollback

1. Check logs: `docker-compose logs cs-server`
2. Test API: `curl http://localhost:8080/ping`
3. Verify venues: `curl "http://localhost:8080/v1/venues/nearby?lat=-8.07834&lon=-34.90938&radius=5"`

## Performance Benchmarks

### Target Metrics

- `/v1/venues/nearby` response time: < 100ms
- Venue catalog refresh (500 venues): < 5 minutes
- Live forecast refresh (all venues): < 2 minutes
- Memory usage: < 512MB

### Load Testing

Use tools like `locust` or `ab` to test performance:

```bash
# Apache Bench example
ab -n 1000 -c 10 "http://localhost:8080/v1/venues/nearby?lat=-8.07834&lon=-34.90938&radius=5"
```

## Support

For issues or questions:
- Check logs: `docker-compose logs -f cs-server`
- Review tests: `pytest tests/ -v`
- GitHub Issues: [Project Repository]

## Appendix

### Docker Image Size

Expected image size: ~400-500MB (Python 3.13-slim + dependencies)

### Startup Time

Expected startup time: 30-60 seconds (includes initial data load)

### Resource Usage

Typical resource usage:
- CPU: 5-10% idle, 50-80% during refresh jobs
- Memory: 200-300MB idle, 400-500MB during refresh jobs
- Disk: Redis data grows with number of venues (~10MB per 1000 venues)
