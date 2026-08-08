# Per-user venue block list

## Branch
feature/blocked-venues

## Goal
cs-server (the engagement system of record) persists a per-user venue block:
blocking a venue records it durably in RDS, atomically removes any existing
favorite for that same (user, venue) pair in the same transaction, mirrors the
block to a Redis projection immediately (matching the favorites write-through
pattern), and exposes `POST /v1/blocks` / `DELETE /v1/blocks` mirroring the
favorites API shape. GDPR erasure (`DELETE /v1/user-data`) also purges block
rows.

## Non-goals
- vibes_bot exposing the block endpoints to the app, reading the new Redis
  key, or filtering the served feed by blocked venues — that is the vibes_bot
  slice of this feature, planned separately (per the wrapper's routing table:
  vibes_bot owns favorites/hot-likes API and serve-time enrichment).
- Mobile UI/UX — already mocked and approved; not this repo's concern.
- Restoring a favorite on unblock. Locked product decision: unblocking never
  restores the favorite that was removed when the venue was blocked.
- Any change to the unrelated admin-side venue-eligibility "block-list"
  (`app/services/venue_eligibility.py`, `admin.eligibility_rule`,
  `admin_config:venue_eligibility`). Different concept (Google/BestTime
  type + keyword exclusion, admin-tunable), different owner, not touched.
- Any change to `engagement.hot_like_event` or hot-likes behavior.

## Evidence
- `migrations/versions/0001_baseline_schemas.py:136-143` — real current DDL for
  `engagement.favorite`: `user_pseudo text NOT NULL`, `venue_id text NOT NULL
  REFERENCES venues.venue(venue_id)`, `created_at timestamptz NOT NULL DEFAULT
  now()`, `deleted_at timestamptz` (soft-delete = unfavorite), `updated_at
  timestamptz NOT NULL DEFAULT now()`, `PRIMARY KEY (user_pseudo, venue_id)`,
  plus `CREATE INDEX ix_favorite_venue ON engagement.favorite (venue_id)`. No
  migration since 0001 has altered this table. This confirms the prior-research
  summary's shape claim.
- `app/dao/rds_venue_store.py:1151-1164` — `upsert_favorite` (ON CONFLICT DO
  UPDATE SET deleted_at=NULL, updated_at=now()) and `soft_delete_favorite`
  (UPDATE SET deleted_at=now()), each its own single-statement `engine.begin()`
  transaction.
- `app/dao/rds_venue_store.py:1203-1224` — `purge_user_engagement` runs THREE
  deletes (`engagement.favorite`, `engagement.hot_like_event`,
  `engagement.app_session_day`) inside one `engine.begin()` block — the
  established pattern for one-transaction, multi-statement writes.
- `app/dao/rds_venue_store.py:1338-1360` — `set_geo_fence` is a second example
  of the same pattern (upsert + delete + inserts, one `engine.begin()`). This is
  the model for the new block+auto-unfavorite DAO method.
- `app/services/engagement_service.py:45-60` — `add_favorite`/`remove_favorite`:
  RDS write via `rds_store` first, then Redis projection
  (`self.redis.sadd`/`srem(self._fav_key(user_id), venue_id)`); key format is
  `user_favorites:{user_id}`, and the comment at lines 45-49 states this format
  MUST match vibes_bot's `favorites_dao.KEY_PREFIX`. Confirms the prior-research
  key-pattern claim exactly.
- `app/services/engagement_service.py:90-145` — `delete_user_data`: erasure
  cleans Redis projections BEFORE the RDS hard-delete (inverted from the normal
  write order) so a retry can converge; the new blocks projection must be
  cleaned in the same step alongside the favorites/hot-likes cleanup.
- `app/routers/engagement_router.py:1-9,49-69` — write-through docstring plus
  the real `POST`/`DELETE /v1/favorites` handlers: `EngagementRequest` DTO
  (`user_id`, `venue_id`, optional `ttl_seconds` used only by hot-likes),
  `_svc()` raises 503 if unconfigured, generic exceptions map to 502
  `"<action> failed; retry"`, success returns `{"status": "ok"}`.
- `app/services/venue_eligibility.py:1-22` — the ONLY existing "block" concept
  in the repo today is the unrelated admin-tunable eligibility block-list
  (Redis key `admin_config:venue_eligibility`, table
  `admin.eligibility_rule`). Confirms no prior block/hide/mute engagement
  concept exists, and flags a real naming-collision risk this plan must avoid.
- `tests/bdd/persistence/rds_system_of_record.feature:66-73` and
  `tests/bdd/persistence/redis_projection_decoupling.feature:75-79` — the
  existing favorites write-through BDD coverage lives under `persistence`, not
  `api`. The new blocks scenarios should follow the same domain placement since
  the behavior under test (RDS truth + immediate Redis projection, transaction
  boundary) is the same shape.
- `tests/bdd/persistence/account-deletion-engagement-purge.feature` — existing
  erasure BDD; needs a new assertion that blocked-venue rows are also purged.
- `tests/rds_fake.py:42,54,965-980,1010-1033` — `InMemoryRdsVenueStore` mirrors
  `upsert_favorite`/`soft_delete_favorite`/`purge_user_engagement`; used by both
  pytest and BDD steps, and needs matching block methods so BDD/pytest can run
  without a real Postgres.
- `migrations/versions/0028_event_ticket_info_and_attractions.py:54-55` — the
  current head is `0028_event_ticket_info_and_attractions` (revision name =
  file stem, `down_revision` chains to the prior file stem). The new migration
  is therefore `0029_blocked_venues`.
- `app/metrics.py:1080-1104` — `ENGAGEMENT_SESSION_TOTAL`,
  `ENGAGEMENT_USER_DELETION_TOTAL`, `ENGAGEMENT_HOT_LIKE_DEDUP_TOTAL` establish
  the engagement metrics conventions: `result` label for outcome counters, a
  plain counter for a specific suppressed/derived event, raw `user_id` never a
  label.

## Current Behavior
`engagement.favorite` is the only per-user venue engagement state with a
soft-delete lifecycle. `EngagementService.add_favorite`/`remove_favorite` write
RDS first then mirror to `user_favorites:{user_id}` in Redis. There is no way
for a user to hide a venue from their own feed; the only "block" concept in the
codebase is the unrelated, admin-tunable venue-eligibility exclusion list.

## Desired Behavior
1. A new `engagement.blocked_venue` table, identical in shape to
   `engagement.favorite` (PK `(user_pseudo, venue_id)`, FK to
   `venues.venue(venue_id)`, `created_at`/`updated_at`/`deleted_at`, index on
   `venue_id`), added in migration `0029_blocked_venues`
   (`down_revision = "0028_event_ticket_info_and_attractions"`).
2. Blocking a venue is one atomic RDS transaction that both upserts the block
   row (soft-delete-aware upsert, same `ON CONFLICT DO UPDATE SET
   deleted_at=NULL, updated_at=now()` shape as `upsert_favorite`) and, in the
   same `engine.begin()` block, soft-deletes any currently-active favorite for
   that `(user_pseudo, venue_id)` pair — so there is never a moment where a
   venue is both blocked and favorited, and no partial-apply state is possible
   on a mid-transaction failure.
3. Unblocking only soft-deletes the block row (`deleted_at=now()`); it never
   re-creates or restores a favorite (locked decision).
4. The Redis projection mirrors `engagement.blocked_venue` the same way
   favorites are mirrored: a new key `user_blocked_venues:{user_id}` (parallel
   to `user_favorites:{user_id}`), `sadd` on block / `srem` on unblock, RDS
   commit first then Redis projection second (same ordering `add_favorite`
   uses). Blocking additionally `srem`s the venue from
   `user_favorites:{user_id}` when the RDS transaction reports a favorite was
   actually removed, keeping the two projections consistent with the RDS
   transaction's outcome.
5. `purge_user_engagement` gains a fourth delete
   (`engagement.blocked_venue WHERE user_pseudo = :p`) inside its existing
   transaction, and its returned dict gains a `"blocked_venues"` count.
   `EngagementService.delete_user_data` gains a step (alongside the existing
   hot-likes/favorites projection cleanup, before the RDS purge) that `srem`s
   the user out of the blocked-venue projection key.
6. New endpoints, same DTO and error-handling conventions as favorites:
   - `POST /v1/blocks` — body `EngagementRequest {user_id, venue_id}`
     (`ttl_seconds` unused). Response `{"status": "ok", "favorite_removed":
     bool}` — `favorite_removed` tells the caller (vibes_bot, eventually the
     app) whether blocking this venue also cleared an existing favorite, so it
     can update its own UI state without a second round trip.
   - `DELETE /v1/blocks` — body `EngagementRequest {user_id, venue_id}`.
     Response `{"status": "ok"}`, matching `DELETE /v1/favorites` exactly.
   - Both follow the existing `_svc()` 503-if-unconfigured guard and the
     generic-exception → 502 "`<action> failed; retry`" mapping already used by
     every other engagement route.

Naming note (flagged per the task's instruction to avoid collision): the table
is named `engagement.blocked_venue` and the service/DAO methods use
`block_venue`/`unblock_venue` (not `block`/`block_list`), specifically to stay
unambiguous against the pre-existing, unrelated `admin.eligibility_rule` /
`venue_eligibility.py` "block-list" concept. The Redis key
`user_blocked_venues:{user_id}` similarly avoids the `admin_config:*` key
namespace the eligibility block-lists use.

## Implementation Approach
- **Migration** `migrations/versions/0029_blocked_venues.py`: `CREATE TABLE
  engagement.blocked_venue (...)` mirroring `engagement.favorite`'s DDL exactly
  (see sketch below), plus `CREATE INDEX ix_blocked_venue_venue ON
  engagement.blocked_venue (venue_id)`. Downgrade drops both.
- **DAO** (`app/dao/rds_venue_store.py`, new methods near the existing
  `# ── engagement ──` section):
  - `block_venue(user_pseudo, venue_id) -> bool`: one `engine.begin()` covering
    the upsert into `engagement.blocked_venue` and the conditional
    `UPDATE engagement.favorite SET deleted_at=now(), updated_at=now() WHERE
    user_pseudo=:u AND venue_id=:v AND deleted_at IS NULL`; returns
    `result.rowcount > 0` from that UPDATE as the "a favorite was removed"
    signal.
  - `soft_delete_block(user_pseudo, venue_id) -> None`: same shape as
    `soft_delete_favorite`.
  - `purge_user_engagement` extended with the fourth delete + count, described
    above.
  - `tests/rds_fake.py`'s `InMemoryRdsVenueStore` gets matching
    `block_venue`/`soft_delete_block` methods and a `blocked_venues` dict, plus
    the extra purge count, so BDD/pytest keep running without live Postgres.
- **Service** (`app/services/engagement_service.py`):
  - `_blocked_key(user_id)` returning `user_blocked_venues:{user_id}`.
  - `block_venue(user_id, venue_id) -> bool`: pseudonymize once, call
    `rds_store.block_venue(...)`, then `redis.sadd(self._blocked_key(...),
    venue_id)`, then conditionally `redis.srem(self._fav_key(...), venue_id)`
    only if the DAO reported a favorite was removed; returns that same bool to
    the router.
  - `unblock_venue(user_id, venue_id) -> None`: mirrors `remove_favorite`.
  - `delete_user_data` gains a blocked-venue projection cleanup step
    (enumerate not required — a plain `redis.delete(self._blocked_key(user_id))`
    is enough, since the projection is a single per-user set, unlike the
    venue-keyed hot-likes sets).
- **Router** (`app/routers/engagement_router.py`): `POST`/`DELETE /v1/blocks`
  added next to the favorites handlers, reusing the existing
  `EngagementRequest` model — no new DTO class needed since the shape is
  identical to favorites.
- **Metrics** (`app/metrics.py`): add
  `ENGAGEMENT_BLOCK_FAVORITE_CLEARED_TOTAL` (plain Counter, no labels, same
  style as `ENGAGEMENT_HOT_LIKE_DEDUP_TOTAL`) incremented in the router (or
  service) whenever `block_venue` returns `favorite_removed=True`, so the rate
  of block-clears-a-favorite is observable without inflating existing favorite
  metrics (none currently exist for favorites, so this is the first engagement
  counter tied to the block path specifically).

## Data, Config, And API Impact
- New table: `engagement.blocked_venue` (DDL sketch):
  ```sql
  CREATE TABLE engagement.blocked_venue (
    user_pseudo text NOT NULL,
    venue_id    text NOT NULL REFERENCES venues.venue(venue_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,                 -- un-block = soft-delete
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_pseudo, venue_id));
  CREATE INDEX ix_blocked_venue_venue ON engagement.blocked_venue (venue_id);
  ```
- New migration file: `migrations/versions/0029_blocked_venues.py`
  (`down_revision = "0028_event_ticket_info_and_attractions"`).
- New endpoints: `POST /v1/blocks`, `DELETE /v1/blocks` — request body
  `{user_id: str, venue_id: str}`; POST response adds `favorite_removed: bool`.
- New Redis key: `user_blocked_venues:{user_id}` (set of `venue_id`), analogous
  to `user_favorites:{user_id}`. This is a new key namespace, not a change to
  an existing one, so it does not affect Redis key compatibility for any
  existing reader.
- `purge_user_engagement`'s return dict gains a `"blocked_venues"` key
  (additive; `delete_user_data`'s response already forwards the whole dict, so
  `DELETE /v1/user-data`'s `removed` payload gains this field — additive, not
  a breaking change for the existing caller).

## Error Handling And Observability
- Router paths reuse the existing 503-if-unconfigured and 502-on-error
  conventions already used by every other `/v1/*` engagement route — no new
  error shapes.
- New Prometheus counter `ENGAGEMENT_BLOCK_FAVORITE_CLEARED_TOTAL` (see
  Implementation Approach) tracks how often blocking clears a favorite.
- Erasure (`DELETE /v1/user-data`) failure/log-redaction behavior is unchanged;
  the new purge count flows through the existing `logger.info(... removed=%s
  ...)` line in `delete_user_data`, which never logs the raw user id today and
  continues not to.

## Test Plan
Feature file: `tests/bdd/persistence/blocked-venues.feature`

Scenarios:
- Blocking a venue that is not currently favorited persists the block in RDS
  and projects it to Redis immediately, and reports no favorite was removed.
- Blocking a currently-favorited venue atomically removes the favorite (RDS no
  longer holds an active favorite, Redis favorites projection loses the
  member) and reports a favorite was removed.
- Unblocking a venue does not restore a favorite that was removed when it was
  blocked.
- Unblocking a venue that was never blocked succeeds and changes nothing.
- Blocking the same venue twice is idempotent (no duplicate row, block stays
  active).
- Erasing a user's engagement data (`DELETE /v1/user-data`) removes their
  blocked-venue rows and their `user_blocked_venues:{user_id}` projection, and
  leaves another user's blocks untouched.
- Blocking or unblocking with a missing `venue_id`/`user_id` is rejected at the
  request boundary (422).

Pytest unit tests:
- DAO-level (extends `tests/test_rds_store_contract.py`'s fake+real pattern):
  `block_venue` runs as a single transaction — a forced failure after the
  block-upsert leaves the favorite row untouched (no partial apply); the
  `favorite_removed` return value is correct in both the
  favorited-before-block and not-favorited-before-block cases.
  `purge_user_engagement` count includes `blocked_venues`.
- Service-level (extends `tests/test_engagement_user_deletion.py`'s `_Redis`/
  `_Store` fake-call-order pattern): `block_venue` calls RDS before Redis;
  `redis.srem` on the favorites key only fires when the DAO reports
  `favorite_removed=True`; `delete_user_data` clears the blocked-venue
  projection key.

Manual or integration checks: None.

## Acceptance Criteria
- `engagement.blocked_venue` exists with the DDL above, reachable via
  `alembic upgrade head` from `0028_event_ticket_info_and_attractions`.
- Blocking a venue that has an active favorite removes that favorite in the
  same DB transaction as the block insert; no intermediate state is
  observable where the venue is both blocked and favorited.
- Unblocking a venue never re-creates a favorite.
- `POST /v1/blocks` and `DELETE /v1/blocks` exist, follow the favorites
  endpoints' DTO and error-handling conventions, and `POST` reports whether a
  favorite was cleared.
- `user_blocked_venues:{user_id}` is updated in Redis immediately on
  block/unblock, matching the favorites projection's immediacy.
- `DELETE /v1/user-data` purges `engagement.blocked_venue` rows and the
  blocked-venue Redis projection for the erased user, and the response's
  `removed` payload reports the count.
- All new pytest and BDD scenarios above pass; existing favorites/erasure BDD
  scenarios remain green (extended, not broken).

## Open Questions
None.
