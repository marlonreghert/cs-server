# Venue Visit Tracking — RDS system of record and batch ingest

## Branch
feature/venue-visit-tracking

## Goal

Persist which signed-in user was physically present at which venue, and for how
long, as the system-of-record dataset that later personalization and busyness
forecasting will be built on.

cs-server must accept a **batch** of device-detected visits, pseudonymize the
user, validate each visit, and write one durable row per visit into a new
`engagement.venue_visit` table. Erasure must remove those rows with the rest of
a deleted user's engagement data.

This is the first of three repos in the data-flow (`cs-server → vibes_bot →
mobile`). It ships the storage and the ingest contract; nothing reads the data
back yet.

## Non-goals

- **No Redis projection.** Visits are RDS-only, exactly like
  `engagement.app_session_day` (`migrations/versions/0012_engagement_app_session_day.py`).
  Nothing on the serve path reads a visit, so projecting one would add a key
  family to a Redis instance that runs with `maxmemory 0` / `noeviction` for no
  serving benefit. `EngagementService.record_visits` therefore has no
  projection step and no "RDS-then-Redis" retry contract.
- **No analytics, aggregation, or read API.** No "venues this user visits", no
  per-venue visit histogram, no admin dashboard panel. Deriving signal from the
  rows is deliberate follow-up work, and the per-visit grain chosen here keeps
  every aggregate derivable later.
- **No forecasting or recommendation change.** The busyness pipeline and its
  BestTime inputs are untouched.
- **No geofence detection logic.** Detecting arrival/departure is the device's
  job (vibe_sense_mobile). cs-server never infers a visit; it records what it is
  told, after validating it.
- **No retention/TTL job.** Rows accumulate. A retention policy is follow-up
  work — see Open Questions.

## Evidence

- `app/routers/engagement_router.py` — the existing engagement write surface
  (`/v1/favorites`, `/v1/hot-likes`, `/v1/blocks`, `/v1/sessions`,
  `/v1/user-data`). Its module docstring fixes the conventions this endpoint
  must follow: vibes_bot is the only caller, a retryable failure returns 5xx,
  and the client retries. `SessionRequest` shows the precedent for a
  venue-less, user-only engagement DTO.
- `app/services/engagement_service.py` — `pseudonymize()` HMACs the raw user id
  before it reaches RDS, and the constructor refuses to start on an empty
  `ENGAGEMENT_PSEUDONYMIZATION_KEY` rather than silently hashing under `b""`.
  `record_session()` is the closest existing shape: RDS-only, no projection,
  idempotent per Recife day. `delete_user_data()` is the erasure path this
  change must extend.
- `app/dao/rds_venue_store.py:1587` `purge_user_engagement()` — hard-deletes
  from `engagement.favorite`, `hot_like_event`, `app_session_day`,
  `blocked_venue` in one transaction and returns per-store counts. A new table
  that is not added here would survive an account deletion.
- `app/dao/rds_venue_store.py:1615` `record_app_session()` — the
  `INSERT ... ON CONFLICT DO NOTHING` idempotency pattern.
- `migrations/versions/0016_hot_like_event_idempotency.py` — the load-bearing
  precedent, and the one this plan deliberately diverges from. Its rationale:
  the router mandates a retry on 5xx, the RDS insert commits before the Redis
  projection, so without a natural key a retry persists a duplicate row. It
  also documents why a stored `business_period date` column is required rather
  than an expression index (`(created_at AT TIME ZONE 'America/Recife')::date`
  is only STABLE, not IMMUTABLE, so Postgres will not index it).
- `migrations/versions/0012_engagement_app_session_day.py` — the RDS-only,
  no-projection engagement precedent, and the warning that **deploy does not run
  alembic** (CI-only); the migration must be applied manually against RDS.
- `migrations/versions/0043_instagram_profile_photo.py` — current alembic head
  (`revision = "0043_instagram_profile_photo"`); nothing declares it as a
  `down_revision`.
- `app/utils/recife_time.py` `recife_today()` — the America/Recife calendar-day
  convention every engagement table already uses.
- `app/metrics.py` — `ENGAGEMENT_SESSION_TOTAL`,
  `ENGAGEMENT_HOT_LIKE_DEDUP_TOTAL`, `ENGAGEMENT_USER_DELETION_TOTAL` are the
  naming precedent for the new counters.

## Current Behavior

cs-server records four kinds of engagement, all keyed by an HMAC pseudonym:
favorites, hot-like events, blocked venues, and one app-session row per user per
Recife day. Every one of them is either a set membership or a once-per-day fact.

There is no representation of physical presence. Nothing in RDS answers "was
this user at this venue", "when", or "for how long". `engagement.app_session_day`
knows a user opened the app on a given day but not where they were, and it
deliberately stores no timestamp.

Every existing engagement write is a **single** (user, venue) fact per request.
There is no batch ingest endpoint.

## Desired Behavior

1. **Accept a batch of visits.** `POST /v1/visits` takes one raw `user_id` and a
   list of visits. Each visit carries `client_visit_id`, `venue_id`,
   `arrived_at`, `dwell_seconds`, and `source`. The endpoint pseudonymizes the
   user once and writes every accepted visit in one transaction.

2. **Be idempotent on the device's visit id, not on the calendar day.** A user
   can genuinely visit the same venue twice in one Recife day — lunch and again
   at night — and both visits are real data. The `(user, venue, business_period)`
   key that `0016` applies to hot-likes would silently collapse them and destroy
   exactly the signal this feature exists to capture. The device therefore mints
   a `client_visit_id` (UUID) when it opens a visit, and a unique index on
   `(user_pseudo, client_visit_id)` + `ON CONFLICT DO NOTHING` absorbs retries
   without collapsing distinct visits.

3. **Report per-visit outcomes.** The response returns `accepted`, `duplicate`,
   and `rejected` counts so the caller can distinguish "your retry was already
   applied" from "your batch was garbage" without inspecting the rows.

4. **Validate every visit at the boundary and reject individually.** One bad
   visit must not fail an otherwise good batch — the device may have buffered
   days of visits and a single poisoned row would strand all of them forever
   through the retry loop. Rejections are counted and logged, never fatal.

5. **Derive `business_period` server-side.** The Recife calendar day is computed
   from `arrived_at` on the server, never taken from the client. A device with a
   wrong timezone must not be able to write a row into the wrong day bucket.

6. **Store an unknown `venue_id` rather than rejecting it.** A venue can be
   soft-deleted between the moment the device detected the visit and the moment
   it uploads (the device may be offline for days). Rejecting would discard a
   real visit to a real place because of a race. The row is written and
   `ENGAGEMENT_VISIT_UNKNOWN_VENUE_TOTAL` is incremented so the rate is visible.
   No foreign key.

7. **Erase visits with the user.** `purge_user_engagement` hard-deletes
   `engagement.venue_visit` rows in the same transaction as the other four
   tables, and `delete_user_data` reports them in its counts. Apple Guideline
   5.1.1(v) treats a surviving pseudonymized row as a deactivation, not an
   erasure — and a location history is the most sensitive thing this system
   holds.

## Implementation Approach

**Migration** — `migrations/versions/0044_engagement_venue_visit.py`,
`down_revision = "0043_instagram_profile_photo"`. Raw SQL in `UPGRADE` /
`DOWNGRADE` string constants driven by `op.execute`, matching every other
migration in `engagement`. Creates the table, the unique idempotency index, and
two read indexes sized for the follow-up analytics work: `(venue_id,
business_period)` and `(user_pseudo, arrived_at)`. Include the same
apply-manually warning `0012` carries — deploy does not run alembic.

**DAO** — `RdsVenueStore.insert_visits(user_pseudo, visits) -> int` performs a
single multi-row `INSERT ... ON CONFLICT (user_pseudo, client_visit_id) DO
NOTHING` inside one `engine.begin()` and returns the inserted `rowcount`. The
count of duplicates is `len(visits) - inserted`, which is why the insert must be
one statement rather than a loop. Extend `purge_user_engagement` with the
`venue_visit` delete and a `venue_visits` key in its returned dict.

**Service** — `EngagementService.record_visits(user_id, visits) -> dict`
pseudonymizes once, runs each visit through validation, computes
`business_period` from `arrived_at` in America/Recife, hands the survivors to
the DAO, and returns `{"accepted", "duplicate", "rejected"}`. It is RDS-only:
no Redis call, so there is no projection-failure retry contract to honour.
Extend `delete_user_data` to surface the new count.

**Router** — `POST /v1/visits` on the existing engagement router, with
`VisitItem` / `VisitBatchRequest` Pydantic models. A validation failure at the
Pydantic boundary is a 422; a store failure is a 502 with the "retry" detail
that `engagement_router`'s docstring and `EngagementClient` already agree on.

**Validation rules** (each rejection increments the counter with its own
`reason` label, so the metric alone identifies a misbehaving client build):

- `dwell_seconds` must be a positive integer at or below `VISIT_MAX_DWELL_SECONDS`
  (default 86400). A dwell longer than a day is a device that failed to observe
  the exit, not a visit.
- `dwell_seconds` below `VISIT_MIN_DWELL_SECONDS` (default 60) is rejected as
  GPS jitter. Configurable, because the device applies the same floor and the
  two must be tunable together.
- `arrived_at` must parse as a timezone-aware timestamp, must not be more than
  `VISIT_MAX_CLOCK_SKEW_SECONDS` (default 300) in the future, and must not be
  older than `VISIT_MAX_BACKFILL_DAYS` (default 30). A device buffering longer
  than that has data too stale to trust against a moved or renamed venue.
- `source` must be one of `geofence` / `foreground`. An unrecognized source from
  a future client build is rejected rather than stored, so the column stays a
  usable dimension.
- `client_visit_id` and `venue_id` must be non-empty.
- The batch itself is capped at `VISIT_MAX_BATCH_SIZE` (default 200) items;
  an oversized batch is a 422 at the Pydantic boundary, not a partial write.

**Config** — the six `VISIT_*` values land in `app/config.py` and
`config.example.json` with the defaults above.

## Data, Config, And API Impact

**New table** — `engagement.venue_visit`:

```
id               bigserial PRIMARY KEY
user_pseudo      text        NOT NULL   -- HMAC(user_id); raw id never stored
venue_id         text        NOT NULL   -- no FK: see Desired Behavior 6
client_visit_id  text        NOT NULL   -- device UUID; the idempotency key
arrived_at       timestamptz NOT NULL   -- device-observed arrival
dwell_seconds    integer     NOT NULL   -- device-measured presence
source           text        NOT NULL   -- 'geofence' | 'foreground'
business_period  date        NOT NULL   -- America/Recife day, derived server-side
created_at       timestamptz NOT NULL DEFAULT now()

UNIQUE INDEX ux_venue_visit_client_id ON (user_pseudo, client_visit_id)
INDEX ix_venue_visit_venue_period     ON (venue_id, business_period)
INDEX ix_venue_visit_user_arrived     ON (user_pseudo, arrived_at)
```

`departed_at` is deliberately absent — it is exactly `arrived_at +
dwell_seconds`, and storing both invites the two to disagree when a device
measures dwell across a clock adjustment. `dwell_seconds` is the measured value
and stays authoritative.

**New API** — `POST /v1/visits`:

```
request   { "user_id": "<raw firebase uid>",
            "visits": [ { "client_visit_id": "<uuid>",
                          "venue_id": "<id>",
                          "arrived_at": "<ISO-8601 with offset>",
                          "dwell_seconds": 1800,
                          "source": "geofence" } ] }
response  { "status": "ok", "accepted": 3, "duplicate": 1, "rejected": 0 }
```

**Changed API** — `DELETE /v1/user-data` response gains a `venue_visits` count.
Additive only.

**Config** — six new keys: `VISIT_MIN_DWELL_SECONDS` (60),
`VISIT_MAX_DWELL_SECONDS` (86400), `VISIT_MAX_CLOCK_SKEW_SECONDS` (300),
`VISIT_MAX_BACKFILL_DAYS` (30), `VISIT_MAX_BATCH_SIZE` (200).

**Migration ordering** — `0044` must be applied to RDS **before** the vibes_bot
release that starts calling `/v1/visits`, or the endpoint 500s on a missing
table. Same constraint `0012` documents.

## Error Handling And Observability

- A DAO/transaction failure returns 502 with a "retry" detail. The insert is
  idempotent, so the caller's retry converges.
- A per-visit validation failure never fails the batch: the visit is dropped,
  counted, and reported in `rejected`.
- Logs carry the pseudonym and the counts, never the raw user id and never
  coordinates — `CLAUDE.md`'s Security section forbids logging sensitive user or
  location data, and this is the most sensitive path in the service.

New Prometheus metrics in `app/metrics.py`:

- `ENGAGEMENT_VISIT_TOTAL{outcome}` — `accepted` / `duplicate` / `rejected`.
- `ENGAGEMENT_VISIT_REJECTED_TOTAL{reason}` — `dwell_too_short`,
  `dwell_too_long`, `future_timestamp`, `too_old`, `bad_source`, `missing_id`.
  A reason label that never appears proves that rejection path never ran, which
  is the fastest way to tell a client bug from a server bug.
- `ENGAGEMENT_VISIT_UNKNOWN_VENUE_TOTAL` — visits stored against a venue id not
  currently in RDS.
- `ENGAGEMENT_VISIT_BATCH_SIZE` — histogram; a persistently large batch size
  means devices are failing to upload and buffering.

## Test Plan

Feature file: `tests/bdd/api/venue-visit-tracking.feature`

Scenarios:

- **A batch of visits is persisted** — a two-visit batch returns
  `accepted: 2`, and both rows exist with the pseudonym, not the raw id.
- **The raw user id is never stored** — the persisted `user_pseudo` is the HMAC,
  and the raw id appears nowhere in the row.
- **A retried batch does not duplicate rows** — replaying an identical batch
  returns `duplicate` for every visit and leaves the row count unchanged.
- **Two visits to the same venue on the same day are both kept** — the
  regression that a Recife-day idempotency key would cause; distinct
  `client_visit_id`s must both persist.
- **A visit shorter than the minimum dwell is rejected** — counted in
  `rejected`, no row written, `dwell_too_short` reason recorded.
- **A visit with a future arrival beyond the skew allowance is rejected** —
  `future_timestamp`, no row.
- **A visit older than the backfill window is rejected** — `too_old`, no row.
- **An unrecognized source is rejected** — `bad_source`, no row.
- **One invalid visit does not reject the valid ones in the same batch** — the
  mixed batch reports `accepted: 1, rejected: 1` and writes exactly one row.
- **`business_period` is derived from `arrived_at` in America/Recife** — a visit
  just before Recife midnight lands on the correct calendar day regardless of
  the offset the client sent.
- **A visit to an unknown venue is stored, not rejected** — the row exists and
  the unknown-venue counter is incremented.
- **An oversized batch is refused with 422** — nothing is written.
- **Account deletion erases visits** — `DELETE /v1/user-data` removes every
  `venue_visit` row for the user and reports the count.

Pytest unit tests:

- `tests/unit_test/test_engagement_visits.py` — validation rules in isolation:
  each rejection reason, the Recife `business_period` boundary, and the
  accepted/duplicate arithmetic derived from the DAO rowcount.
- `tests/unit_test/test_rds_visit_store.py` — the multi-row `ON CONFLICT DO
  NOTHING` insert returns the inserted count (not the submitted count), and
  `purge_user_engagement` includes `venue_visit`.

Manual or integration checks:

- Apply `0044` against RDS manually (`docker exec vibes_bot-cs-server-1 alembic
  upgrade head`) **before** the vibes_bot release — deploy does not run alembic.
- Confirm `ENGAGEMENT_VISIT_TOTAL` appears on `/metrics` after the first batch.

## Acceptance Criteria

- `POST /v1/visits` persists one `engagement.venue_visit` row per accepted
  visit, keyed by the HMAC pseudonym.
- Replaying an identical batch writes no additional rows and reports every visit
  as `duplicate`.
- Two visits to the same venue on the same Recife day, with distinct
  `client_visit_id`s, both persist.
- An invalid visit is dropped and counted without failing the rest of its batch.
- `business_period` is always the America/Recife calendar day of `arrived_at`,
  computed server-side.
- `DELETE /v1/user-data` leaves zero `venue_visit` rows for that user and
  reports the deleted count.
- No raw user id and no coordinates appear in any log line on this path.
- `0044` is alembic head and applies cleanly on a database already at `0043`.

## Open Questions

- None blocking. **Follow-up, out of scope:** this table has no retention
  policy, so a location history grows unbounded. A retention window (and the
  privacy-policy sentence that states it) should be planned before the dataset
  is large enough to matter.
