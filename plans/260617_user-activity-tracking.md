# User Activity Tracking (system-of-record for app usage counts)

## Branch
feature/user-activity-tracking

## Goal
Give the platform a system-of-record signal for "people who use the app" so the
admin dashboard can report real user/active-user counts instead of only
users-who-favorited. cs-server records each authenticated app session in RDS
(pseudonymized) via the existing engagement write-through, and exposes
total/active-window counts for the admin panel.

This is the **upstream half** of the admin-stats fix; vibes_bot routes the write
and reads the counts (see `vibes_bot/plans/260617_admin-stats-counts.md` and the
wrapper coordination plan `plans/260617_admin-stats-counts.md`).

## Non-goals
- No raw user identifiers in RDS. Activity is keyed by `user_pseudo =
  HMAC(user_id)`, exactly like `engagement.favorite` — raw id never stored.
- No Redis projection for activity. Favorites/hot-likes project to Redis because
  the **serve path** reads them; activity counts are read **only by the admin**,
  from RDS — so this rides only the *write* half of the engagement pattern, not
  the Redis-projection half.
- No per-request event log. One row per `(user_pseudo, activity_date)` is enough
  for total + DAU/WAU/MAU and is privacy-minimal.
- No mobile change. No change to favorites/hot-likes behavior.

## Evidence
- `app/services/engagement_service.py:27` — `pseudonymize(user_id)` =
  `hmac.new(key, user_id, sha256)`; `add_favorite` persists
  `rds_store.upsert_favorite(self.pseudonymize(user_id), venue_id)`. Reuse this
  for sessions.
- `app/routers/engagement_router.py:39-84` — `/v1/favorites` + `/v1/hot-likes`
  write-through contract (200 ok; 502 "…; retry" on persist failure). The new
  `POST /v1/sessions` mirrors it.
- `app/dao/rds_venue_store.py:339` — `upsert_favorite(user_pseudo, venue_id)`
  INSERT pattern to mirror for the session upsert.
- `migrations/versions/0001_baseline_schemas.py:134-150` — `engagement` schema
  (`favorite` keyed by `user_pseudo`; `hot_like_event` append-only). The new
  table joins this schema. Latest migration is `0011`; this adds `0012`.
- `app/routers/admin_trigger_router.py:513-580` — admin `GET
  /admin/venues/inventory` returning `counts{…}`; the new admin counts endpoint
  follows the same admin-router placement that vibes_bot already proxies.
- `.github/workflows/tests.yml:75` runs `alembic upgrade head` in **CI only** —
  deploy/startup does **not** migrate. The new table must be applied manually in
  prod (see Data/Config/API Impact).

## Current Behavior
There is no record of app usage / logins. Only `engagement.favorite` and
`engagement.hot_like_event` exist, populated when a user favorites/likes a venue.

## Desired Behavior
- `POST /v1/sessions` with `{ "user_id": "<firebase uid>" }` records that the
  user was active **today** (America/Recife date): upsert one row into
  `engagement.app_session_day(user_pseudo, activity_date)`, idempotent for repeat
  pings the same day. Returns `{"status":"ok"}`; returns 502 ("…; retry") if the
  RDS write fails.
- `GET /admin/users/activity-counts` returns
  `{ total_users, active_1d, active_7d, active_30d }`, each a distinct-user count
  over the trailing window (today, last 7 days, last 30 days inclusive of today).
- Counts reflect a recorded session within the same request cycle.
- "Active" means *made an authenticated app request that day* — the closest
  backend-observable proxy for a login; framed honestly in the response/docs.

## Implementation Approach
- **Migration `0012_engagement_app_session_day`** (raw SQL like the baseline):
  `CREATE TABLE engagement.app_session_day (user_pseudo text NOT NULL,
  activity_date date NOT NULL, PRIMARY KEY (user_pseudo, activity_date));` plus
  `CREATE INDEX ix_app_session_day_date ON engagement.app_session_day
  (activity_date);`. No timestamp columns — PK + `ON CONFLICT DO NOTHING` covers
  every count cheaply.
- **`rds_venue_store`**: `record_app_session(user_pseudo, activity_date)` →
  `INSERT … ON CONFLICT (user_pseudo, activity_date) DO NOTHING`;
  `count_users(since_date | None)` → `SELECT count(DISTINCT user_pseudo) FROM
  engagement.app_session_day [WHERE activity_date >= :since]` (None = total).
- **`EngagementService`**: `record_session(user_id)` →
  `rds_store.record_app_session(self.pseudonymize(user_id),
  recife_today())`; `activity_counts()` → builds the four windows from
  `count_users(...)`. No Redis call on this path.
- **`engagement_router`**: add `POST /v1/sessions` with a new
  `SessionRequest{user_id: str}` model (favorites' `EngagementRequest` requires
  `venue_id`, which sessions has none of). Same 200/502 convention.
- **`admin_trigger_router`**: add `GET /admin/users/activity-counts` returning
  the counts dict (admin-router placement so vibes_bot proxies it like
  inventory).
- **Timezone**: bucket `activity_date` in **America/Recife** (the repo's day
  convention) via the existing tz handling, so "today" matches operator/local
  expectations.

## Data, Config, And API Impact
- **RDS:** new table `engagement.app_session_day` (+ date index). Migration
  `0012`. **Deploy step (required):** deploy does not run alembic — apply
  manually before/with the vibes_bot release, e.g. SSM `docker exec
  vibes_bot-cs-server-1 alembic upgrade head`. Without it, `POST /v1/sessions`
  500s on the missing table.
- **API:** new `POST /v1/sessions` and `GET /admin/users/activity-counts`. No
  change to existing endpoints.
- **Config:** reuses the existing engagement pseudonymization key; no new config.
- **Redis / mobile:** none.

## Error Handling And Observability
- `POST /v1/sessions` returns 502 ("…; retry") on RDS failure, matching the
  engagement contract (vibes_bot treats the session ping as best-effort and does
  not surface errors to users).
- Add `ENGAGEMENT_SESSION_TOTAL{result=success|error}` counter
  (`app/metrics.py`); log failures with operation context, never the raw
  `user_id`.

## Test Plan
Feature file: `tests/bdd/api/user-activity-tracking.feature`

Scenarios:
- Recording a session makes the user appear in counts (POST then counts:
  total=1, active_1d=1).
- Recording the same user twice the same day counts once (idempotent upsert).
- Two distinct users today → total=2, active_1d=2.
- A user last active 10 days ago is in active_30d but not active_7d/active_1d.
- The persisted identifier is pseudonymized — the stored `user_pseudo` is not the
  raw `user_id`.
- `GET /admin/users/activity-counts` returns all four windows.
- `POST /v1/sessions` with a missing `user_id` returns 422.

Pytest unit tests:
- `rds_venue_store.record_app_session` is idempotent (ON CONFLICT) and
  `count_users(since)` computes distinct-user windows correctly, including
  empty/no-rows.
- `EngagementService.record_session` pseudonymizes before persisting and never
  touches Redis; `activity_counts()` shape and window math (Recife date
  boundaries).

Manual or integration checks:
- Apply migration `0012` on RDS; `POST /v1/sessions` twice for one uid + once for
  another, confirm `activity-counts` shows total=2, active_1d=2, and the table
  holds two `user_pseudo` rows for today.

## Acceptance Criteria
- `POST /v1/sessions` upserts one pseudonymized row per user per Recife day,
  idempotently, and returns 200 (502 on RDS failure).
- `GET /admin/users/activity-counts` returns correct total/1d/7d/30d
  distinct-user counts.
- No raw `user_id` is ever written to RDS or logs.
- Migration `0012` is documented as a manual prod apply-step.
- `make test-bdd` and targeted pytest pass.

## Open Questions
- None.
