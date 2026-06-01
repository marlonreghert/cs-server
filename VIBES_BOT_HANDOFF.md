# vibes_bot changes for the RDS system-of-record

Do this **after** RDS is provisioned (`infra/rds/README.md`) and cs-server is
cut over (`rds_enabled=true`). cs-server is the sole DB owner — **vibes_bot never
connects to RDS directly**; it goes through cs-server's API. Plan:
`plans/rds_system_of_record_01_06_26.md`. Plan/execute these under vibes_bot's
own lifecycle (companion plan in `vibes_bot/plans/`).

## What does NOT change in vibes_bot
- Venue **serving/retrieval**: keep calling cs-server `GET /v1/venues/nearby`
  (`CrowdSenseClient`). Unchanged.
- Reading favorites/hot_likes: keep reading **Redis** (a few seconds of staleness
  after a write is acceptable). Unchanged.
- vibes_bot-only short-lived caches (weather, pricing, busyness predictions):
  stay in Redis, out of scope.

## What MUST change in vibes_bot

### 1. Favorites / hot_likes WRITES → cs-server engagement API
Today `app/daos/favorites_dao.py` / `hot_likes_dao.py` write Redis directly.
Switch the **write** paths to call cs-server (reads stay on Redis):
- Add a favorite:    `POST   {CS}/v1/favorites`   body `{"user_id","venue_id"}`
- Remove a favorite: `DELETE {CS}/v1/favorites`   body `{"user_id","venue_id"}`
- Add a hot-like:    `POST   {CS}/v1/hot-likes`    body `{"user_id","venue_id"}`
cs-server persists to RDS (pseudonymized) and projects the same Redis keys
vibes_bot reads (`user_favorites:{user_id}`, `hot_likes:{venue_id}`). On a 5xx,
**retry** (idempotent) so the user's read path stays consistent.
Do NOT also write Redis directly from vibes_bot for these anymore (cs-server owns
the projection).

### 2. Admin panel → cs-server admin API (replace direct Redis/RDS access)
Today `app/admin/routes.py` + `app/admin/config_dao.py` read/write
`admin_config:*` and venue keys **directly in Redis**. Repoint them at cs-server
admin endpoints so the panel becomes a true DB interface:
- Config CRUD → cs-server `GET/PUT/DELETE /admin/config/{key}` *(Phase 2 — these
  generic config endpoints + the config-in-RDS mirror are NOT yet implemented in
  cs-server; build them with this vibes_bot change. Today only
  `GET/POST /admin/venues/eligibility-config` exists.)*
- Venue inspection → cs-server `GET /admin/venues/inventory` (exists) and a
  venue-detail/edit endpoint *(Phase 2 — to be added in cs-server)*.
- Stop writing venue/config data directly to Redis from the admin panel.

> NOTE: items marked *(Phase 2)* require small additional cs-server work
> (generic config CRUD + venue-edit admin endpoints + config-in-RDS mirror).
> The cs-server BDD scenario "Admin configuration is stored in RDS and mirrored
> to Redis" is tagged `@wip` pending that. Sequence: finish those cs-server
> endpoints, then do this vibes_bot repoint.

## Config / env vibes_bot needs
- The cs-server base URL it already uses for `CrowdSenseClient` (engagement +
  admin calls go to the same cs-server).
- No DB credentials in vibes_bot (it must not connect to RDS).
