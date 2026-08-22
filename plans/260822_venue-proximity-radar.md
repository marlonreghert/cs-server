# Venue Proximity (Radar) — staging, aggregate, and notification ledger

## Branch
feature/venue-proximity-radar

## Goal

Persist the **pass-by** dataset: which venues a consenting user passed near, in
which band, in which part of which day — so the product can understand where
users go, not only where they stop.

cs-server must accept a batch of device-computed proximity day-rows, store them
in a short-lived staging table, roll them up nightly into a k≥5 venue-day
aggregate, and hold the notification ledger that caps how often the Radar alert
may fire. It must never hold a movement trace longer than the staging window.

Depends on `plans/260822_venue-visit-tracking.md` (Tier A) having landed:
`0044`, the engagement write surface, and the erasure path are prerequisites.

## Non-goals

- **No metres.** No `min_distance_meters`, no `max_speed_kmh`. See Desired
  Behavior 2 — both were cut on measurement and privacy grounds, and re-adding
  either reopens a settled decision.
- **No per-encounter grain, no timestamps finer than a 6-hour day-part.** No
  `first_seen`, `last_seen`, `dwell`, or encounter id on this path.
- **No changes to `engagement.venue_visit`.** No `kind` discriminator. Visits and
  proximity are different row shapes with different retention; the `0029`
  blocked-venues precedent created a new table for a far more similar pair.
- **No read API for proximity, ever** — not the staging table, not the
  aggregate, not through admin. The aggregate is queried directly against RDS by
  operators.
- **No detection or distance estimation.** The device computes the band; this
  service validates and stores it.
- **No notification delivery.** cs-server holds the ledger that bounds the rate;
  vibes_bot decides and sends.

## Evidence

- `plans/260822_venue-visit-tracking.md` — the Tier A plan this builds on: the
  engagement write surface, `pseudonymize()`, `purge_user_engagement`, the
  apply-manually migration warning, and the retention job this plan extends.
- `migrations/versions/0029_blocked_venues.py` — the precedent for a new table
  over a discriminator on an existing one.
- `migrations/versions/0012_engagement_app_session_day.py:11-13` — deploy does
  **not** run alembic; migrations are applied manually against RDS.
- `app/services/engagement_service.py` — `pseudonymize()` and the erasure
  ordering this path must extend. Note that the existing engagement pseudonym is
  projected to Redis under the **raw** uid for favorites/blocks/hot-likes, so it
  is recoverable by set-matching — which is why proximity needs its own key.
- `app/dao/rds_venue_store.py` `purge_user_engagement()` — must gain the new
  tables or a deleted user's trace survives.
- `main.py` `register_*_jobs` — the background-job skeleton the rollup and
  partition-drop jobs must register through to inherit `BACKGROUND_JOB_*`
  instrumentation and the concurrency lock.
- `infra/rds/main.tf` / `variables.tf:44` — `allocated_storage` default 20 GB
  with **no** `max_allocated_storage`. Autoscaling is off.

## Current Behavior

After Tier A, cs-server stores one row per venue visit with an arrival timestamp
and a dwell, retained 12 months. Nothing records that a user passed *near* a
venue without entering it, and nothing bounds how often the product may
proactively notify a user.

## Desired Behavior

1. **Accept a batch of proximity day-rows.** `POST /v1/proximity` takes a
   `location_pseudo` (already pseudonymized by vibes_bot — see 6) and a list of
   rows, each `{venue_id, business_period, day_part, band, sample_count,
   best_accuracy_m, median_gap_s, platform, fg_service_active,
   estimator_version}`.

2. **Store a band, never a distance.** `band` is `1` (`at`, ≤120 m) or `2`
   (`passing`, 120–250 m). A precise metre value is not storable for two
   independent reasons: the sampling gap and the GPS accuracy radius are both
   larger than the quantity, so the estimator's bias flips sign with speed; and
   integer metres against several simultaneous known venue anchors multilaterate
   back to the user's actual position, which makes the row a positioning system
   rather than a preference signal. Two bands over a 250 m ring is the coarsest
   encoding that still answers "was at" versus "passed nearby".

3. **Idempotency is structural, not contractual.** The natural key
   `(location_pseudo, venue_id, business_period, day_part)` *is* the idempotency
   key, so there is no client-minted id to go stale. Conflicts merge
   monotonically — `band = LEAST(...)` so `at` beats `passing`, `sample_count =
   GREATEST(...)`, `best_accuracy_m = LEAST(...)` — which converges under the
   router's mandated retry regardless of arrival order. Report `accepted` /
   `merged` / `rejected`; **not** `duplicate`. With `DO UPDATE`, Postgres counts
   updated rows in `rowcount`, so the visits endpoint's
   `len(rows) - inserted` arithmetic would report zero forever. Use
   `RETURNING (xmax = 0)`.

4. **Staging is short-lived and dropped by partition.** `proximity_stage` is
   `PARTITION BY RANGE (business_period)`, one partition per day, and retention
   is a `DROP TABLE` of the expired partition. A `DELETE`-based job is not
   affordable on a `db.t4g.small`, and the partition key must be chosen now —
   deciding it later means a second migration over the largest table in the
   database.

5. **Only the k≥5 aggregate survives.** A nightly job rolls staging into
   `venue_proximity_daily` with `distinct_users >= 5` enforced as a **CHECK
   constraint, not a convention** — a cell with one distinct user is a
   re-identifiable trace row with extra steps. Sub-k cells roll to the ISO week
   and are re-tested; still-suppressed cells are dropped. This preferentially
   deletes isolated venues in emerging neighbourhoods, which is a real cost to
   the product question and is accepted rather than worked around.

6. **A separate pseudonymization key.** `location_pseudo = HMAC(K_loc, uid)`
   where `K_loc` ≠ `ENGAGEMENT_PSEUDONYMIZATION_KEY`, rotated every 90 days with
   rows under the retired key hard-dropped. The existing engagement pseudonym is
   effectively invertible in this system, because cs-server projects favorites,
   blocks and hot-likes to Redis keyed by the **raw** uid — set-matching recovers
   the mapping. Reusing it would extend that weakness to a location trace.
   vibes_bot computes it and cs-server never sees the raw uid on this path.

7. **Blocked venues are suppressed as a ring, not a point.** When a user has
   blocked venue V, ingest drops every proximity row from that user for any
   venue within `2 × R_out` (500 m) of V. Suppressing only V is theatre:
   multilaterating from the unblocked neighbours reconstructs presence at V.
   Enforced server-side even though the device also filters, because a
   client-side privacy promise is unauditable.

8. **Erasure cannot be undone by a buffered device.** `purge_user_engagement`
   gains both tables, and an `engagement.erasure_tombstone(pseudo, erased_at)`
   row is written. Ingest drops any row whose `business_period` precedes the
   tombstone; `DELETE /v1/user-data` returns a `buffer_epoch` the device must
   persist and honour. Without this a device holding buffered rows recreates a
   partial history under the same deterministic pseudonym within one sync.

9. **Bound the Radar notification rate durably.** `engagement.radar_notification`
   records `(location_pseudo, venue_id, sent_at)`. The cap must live server-side
   so a reinstall cannot reset it — a device-local counter is a rate limit a user
   can clear by deleting the app, which is exactly the failure mode that turns a
   rare, welcome alert into a complaint.

## Implementation Approach

**Migration** `0045_engagement_proximity`, `down_revision =
"0044_engagement_venue_visit"`, raw SQL in `UPGRADE`/`DOWNGRADE` via
`op.execute`, carrying `0012`'s apply-manually warning. Creates the three tables,
the daily partitions for the live window, and a partition-creation helper.

**DAO** — `RdsVenueStore.upsert_proximity_rows(location_pseudo, rows) -> dict`
(one multi-row `INSERT ... ON CONFLICT DO UPDATE ... RETURNING (xmax = 0)`),
`rollup_proximity_day(business_period)`, `drop_proximity_partition(date)`,
`record_radar_notification(...)`, `count_radar_notifications(pseudo, since)`,
plus the `purge_user_engagement` and tombstone extensions.

**Service** — `ProximityService` validates each row, applies the block-ring
suppression and the per-user-day cap, and returns the counts. Validation floors
are config, not literals.

**Router** — `POST /v1/proximity` on the engagement router. Oversized batches
**truncate and report the surplus as rejected**, never 422 — a device cannot
re-split a batch it already built.

**Jobs** — a nightly rollup and a partition-drop job, both registered through
`main.py`'s `register_*_jobs`.

## Data, Config, And API Impact

```
engagement.proximity_stage                    PARTITION BY RANGE (business_period)
  location_pseudo text NOT NULL          -- HMAC(K_loc, uid); NOT the engagement pseudonym
  venue_id        text NOT NULL          -- no FK (same soft-delete race as visits)
  business_period date NOT NULL          -- partition key, America/Recife
  day_part        smallint NOT NULL      -- 1=06-12 2=12-18 3=18-00 4=00-06
  band            smallint NOT NULL      -- 1='at' (<=120m) 2='passing' (120-250m)
  sample_count    smallint NOT NULL
  best_accuracy_m smallint NOT NULL
  median_gap_s    smallint NOT NULL
  platform        text NOT NULL          -- 'ios' | 'android'
  fg_service_active boolean NULL         -- Android only
  estimator_version smallint NOT NULL
  created_at      timestamptz NOT NULL DEFAULT now()
  PRIMARY KEY (location_pseudo, venue_id, business_period, day_part)
  CHECK (band IN (1,2)) CHECK (day_part BETWEEN 1 AND 4)
  CHECK (best_accuracy_m BETWEEN 1 AND 60) CHECK (sample_count > 0)

engagement.venue_proximity_daily
  venue_id text, business_period date, day_part smallint, band smallint,
  distinct_users integer NOT NULL, row_count integer NOT NULL
  PRIMARY KEY (venue_id, business_period, day_part, band)
  CHECK (distinct_users >= 5)

engagement.radar_notification
  location_pseudo text, venue_id text, sent_at timestamptz NOT NULL
  INDEX (location_pseudo, sent_at DESC)

engagement.erasure_tombstone
  pseudo text PRIMARY KEY, erased_at timestamptz NOT NULL
```

The four provenance columns are **mandatory**. Android's background location is
throttled and its foreground service cannot start from the background, so an
Android device after a reboot contributes at a structurally lower sample rate
than an iOS device. Without `platform`, `median_gap_s` and `fg_service_active`,
any per-venue count measures the device population rather than user behaviour,
and no analysis can tell the two apart afterwards.

**API** — `POST /v1/proximity`; `DELETE /v1/user-data` gains `proximity_stage`
and `radar_notifications` counts plus `buffer_epoch`.

**Config** — `PROXIMITY_MAX_BATCH_SIZE` (500), `PROXIMITY_STAGE_RETENTION_DAYS`
(7), `PROXIMITY_MAX_VENUES_PER_USER_DAY` (60), `PROXIMITY_MAX_ACCURACY_M` (60),
`PROXIMITY_BLOCK_RING_METERS` (500), `PROXIMITY_ROLLUP_MIN_USERS` (5),
`LOCATION_PSEUDONYMIZATION_KEY`, `LOCATION_PSEUDONYM_ROTATION_DAYS` (90).

**Infrastructure precondition** — `max_allocated_storage = 100` on the RDS
instance, via Terraform, merged **before** any proximity ingest ships. Storage
autoscaling is currently off at 20 GB; an append-only table on a volume that
cannot grow is a write outage for the entire database, pipelines included.

## Error Handling And Observability

Per-row validation failures are counted and dropped, never fatal to the batch. A
transaction failure is a 502 with the retry detail; the upsert is idempotent so
retries converge. Logs carry the pseudonym and counts — never a raw uid, never
coordinates, never a venue sequence.

Metrics: `ENGAGEMENT_PROXIMITY_TOTAL{outcome=accepted|merged|rejected}`,
`ENGAGEMENT_PROXIMITY_REJECTED_TOTAL{reason=band_invalid|accuracy_out_of_range|
day_part_invalid|user_day_cap|block_ring|tombstoned}`,
`PROXIMITY_SAMPLE_GAP_SECONDS`, `PROXIMITY_ROLLUP_SUPPRESSED_CELLS`,
`PROXIMITY_STAGE_PARTITIONS_DROPPED`, `RADAR_NOTIFICATION_CAP_HIT_TOTAL`.

## Test Plan

Feature file: `tests/bdd/api/venue-proximity-radar.feature`

Scenarios:

- A batch of proximity rows is persisted under the location pseudonym, not the
  engagement pseudonym and not the raw id.
- Re-uploading the same natural key **merges** rather than duplicating, and the
  response reports `merged` rather than `duplicate`.
- `at` beats `passing` when two rows collide on the natural key, in either
  arrival order.
- A row with accuracy above the ceiling is rejected.
- An invalid band or day_part is rejected without failing the batch.
- The per-user-day venue cap drops the surplus and counts it.
- A row for a venue inside the block ring of a blocked venue is suppressed —
  asserting the **ring**, not just the blocked venue itself.
- A row whose business period precedes the user's erasure tombstone is dropped.
- An oversized batch truncates and reports the surplus, returning 200.
- The nightly rollup emits only cells with ≥5 distinct users; a 4-user cell is
  suppressed and counted.
- The expired staging partition is dropped and its rows are gone.
- Account deletion removes staging rows, the notification ledger, and returns a
  buffer epoch; the aggregate is untouched.
- The radar ledger enforces the cap across a simulated reinstall.
- No raw uid and no coordinates appear in logs on this path.

Pytest unit tests:
- `tests/unit_test/test_proximity_validation.py` — each rejection reason, the
  day-part derivation, and the monotone merge arithmetic.
- `tests/unit_test/test_proximity_rollup.py` — k-suppression, weekly re-test.
- `tests/unit_test/test_location_pseudonym.py` — `K_loc` distinct from the
  engagement key; rotation drops retired-key rows.

Manual or integration checks:
- Apply `0045` manually before the vibes_bot release.
- Confirm `max_allocated_storage` is set first.

## Acceptance Criteria

- Proximity rows persist as bands with provenance; no metres or speed exist
  anywhere in the schema.
- The natural key makes re-upload idempotent and order-independent.
- Staging rows do not survive `PROXIMITY_STAGE_RETENTION_DAYS`, enforced by
  partition drop.
- No aggregate cell with fewer than 5 distinct users is readable.
- A blocked venue's ring is suppressed, not just the venue.
- Erasure survives a device that still holds buffered rows.
- `K_loc` is distinct from the engagement key.

## Open Questions

- **Is any proximity-derived output shared outside the company** (e.g. with venue
  partners)? If so this becomes a data-sharing question with Play Data-safety and
  ATT consequences. Working constraint until answered: **the k≥5 venue-day
  aggregate is the only artefact permitted to leave the per-user tier.**
