# RDS Schema Normalization (Address Table, Admin Config, Drop Payload Duplication)

Umbrella plan covering three persistence design-smell fixes in the freshly
migrated RDS system-of-record. Single branch, three sequenced steps: **Ex1 drop
venue payload duplication → Ex3 address table → Ex2 admin config normalization**.
Ex1 leads (despite being the most destructive) because it makes venue
reconstruction column-based from the start, so Ex3 becomes a clean address
source-swap with no payload-overlay hybrid window. Each step is its own
expand → backfill → verify → cutover → contract migration on a **live, populated**
database, gated by a full-dataset equivalence harness that compares the old
("v1") and new ("v2") shapes — in RDS **and** in the Redis serving projection —
before any irreversible contract. Serving output must not change.

## Branch
chore/rds-schema-normalization

## Goal
Remove three design smells in `app/dao/rds_venue_store.py` + the Alembic schema
without changing externally observable serving behavior:

- **Ex1 — Drop venue payload duplication.** Make the relational columns on
  `venues.venue` the sole source of truth for scalar fields and stop storing
  those same fields again inside `payload`. Keep only a slim residual JSON column
  holding the genuinely-nested fields columns cannot hold
  (`venue_foot_traffic_forecast`, `venue_dwell_time_min/max`) — the "justified
  exception" the original RDS plan explicitly anticipated.
- **Ex3 — Structured address table.** Extract `venue_address`, `venue_lat`,
  `venue_lng` off `venues.venue` into a referenced `venues.address` table with
  structured components (street / neighborhood / city / postal_code) plus raw
  text and lat/lng. lat/lng stay reachable so the Redis geo index still rebuilds.
- **Ex2 — Admin config normalization.** Break the monolithic
  `admin.admin_config` JSONB blobs (the painful one is `venue_eligibility`) into
  typed rows/columns so editing a single rule is a one-row insert/delete, and
  move runtime readers off the monolithic JSON mirror to read the normalized
  representation. (cs-server readers in scope; vibes_bot is a coordinated
  cross-repo dependency — see Non-goals + Sequencing.)

## Non-goals
- No change to the public venue API response shape or serving semantics
  (`MinifiedVenue` / `VenueWithLive` output is byte-stable).
- No change to Redis serving key formats or the RDS-as-truth / Redis-as-projection
  model. The projector stays the sole Redis writer for venue/enrichment data.
- No live-busyness remodeling (stays current-state in `besttime.live_forecast`,
  served via Redis).
- **Ex3:** no address-parser/geocoder backfill of structured components. Existing
  free text + lat/lng are backfilled; `street`/`neighborhood`/`city`/`postal_code`
  stay null until Google Places enrichment populates them going forward.
- **Ex2:** no vibes_bot code change in this repo's branch. vibes_bot currently
  reads the Redis `admin_config:venue_eligibility` mirror; its migration onto the
  normalized representation is a separate, coordinated change. The Redis mirror is
  retained as a compatibility shim until vibes_bot has cut over (the Ex2 contract
  step that removes the mirror is gated on that).
- No new enrichment-table denormalization. The 1–2 promoted columns on
  `vibe_attributes` / `instagram.handle` plus their whole-model payloads are an
  accepted exception (they are read back as whole Pydantic models), not in scope.

## Evidence
- `venues.venue` carries promoted columns **and** `payload jsonb` (full Venue
  model): `migrations/versions/0001_baseline_schemas.py:24-44`. The "JSONB payload
  + promoted columns" choice is documented as deliberate in
  `plans/rds_system_of_record_01_06_26.md:446-450` — Ex1 knowingly overrides it.
- Venue reconstruction reads **only** `payload`, never the columns:
  `app/dao/venue_repository.py:54-56` (`get_venue`), `:106-113` (`list_all_venues`),
  and the projector `app/services/redis_projection_service.py:73-75`
  (`Venue.model_validate(row["payload"])`). This is the Ex1 blast radius.
- Non-column Venue fields live only in payload and are populated at discovery:
  `app/models/venue.py:103-106` (`priority`, `venue_foot_traffic_forecast`,
  dwell) and `app/services/venues_refresher_service.py:488-503` (one-day
  forecast + dwell built from the BestTime filter result). These are the Ex1
  residual-JSON fields.
- lat/lng are explicitly "kept so Redis geo index is rebuildable":
  `migrations/versions/0001_baseline_schemas.py:28-29`. Ex3 must preserve that.
- Admin config is a single `(key, value jsonb)` table:
  `migrations/versions/0001_baseline_schemas.py:124-128`. Keys in use:
  `venue_eligibility`, `discovery_points`, `venue_monthly_budget`,
  `venue_photos_cache_ttl_days`.
- The eligibility blob is large and nested (four string lists):
  `app/services/venue_eligibility.py:150-230` (`EligibilityConfig.from_dict`).
  Editing one keyword today means rewriting the whole JSON.
- The write-through + Redis mirror that BOTH cs-server and vibes_bot read:
  `app/services/admin_config_service.py:41-68`; the cs-server reader
  `app/services/venue_eligibility.py:316-345` (`load_eligibility_config`) parses
  the Redis mirror.
- Behaviour contract for the store is the in-memory fake `tests/rds_fake.py`
  (`InMemoryRdsVenueStore`), proven by `tests/test_rds_store_contract.py`,
  `tests/test_rds_repository.py`, `tests/test_admin_config.py`,
  `tests/bdd/persistence/*`. Every schema/store change must update the fake in
  lockstep so the offline suite stays the contract.

## Current Behavior
- A venue is written by serializing the whole `Venue` model into `payload` plus a
  parallel set of promoted scalar columns; reads/projection reconstruct the model
  from `payload` and ignore the columns except for filtering/ordering. The same
  scalar values therefore exist twice, and can silently drift.
- Address lives as three inline columns on `venues.venue`; there is no structured
  address representation.
- Admin config is one JSONB value per key. Tuning one eligibility keyword means
  read-modify-write of the entire blob through the admin endpoint; the value is
  mirrored to Redis and every runtime reader parses that mirror.

## Desired Behavior
- **Ex1:** `venues.venue` stores each scalar exactly once (the column). The
  residual JSON holds only the nested fields. A venue reconstructs identically
  (columns + residual) and projects to Redis identically; no scalar can drift
  between a column and the payload because the payload no longer holds it.
- **Ex3:** Each venue references exactly one `venues.address` row. Venue
  reconstruction yields the same `venue_address` / `venue_lat` / `venue_lng` as
  today, and the Redis geo index rebuilds identically. Structured components are
  available (null until enrichment fills them) without changing serving output.
- **Ex2:** An operator can add/remove a single eligibility rule as a one-row
  change. The effective eligibility config computed from the normalized rows is
  identical to the config the JSON blob produced. cs-server runtime readers read
  the normalized representation directly. The Redis mirror remains available for
  vibes_bot until it migrates.

## Implementation Approach

### Pre-execution rollback gate (blocking, before any step)
Before `/execute-feature` runs **any** migration, capture a full, restorable copy
of the live RDS to the operator's own device:

- Take a full logical dump (`pg_dump` of all schemas — `venues`, `besttime`,
  `google_places`, `instagram`, `admin`, `engagement`, `audit`) over the SSM
  tunnel and **download it to the local device**, plus trigger an RDS snapshot as
  a secondary. Record the dump's row counts per table.
- **Verify the dump restores** into a throwaway local Postgres and that row
  counts match before proceeding. An unverified backup is not a rollback plan.
- Rollback contract: if any step misbehaves, restore from the local dump. Stale
  `besttime.live_forecast` after a restore is **acceptable** — live busyness is
  high-churn and self-heals on the next live cron, and Redis is only a projection
  rebuilt from RDS. Treat the dump as the ultimate rollback; each step's
  expand/contract reversibility (below) is the first-line rollback.
- Handle the dump as sensitive: it contains `google_places.reviews` author names
  (third-party PII) and pseudonymized `engagement` data. Store encrypted, never
  commit it, delete after the change is confirmed stable. Do not paste its
  contents into logs, code, or docs.

This gate is documented as the first acceptance criterion and the first
`/execute-feature` action; no DDL/backfill begins until the verified local dump
exists.

### General migration discipline (all three steps)
Every step follows expand → backfill → verify → cutover → contract as separate,
reversible moves, never a single destructive ALTER:

1. **Expand:** add the new ("v2") shape additively; keep the old ("v1") shape
   intact and still written (dual-write). v1 stays authoritative until cutover.
2. **Backfill:** populate the v2 shape from existing rows in batches.
3. **Verify:** the equivalence harness (see *Data integrity & equivalence
   verification*) proves the v2 shape reconstructs identically to the retained v1
   shape for **every** row — in RDS and in a Redis shadow projection — before any
   read flips.
4. **Cutover:** flip reads/reconstruction to the v2 shape. While v1 is still
   present, first-line rollback is a read-flip back to v1.
5. **Contract:** stop writing and drop the v1 shape only after a soak window and a
   green full-dataset diff — this is the irreversible move and goes in its own
   migration/PR-able commit.

### Step Ex1 — drop venue payload duplication (first; untangles reconstruction)
Doing Ex1 first makes venue reconstruction **column-based from the start**, so the
later address step is a clean source-swap with no payload overlay. The destructive
change runs immediately after the freshest verified backup.

- New migration `0003_venue_residual_payload`: add a slim residual JSON column
  (`extra jsonb`) holding ONLY the genuinely-nested fields columns cannot hold —
  `venue_foot_traffic_forecast`, `venue_dwell_time_min`, `venue_dwell_time_max`
  (plus any other non-column nested field found during execution). Keep the full
  `payload` column during expand as the **v1 golden baseline** to diff against.
- All scalar fields (`venue_name`, `venue_type`, `price_level`, `rating`,
  `reviews`, `forecast`, `processed`, `priority`, lifecycle/deprecation,
  `google_business_status`) read from columns. Address still reads from the
  existing `venue_address`/`venue_lat`/`venue_lng` columns at this point (Ex3 has
  not moved them yet); Ex3 later repoints only the address source.
- Reconstruction path: a single `_venue_from_row(columns, residual)` helper
  rebuilds the `Venue`, replacing the three current `Venue.model_validate(payload)`
  sites (`get_venue`, `list_all_venues`, projector) so they cannot diverge.
- Expand keeps writing the full `payload` too (dual-write). Verify: the
  equivalence harness asserts `_venue_from_row(...)` deep-equals
  `Venue.model_validate(payload)` for 100% of venues, and the Redis shadow
  re-projection matches the pre-change snapshot, before cutover.
- Contract (`0003b`): stop writing `payload`, drop the column. Irreversible move;
  ultimate rollback is the dump.
- `tests/rds_fake.py` reconstructs the same way (the offline contract encodes
  "columns are truth").

### Step Ex3 — `venues.address` (second; clean source-swap after Ex1)
Because Ex1 already made reconstruction column-based, Ex3 only repoints the
address portion — **no payload overlay, no hybrid window.**

- New migration `0004_address_table`: create `venues.address` with `venue_id`
  PK/FK → `venues.venue(venue_id)`, `raw_text text`, `street`, `neighborhood`,
  `city`, `postal_code` (all nullable), `lat double precision NOT NULL`,
  `lng double precision NOT NULL`, `updated_at`. Keep `venues.venue.venue_address`
  / `venue_lat` / `venue_lng` during expand as the v1 golden baseline.
- Backfill one `venues.address` row per venue from the existing columns
  (`raw_text` = `venue_address`, lat/lng copied; structured components null).
- `RdsVenueStore.upsert_venue` dual-writes address (columns + table) during
  expand. Cutover: `_venue_from_row` and the geo-index rebuild source address +
  lat/lng from `venues.address` instead of the venue columns — a single input
  swap, no payload involved (Ex1 already removed it).
- Verify: the harness diffs address-from-table reconstruction against
  address-from-columns for 100% of venues, and the Redis shadow projection
  (geo members + coordinates + sampled radius-query results) against the snapshot.
- Contract (`0004b`): drop `venue_address` / `venue_lat` / `venue_lng` from
  `venues.venue`. lat/lng now live only on `venues.address` and feed the geo
  rebuild; they are not duplicated anywhere.
- Update `tests/rds_fake.py` to model the address table and the source swap.

### Step Ex2 — admin config normalization (third; independent of reconstruction)
- New migration `0005_admin_config_normalize`:
  - `admin.eligibility_rule (rule_type text, value text, updated_by, updated_at,
    PRIMARY KEY (rule_type, value))` where `rule_type ∈ {blocked_venue_type,
    blocked_google_type, hard_blocked_name_keyword, ambiguous_name_keyword}`. One
    rule = one row; add/remove = one-row insert/delete.
  - Promote the genuine scalar configs to typed storage (a small typed
    `admin.setting` table or typed columns): `venue_monthly_budget` and
    `venue_photos_cache_ttl_days` are ints that gain little from JSON.
    `discovery_points` is a `list[dict]` (machine-managed via `recount`, not
    hand-tuned), so it stays JSON (in `admin.setting`/a JSON column) unless
    per-point editing is wanted — then a small `admin.discovery_point` rows table.
- Backfill: expand the current `admin.admin_config` JSON values into rows. The
  effective eligibility config assembled from rows must equal
  `EligibilityConfig.from_dict(<old blob>)` field-for-field (an explicit parity
  assertion, accounting for the defaults-merge + `blocked_name_keywords` alias in
  `venue_eligibility.py:186-213`).
- New admin endpoints/store methods for single-rule add/remove and scalar
  get/set, plus an "assemble effective config" read used by runtime.
- Read path (decided during execution): **rows are the source of truth; the Redis
  mirror is demoted to a derived projection** — `AdminConfigService` reassembles
  an equivalent `admin_config:venue_eligibility` JSON from the rows on every write
  (same effective config; the block-lists are membership sets, so list element
  order may normalize — parity is checked on the effective config, not bytes). Hot/runtime readers (serving `venue_handler`, the refresh sweep) keep
  reading the fast mirror **unchanged** (no RDS round-trip per nearby request);
  only the low-frequency **admin GET** reads the rows directly (durable truth +
  per-rule metadata). This matches the RDS-truth/Redis-projection model used for
  venues, is lower-risk (serving + mirror bytes unchanged), and the operability
  win (one-row edits, queryable rules, the views) is fully delivered. Scope note:
  this expand normalizes **eligibility only**; the scalar configs (budget,
  photos-TTL, discovery_points) gain little and are deferred to a small follow-up.
- **Mirror retained** for vibes_bot: `AdminConfigService` keeps writing the
  reassembled `admin_config:venue_eligibility` JSON to Redis (identical shape) so
  vibes_bot is untouched until its own coordinated migration. The contract step
  (remove the mirror + the old `admin_config` rows) is **gated on vibes_bot
  cutover** and lives in a later migration `0005b` outside this branch's required
  scope.
- Validation: reject unknown `rule_type`, enforce per-type normalization (upper
  for besttime types, lower for google types/keywords) so byte-compatibility with
  the runtime reader is preserved. Keep the hardcoded defaults as the fallback
  when no rows exist (a wiped table must not break filtering —
  `venue_eligibility.py:316-345` semantics preserved).
- **Observability views** (created in the same migration; read-only, no write
  path, freely drop/recreate so no rollback concern). They surface the *effect* of
  the config — catalog impact, not just the stored rules:
  - `admin.v_blocked_google_type_effect` — per blocked Google type: how many
    venues carry that type and their lifecycle split, so a rule's blast radius and
    any drift (still-`active` venues the rule should catch) are visible. Joins the
    new rule rows to the promoted `google_primary_type` column:
    ```sql
    CREATE VIEW admin.v_blocked_google_type_effect AS
    SELECT r.value AS google_type,
           count(va.venue_id)                                       AS venues_with_type,
           count(*) FILTER (WHERE v.lifecycle_status = 'active')    AS active,
           count(*) FILTER (WHERE v.lifecycle_status = 'deprecated') AS deprecated
    FROM admin.eligibility_rule r
    LEFT JOIN google_places.vibe_attributes va
           ON va.google_primary_type = r.value AND va.deleted_at IS NULL
    LEFT JOIN venues.venue v ON v.venue_id = va.venue_id
    WHERE r.rule_type = 'blocked_google_type'
    GROUP BY r.value;
    ```
  - `admin.v_rejection_reason_effect` — per rejection reason: how many venues are
    deprecated under it, with category/description and recency. Uses only existing
    tables (`admin.rejection_reason` + `venues.venue.deprecated_reason`), so it can
    ship independently of Ex2:
    ```sql
    CREATE VIEW admin.v_rejection_reason_effect AS
    SELECT rr.code, rr.category, rr.description,
           count(v.venue_id)    AS deprecated_venues,
           max(v.deprecated_at) AS last_deprecated_at
    FROM admin.rejection_reason rr
    LEFT JOIN venues.venue v
           ON v.deprecated_reason = rr.code AND v.lifecycle_status = 'deprecated'
    GROUP BY rr.code, rr.category, rr.description;
    ```

### Data integrity & equivalence verification (RDS + Redis)
The cutover gate for every step is a concrete, durable, full-dataset comparison —
not a spot check. Mechanism: keep the previous shape as a **golden "v1"**
representation alongside the new **"v2"** shape (dual-write during expand) and
prove they reconstruct identically before the irreversible contract.

- **Canonicalizer (the definition of "equal").** One shared function canonicalizes
  a reconstructed object for comparison: `model_dump(by_alias=True, mode="json")`
  with sorted keys, a float tolerance for lat/lng + rating, consistent
  None/absent handling, normalized datetimes, list order preserved where it is
  semantic (foot-traffic days) and sorted where set-like (eligibility rules).
  Unit-tested; it is the contract for equivalence and is reused on both sides.
- **RDS golden diff (100% of rows).** A read-only, re-runnable job reconstructs
  each entity from the v1 shape and the v2 shape, canonicalizes both, and asserts
  deep equality. Mismatches are written to a durable report (an `audit`-style
  table or a diff manifest) keyed by `venue_id` / `rule_type` with the diverging
  field name only — never payload secrets/PII. Cutover requires zero mismatches
  across the whole dataset, plus matching per-table row counts and an aggregate
  content checksum as a cheap tripwire.
- **Redis serving equivalence (shadow projection).** Redis is the user-facing
  surface, so verify it explicitly rather than trusting RDS parity alone:
  1. Snapshot the current Redis serving state (per-venue serving value, geo-index
     members + coordinates, `admin_config:*` mirror) as the golden snapshot.
  2. Run the projector from the v2 RDS shape into a **separate Redis keyspace /
     logical DB** (shadow projection) — production serving keys untouched.
  3. Diff the shadow projection against the golden snapshot field-by-field, after
     normalizing volatile fields (TTL, `updated_at`) and **exempting live
     busyness** (allowed to be stale/absent, per the rollback policy). Assert
     identical geo membership, coordinates (within tolerance), and a sample of
     radius-query results. Flip the live projector only after the shadow matches.
- **Rollback = read-flip, then dump.** Until a step's contract runs, the v1 shape
  is intact and authoritative, so first-line rollback is "stop reading v2"; the
  verified local dump is the ultimate backstop.

## Data, Config, And API Impact
- **Migrations (execution order):** `0003`/`0003b` (Ex1 venue residual),
  `0004`/`0004b` (Ex3 address), `0005`/`0005b` (Ex2 admin config; `0005b` gated on
  vibes_bot). Run manually via SSM, never on container startup (per `alembic.ini`).
- **Schema:** `venues.venue` `payload` becomes a slim `extra` residual (Ex1) and
  loses `venue_address`/`venue_lat`/`venue_lng` (→ `venues.address`, Ex3); new
  `admin.eligibility_rule` (+ scalar setting storage, Ex2).
- **API:** the public venue API and admin config endpoints keep their external
  contract; new admin endpoints/verbs are added for single-rule editing
  (additive). No serving response shape change.
- **Config / flags:** no app feature flags required; the expand/contract gating is
  done by migration ordering + the equivalence harness, not runtime flags.
  Settings (`app/config.py`) for eligibility/budget/photo-TTL keep their current
  external meaning.
- **Cross-repo:** vibes_bot keeps reading the Redis mirror until separately
  migrated; this branch must not remove the mirror.

## Error Handling And Observability
- **Backups:** the pre-execution dump + restore-verify is a hard gate; record
  per-table row counts as the parity baseline.
- **Equivalence harness (RDS + Redis):** each step's cutover is gated on the
  full-dataset golden diff — v2 reconstruction deep-equals the retained v1 shape
  for 100% of rows, with matching row counts + aggregate checksum, AND a Redis
  shadow re-projection matches the pre-change serving snapshot (live busyness
  exempt). Mismatches are written to a durable diff report keyed by `venue_id` /
  `rule_type` with the diverging field only — never payload secrets/PII. Any
  mismatch blocks cutover and is logged with row context.
- **Migrations:** idempotent (`IF NOT EXISTS` / guarded), with working
  `downgrade()` for the reversible (expand/backfill/cutover) steps; the contract
  step's downgrade is documented as "restore from dump" where a column drop is
  not cleanly reversible.
- **Metrics:** reuse existing venue/data-quality gauges in `app/metrics.py`;
  add migration/parity counters (rows checked, mismatches, shadow-diff result) for
  each step's verification path. Eligibility-rule edits should be observable
  (count of effective rules per type).
- No new external dependency; degrade to hardcoded eligibility defaults if the
  normalized rows are empty/unreadable (preserve current fail-safe).

## Test Plan
Feature file: `tests/bdd/persistence/rds-schema-normalization.feature`

Scenarios (observable invariants — the whole risk is silent data drift):
- **Ex1 reconstruction parity:** Given a venue stored with columns + residual
  JSON, when reconstructed by repo and projector, then the `Venue` equals the
  model rebuilt from the old full payload, and it projects to Redis identically.
- **Ex1 no scalar in residual:** Given a written venue, then the residual JSON
  contains only nested fields (no `venue_name`/`rating`/etc.), so no scalar can
  drift.
- **Ex3 address parity:** Given a venue with an address and coordinates, when its
  address is served from `venues.address`, then the reconstructed
  address/lat/lng equal the pre-migration values and the venue stays in the geo
  index at the same coordinates.
- **Ex3 components optional:** Given a backfilled venue with no structured
  components, when reconstructed, then serving output is unchanged and components
  are absent until enrichment provides them.
- **Ex2 single-rule edit:** Given the normalized eligibility rules, when an
  operator adds one blocked keyword as a single row, then a venue matching that
  keyword becomes ineligible and no other rule changes.
- **Ex2 effective-config equivalence:** Given the rules backfilled from the old
  JSON blob, when the effective config is assembled, then it equals the config
  the JSON blob produced (including defaults-merge and the
  `blocked_name_keywords` alias).
- **Ex2 fail-safe:** Given the rule table is empty/unreadable, when eligibility
  is evaluated, then it falls back to the hardcoded defaults (filtering never
  breaks).
- **Ex2 mirror retained:** Given a config change, then the Redis
  `admin_config:venue_eligibility` mirror is still written in the same JSON shape
  (vibes_bot compatibility).
- **Equivalence harness — RDS golden diff:** Given venues stored in both the v1
  and v2 shapes, when the golden diff runs over all rows, then it returns zero
  mismatches; and when one row is perturbed, it reports a non-passing result
  naming the exact `venue_id`/field with no payload secrets.
- **Equivalence harness — Redis shadow projection:** Given the v2 RDS shape, when
  the projector re-projects into a shadow keyspace, then the shadow serving values
  and geo membership/coordinates equal the pre-change snapshot, with live busyness
  exempt from the comparison.

`# bdd-exempt` (covered by the plan + acceptance criteria, not Gherkin): the
pre-execution local-dump rollback gate and the migration mechanics
(pg_dump/SSM/restore, expand/contract DDL ordering) are operator runbook +
infrastructure — the application cannot verify a dump on the operator's device.
Same posture as `admin_config_rds.feature`.

Pytest unit tests:
- `tests/rds_fake.py` updated for all three shapes; `tests/test_rds_store_contract.py`
  extends to residual reconstruction, address join, and normalized admin config.
- The shared canonicalizer + the RDS golden-diff harness (zero mismatches on equal
  data; pinpoints `venue_id`/field on a perturbed row).
- Redis shadow-projection diff, incl. the live-busyness exemption
  (`tests/test_redis_projection.py`).
- Eligibility assembly equivalence vs `EligibilityConfig.from_dict`
  (`tests/test_venue_eligibility.py` / `tests/test_admin_config.py`).
- Projector + repository reconstruction parity (`tests/test_rds_repository.py`).

Manual or integration checks:
- Pre-execution: `pg_dump` → local download → restore into throwaway Postgres →
  row-count match (the rollback gate).
- Per step: run the RDS golden diff + Redis shadow-projection diff over the full
  dataset and confirm zero mismatches before cutover.
- Post-provisioning smoke test of the real SQL per step (no local Postgres in CI).

## Acceptance Criteria
- A verified local database dump exists and restores with matching row counts
  before any migration runs; rollback-from-dump (accepting stale live busyness)
  is documented and the team has confirmed it.
- Execution order is Ex1 (payload duplication) → Ex3 (address table) → Ex2 (admin
  config); Ex1 leads so reconstruction is column-based before Ex3 swaps the
  address source (no payload overlay).
- Each step ships as expand/backfill/verify/cutover/contract with no single
  destructive ALTER, and every cutover is gated on a green full-dataset
  equivalence result: the v2-vs-v1 RDS golden diff (zero mismatches, matching
  counts + checksum) AND a Redis shadow-projection match against the pre-change
  snapshot (live busyness exempt). The retained v1 shape makes first-line rollback
  a read-flip.
- Serving output (venue API responses) and Redis geo-index membership/coordinates
  are byte-identical before and after each step.
- `venues.venue` no longer stores any scalar inside its JSON (residual holds only
  nested fields) nor address columns (→ `venues.address`).
- An operator can add/remove a single eligibility rule with a one-row change, and
  the effective config equals the pre-normalization config; cs-server readers use
  the normalized representation; the Redis mirror remains for vibes_bot.
- `tests/rds_fake.py` and the offline suite encode the new contracts and pass.

## Open Questions
- None blocking cs-server execution. Cross-repo dependency (not a blocker for this
  branch): the vibes_bot migration off the Redis eligibility mirror — including
  whether vibes_bot will read RDS directly or a new Redis shape — is decided in a
  separate vibes_bot plan; until then the mirror is retained by this branch.
