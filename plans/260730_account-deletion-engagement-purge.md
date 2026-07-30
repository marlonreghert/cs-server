# Account Deletion — Purge A User's Engagement Data

## Branch
feature/account-deletion-engagement-purge

## Goal
Erase every trace of one user from the system of record and from the engagement
Redis projections cs-server owns, given that user's raw id. One idempotent
call that vibes_bot can make on the user's behalf, so the app can offer the
in-app account deletion Apple requires.

## Non-goals
- Deleting the Firebase Auth account. That is the app's job (`deleteUser()`);
  cs-server never holds Firebase credentials.
- vibes_bot's own Redis keys — specifically the cached auth token written by
  `POST /auth/cache`. vibes_bot owns that key and clears it itself.
- Venue data. Deleting a user must never touch `venues.venue`, enrichment, or
  the venue serving projection. A hot-like removal changes a venue's like
  *count*, which is the intended consequence, not venue deletion.
- Any change to how engagement is written. The write-through path
  (`upsert_favorite`, `add_hot_like_event`, `record_app_session`) is unchanged.
- Authentication on the endpoint. This is an internal server-to-server call on
  the private network, the same posture as the existing `/v1/favorites`,
  `/v1/hot-likes` and `/v1/sessions` endpoints, which also take `user_id` in the
  body and carry no auth. Adding auth to the engagement surface is separate work
  — noted as a risk below.

## Evidence
- Apple App Store rejection, Guideline 5.1.1(v), submission
  `db962393-8468-4fce-be13-8477283b6e96`, v1.1.9 (29), reviewed on iPad Air
  11-inch (M3): "The app supports account creation but does not include an
  option to initiate account deletion."
- `app/services/engagement_service.py` — the raw `user_id` is pseudonymized with
  HMAC before it touches RDS; the same service projects the Redis keys vibes_bot
  reads (`user_favorites:{user_id}`, `hot_likes:v1:{venue_id}`). **cs-server is
  the sole writer of both**, so deleting them belongs here, not in vibes_bot.
- `app/dao/rds_venue_store.py:571-607` — the three per-user write paths:
  `upsert_favorite`, `soft_delete_favorite`, `add_hot_like_event`,
  `record_app_session`. There is **no read-back or delete-all for a user**, so
  enumeration and purge are both new.
- `app/routers/engagement_router.py:17` — router prefix `/v1`; existing siblings
  `/favorites`, `/hot-likes`, `/sessions`. `SessionRequest` is the precedent for
  a user-only (no venue) request body.
- `hot_likes:v1:{venue_id}` is keyed by **venue**, with user ids as set members.
  A user's liked venues therefore cannot be discovered from Redis by key — they
  must be enumerated from `engagement.hot_like_event` rows. Missing this leaves a
  deleted user permanently inflating venue like-counts.
- Migration `0016_hot_like_event_idempotency` — `hot_like_event` has a unique
  index on (user_pseudo, venue_id, business_period), so one user may hold many
  rows per venue across days. Enumeration must be DISTINCT by venue.
- The HMAC pseudonym is **deterministic**, so rows are findable from the raw
  `user_id`. This is why the purge must run while the app still has the raw id —
  i.e. before the Firebase account is destroyed.

## Current Behavior
There is no way to delete a user. Favorites, hot-like events and app-session
rows accumulate in RDS keyed by `user_pseudo` forever, and the user's id stays a
member of every `hot_likes:v1:{venue_id}` set they ever liked. Nothing enumerates
or removes them.

## Desired Behavior
1. A single call must, for one raw `user_id`, remove: all `favorites` rows, all
   `hot_like_event` rows, and all `app_session_day` rows for its pseudonym; the
   `user_favorites:{user_id}` Redis key; and the user's membership in every
   `hot_likes:v1:{venue_id}` set they appear in.
2. The user's hot-liked venues must be enumerated from RDS (DISTINCT venue) so no
   membership is missed, and each venue's like count must drop accordingly.
3. The call must be **idempotent**: a second call for the same user, or a call
   for a user who never existed, must succeed and report zero removals — never
   404 and never error. The app may retry after a network failure.
4. The call must report what it removed, per store, so an operator (and the
   BDD suite) can prove erasure rather than assume it.
5. A partial failure must be **visible, not silent**: if RDS succeeds and the
   Redis cleanup fails, the call must fail so vibes_bot retries. Re-running must
   converge (the RDS rows are already gone; the Redis cleanup repeats safely).
6. No venue row, enrichment row, or venue serving projection entry may be
   touched.

## Implementation Approach
**Enumeration + purge in `RdsVenueStore`.** Add, alongside the existing
engagement writes:
- `list_user_hot_like_venue_ids(user_pseudo)` — DISTINCT venue_id from
  `engagement.hot_like_event`. This is the input to the Redis cleanup and must be
  read **before** the rows are deleted.
- `purge_user_engagement(user_pseudo)` — deletes the user's rows from
  `favorites`, `engagement.hot_like_event` and `engagement.app_session_day` in
  one transaction, returning a per-table count.

Favorites are currently *soft*-deleted (`soft_delete_favorite`). For an erasure
request a soft delete is not deletion — the row still carries the pseudonym — so
this path must **hard**-delete. That difference is deliberate and is what makes
this an erasure rather than a deactivation, which is exactly the distinction
Apple's guideline draws.

**Orchestration in `EngagementService.delete_user_data(user_id)`.** Ordered so a
crash at any point leaves the system converging rather than inconsistent:
1. Derive the pseudonym (same HMAC as the write path).
2. Read the hot-liked venue ids from RDS.
3. Purge the RDS rows.
4. Remove the user from each `hot_likes:v1:{venue_id}` set and delete
   `user_favorites:{user_id}`.

RDS before Redis matches the existing write-through ordering (RDS is truth; the
projection follows), and reading the venue list first is what makes step 4
possible after step 3 has removed its source.

**Endpoint.** `DELETE /v1/user-data` in `engagement_router`, body
`{"user_id": "..."}` (a user-only body, following `SessionRequest`), responding
with the per-store counts. Reuses the router's existing `_svc()` guard.

## Data, Config, And API Impact
- **API:** new `DELETE /v1/user-data`. Additive; no existing endpoint changes.
- **Migration:** none. No schema change — only deletes against existing tables.
- **Config:** none.
- **Redis:** no key-format change. Removes members/keys under the existing
  `user_favorites:` and `hot_likes:v1:` formats.
- **Persistence:** introduces the first *hard* delete on `favorites`. Note the
  existing soft-delete path stays for the ordinary un-favorite action; only
  erasure hard-deletes.

## Error Handling And Observability
- A missing/blank `user_id` must be rejected with 422 by the Pydantic model
  rather than purging nothing silently.
- A Redis failure after the RDS commit must return 5xx so vibes_bot retries,
  mirroring the documented write-through behavior in the router's own docstring.
- The endpoint must never raise on "nothing to delete" — that is a successful
  no-op, and it is the retry case.
- Metrics (`app/metrics.py`): `ENGAGEMENT_USER_DELETION_TOTAL` counter labelled
  by outcome (`ok` / `partial` / `error`), so a failing deletion path is visible
  rather than being buried in app-side retries. An erasure that silently stops
  working is a compliance problem, not just a bug.
- Logs must record the **pseudonym and the removal counts, never the raw
  `user_id`** — the raw id is the PII this system exists to keep out of storage,
  and a deletion log must not reintroduce it.

## Test Plan
Feature file: `tests/bdd/persistence/account-deletion-engagement-purge.feature`

Scenarios:
- A user with favorites, hot-likes and sessions is fully erased: all three RDS
  row sets are gone, the favorites Redis key is gone, and the user is no longer a
  member of any hot-likes set.
- A hot-liked venue's like count decreases by exactly one when the user is
  erased, and other users' memberships in that venue are untouched.
- A user who hot-liked the same venue on several days (multiple
  `hot_like_event` rows) is removed from that venue's set exactly once and all
  their rows are deleted.
- Deleting a user twice succeeds, and the second call reports zero removals.
- Deleting a user who never existed succeeds and reports zero removals.
- Another user's favorites, hot-likes and sessions are completely unaffected.
- No venue row, enrichment record, or venue serving-projection entry changes.
- The favorites rows are HARD-deleted, not soft-deleted — no row bearing the
  pseudonym survives.
- A blank or missing `user_id` is rejected and nothing is deleted.
- When the Redis cleanup fails after the RDS purge, the call reports failure and
  a retry converges to fully erased.

Pytest unit tests:
- `tests/test_engagement_user_deletion.py` — pseudonym derivation matches the
  write path; venue enumeration is DISTINCT and read before the purge; step
  ordering; idempotency; per-store counts; the raw `user_id` never appears in a
  log record.

Manual or integration checks:
- Run the migration-free purge against a scratch Postgres (the pattern used by
  `tests/test_eligibility_serving_view_parity.py` with `RDS_TEST_URL`) to confirm
  the DELETEs execute against the real schema — the fake store proves the rule,
  not the SQL.

## Acceptance Criteria
- One call erases a user's favorites, hot-like events and app-session rows from
  RDS, plus their favorites key and every hot-likes membership in Redis.
- Hot-liked venues are enumerated from RDS, so no membership is left behind.
- Repeat calls and unknown users both succeed with zero removals.
- Favorites are hard-deleted; no row carrying the pseudonym remains.
- Other users and all venue data are provably untouched.
- A post-RDS Redis failure surfaces as 5xx and a retry converges.
- No raw `user_id` appears in any log line.
- All new BDD scenarios and pytest tests pass; the existing suites stay green.

## Open Questions
- None blocking. **Recorded risk:** `DELETE /v1/user-data` takes `user_id` in the
  body with no authentication, matching the existing engagement endpoints — so
  anyone able to reach cs-server directly could erase an arbitrary user's data.
  That is the current posture of the whole engagement surface (see Non-goals) and
  cs-server is not internet-facing, but this endpoint is destructive where its
  siblings are not. Authenticating the engagement surface is worth its own plan.
