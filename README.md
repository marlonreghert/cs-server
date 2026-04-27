# CS-Server

Venue data aggregation and caching service for VibeSense. Fetches venue data from BestTime API, enriches with Google Places (photos, opening hours, vibe attributes, Instagram), and serves it via a REST API with Redis geospatial indexing.

## Tech Stack

- Python 3.13, FastAPI 0.115, Uvicorn
- Redis (geospatial queries, caching, persistence via AOF)
- Pydantic v2 + pydantic-settings
- httpx (async HTTP), APScheduler (background jobs)
- Prometheus metrics

## API Endpoints

```
GET  /v1/venues/nearby?lat=...&lon=...&radius=...   # Main venue query
GET  /ping                                            # Health check
GET  /health                                          # Health check (Docker)
POST /admin/trigger/{job_name}                        # Trigger enrichment jobs
GET  /admin/jobs                                      # List jobs + status
POST /admin/recount-discovery-points                  # Recount venue density
```

## Running

```bash
# Docker (recommended)
docker-compose up -d

# Manual
export REDIS_HOST=localhost REDIS_PORT=6379
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

For dev mode, see `CLAUDE.md`.

## Enrichment Services

Triggerable from the admin panel (`admin.apivibesensemiddleware.click`):

| Job | Description |
|-----|-------------|
| `venue_catalog` | Fetch venues from BestTime API |
| `live_forecast` | Refresh live busyness data |
| `weekly_forecast` | Refresh weekly forecast data |
| `google_places` | Enrich with Google Places (vibe attributes, hours, business status) |
| `photos` | Fetch venue photos from Google Places |
| `instagram` | Discover Instagram handles via Apify |
| `instagram_validate` | Check cached Instagram handles and remove invalid ones |
| `vibe_classifier` | Classify venue vibes from photos (GPT-4o) |

## Configuration

Priority: **env vars > JSON config (`config/`) > defaults** (see `app/config.py`)

See `CLAUDE.md` for full configuration details, dev mode setup, and development guidelines.

## Deployment

cs-server is built from source on EC2 via vibes_bot's CI/CD pipeline. Include `[FULL-RESTART]` in the commit message to trigger a rebuild.
