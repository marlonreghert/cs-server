# CS-Server Tests

The test suite has three layers:

- Unit tests that run without Redis or external services.
- Redis integration tests that require a local Redis instance.
- BDD feature contracts written in Gherkin and executed with Behave.

## Prerequisites

Install runtime and development dependencies:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
```

For Redis integration tests, start Redis first:

```bash
docker-compose up -d redis
```

## Unit Tests

The default unit target covers the stable no-dependency suite for Pydantic
models, mocked Redis DAO behavior, BestTime client behavior, services, handlers,
and Instagram enrichment/validation logic.

```bash
make test-unit
```

Run additional focused test files directly when working in that area.

## Redis Integration Tests

Redis integration tests use Redis database `15` and flush that test database
after each run.

```bash
make test-integration
```

These tests validate real Redis geospatial behavior and key compatibility.

## BDD Tests

Gherkin feature files live under `tests/bdd/<domain>/`, with Behave step
definitions under `tests/bdd/steps/`.

Domains:

- `api`: HTTP contracts, response shapes, validation, health, and debug behavior
- `refresh`: BestTime discovery, live forecast refresh, weekly forecast refresh,
  scheduling, and startup refresh behavior
- `enrichment`: optional Google Places, Instagram, menu, and vibe classifier
  behavior
- `persistence`: Redis key compatibility, DAO behavior, cache boundaries, and
  migrations
- `observability`: metrics, tracing, logging, and background-job failure
  visibility

Run all BDD features:

```bash
make test-bdd
```

Run one feature:

```bash
make test-feature FEATURE=tests/bdd/api/<slug>.feature
```

When no `.feature` files exist yet, `make test-bdd` skips cleanly.

## Run The Default Suite

```bash
make test
```

`make test` runs unit tests and BDD tests. Run `make test-integration`
separately when Redis is available.

## Critical Behavior Covered By Existing Pytest Tests

Existing pytest coverage protects:

- Redis key compatibility with the original Go implementation:
  `venues_geo_v1`, `venues_geo_place_v1:{venue_id}`,
  `live_forecast_v1:{venue_id}`, and
  `weekly_forecast_v1:{venue_id}_{day_int}`.
- JSON serialization/deserialization and field aliases such as `venue_lng`,
  `24h`, and `12h`.
- BestTime client request construction, API key usage, and error handling.
- Venue refresh rules, including default Recife locations, nightlife venue
  types, deduplication, live forecast caching, weekly forecast caching, and
  non-OK response handling.
- Handler behavior, including Recife day selection, live-first sorting,
  minified/verbose responses, optional cache data, and health responses.
