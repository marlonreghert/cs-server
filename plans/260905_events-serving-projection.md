# Events Serving Projection

## Branch
feature/events-serving-projection

## Goal
Make extracted events servable to the app, end to end from this repo's side:
carry every field the Events UI blueprint's card and detail screens read into a
new Redis serving projection that cs-server is the sole writer of, with a
flyer image the app is actually allowed to display, a neighbourhood for the
card's location line, and recurring announcements materialised into concrete
per-day occurrences so a day-grouped list is possible at all.

The consuming API (`GET /events`, `GET /events/{occurrence_id}`) is
vibes_bot's half of this work and is planned separately — see
`../../plans/260905_events-serving-and-app-api.md` in the wrapper. Nothing in
this plan serves an app request; it only produces the projection that half
reads.

## Non-goals
- **Any app-facing HTTP route.** cs-server does not serve the app. The route
  contract belongs to vibes_bot.
- **Genre facets, event favorites, price-text classification into an enum.**
  Explicitly descoped by the operator for this round (the blueprint marks the
  last as conditional on a price filter being prioritised). The projection
  payload still carries raw `category` and `price_text`, so all three are
  additive later without a projection migration.
- **Per-event vibe classification** (new LLM extraction + column). The
  blueprint already parks this pending post-launch demand evidence.
- **Promoter-sourced events.** `admin_config:hide_promoter_events` defaults to
  True and stays True; the projection honours the same admin flag the admin
  console does, so flipping it changes both surfaces at once.
- **An `events.event_occurrence` RDS table.** Occurrences are materialised in
  the projection, not in the system of record — see Implementation Approach.
- **Changing extraction, reconciliation, merge, or the review queue.** This
  plan reads what those already produce; it does not alter what they write.

## Evidence
- `app/models/promoter_event_visibility.py:9` — "`events.post_item` never
  reaches the Redis serving projection, so this is admin-surface only —
  nothing here can affect vibes_bot's app API or mobile." The serving path
  does not exist today; this plan creates it.
- `migrations/versions/0023_event_table.py:36` + `0024`/`0027`/`0028`/`0034`—
  `0039` — `events.post_item` already holds `title`, `description`, `lineup`,
  `attractions`, `ticket_info`, `ticket_url`, `price_text`, `category`,
  `post_type`, `starts_at`, `time_known`, `is_recurring`, `recurrence_text`,
  `status`, `venue_id`. The blueprint's card and detail screens need no new
  extraction column.
- `app/dao/rds_venue_store.py:557` (`_EVENT_SELECT`) and `:935`
  (`list_events`) — the **selection filters** (`venue_id`/`status`/`since`/
  `until`, ordered `starts_at NULLS LAST`) already exist for the admin
  console, and the projection's predicate is a sibling of those. The
  similarity stops at the filters: `_EVENT_SELECT` joins `venues.venue` for
  `v.venue_name` ONLY, and `venue_lat`/`venue_lng` were physically dropped
  from `venues.venue` by
  `migrations/versions/0007_drop_legacy_venue_columns.py:35-36`. Coordinates
  and the new `neighborhood` live solely on `venues.address`, which only
  `_VENUE_SELECT` joins (`rds_venue_store.py:67-73`). **An
  events ⋈ venues.address join has never existed in this codebase** — see
  Phase 4 §4 for the mechanism; do not assume extending `_EVENT_SELECT` can
  produce these fields.
- `app/routers/admin_events_router.py:603` (`GET /{event_id}/cover`) — the
  ONLY way a flyer is readable today is an admin-authenticated presign of a
  **data-lake** object.
- `app/dao/venue_media_store.py:1-33` — the app-servable bucket, its
  CloudFront/OAC reach, the content-addressed key rule and the
  `public, max-age=31536000, immutable` cache header that rule makes safe.
  `PROFILE_PHOTO_ROOT = "venue-profile-photos"` is the only prefix the media
  IAM policy grants `PutObject` on (`infra/media/main.tf`), so a new prefix is
  a terraform apply BEFORE any code that writes it.
- `infra/datalake/iam.tf:41-51` (Sid `ReadArchivedMedia`) — cs-server's
  production role **can** read `retrieved/*`, the prefix `cover_photo_key`
  lives under. This is a deliberate carve-out (menu extraction already
  presigns archived photos), and `app/routers/admin_events_router.py:602`
  exercises it in production today. **No new data-lake IAM grant is needed
  for the flyer copy.** Note that `docs/venue-retrieval-storage.md` §5/§8 and
  `app/dao/venue_media_store.py:6-8` both say flatly that the writer role is
  "denied `s3:GetObject`" — true for `raw/*` and the superseded `media/*`
  root, but NOT for `retrieved/*`, and reading it as absolute would wrongly
  condemn this whole design. The lake still blocks public access, so the flyer
  must be *copied* to the media bucket to be servable; it just does not need a
  new read permission to do so.
- `migrations/versions/0004_address_table.py:30` — `venues.address.
  neighborhood` exists. `rds_venue_store.py:192` — "Structured components
  (street/neighborhood/city/postal_code) are left as-is — null until Google
  Places enrichment fills them". A repo-wide grep finds **no writer at all**:
  the column has been null since it was created.
- `app/api/google_places_client.py:31-79` (`VIBE_FIELDS_MASK`) — the mask
  omits `addressComponents`, but already requests `reviews`, `priceLevel`,
  `priceRange`, `generativeSummary` and the whole atmosphere block
  (`liveMusic`, `allowsDogs`, `goodForGroups`, every `servesX`…), so these
  calls are **already billed at the top Enterprise + Atmosphere tier** — not,
  as an earlier draft of this plan claimed, at the cheap Essentials tier.
  Adding `addressComponents` is free because a call already billed at the
  ceiling cannot be pushed higher by one more field, NOT because the call is
  cheap. The conclusion survives; the reasoning that justified it did not.
  **Confirm against Google's current SKU table before merging** — nothing on
  disk independently pins `addressComponents`' own tier, and this repo has a
  hard rule against an unapproved >$10/mo increase.
- Production venue addresses carry the bairro inconsistently —
  `"R. Abdon Batista, 300 - Santo Amaro Recife - PE 50100-460 Brazil"` has it,
  `"R. Barão Rodrigues Mendes 59 - Recife PE 50030-180 Brazil"` does not — so
  parsing `venue_address` cannot be the source of truth.
- `app/services/event_date_resolver.py:360-405` — a recurring announcement
  resolves to ONE `starts_at`. `_weekdays_from_cadence_forms` /
  `_weekdays_from_recurrence_forms` already compute the weekday set behind
  that single answer; this plan reuses that computation rather than
  re-parsing recurrence prose.
- `app/services/redis_projection_service.py:70-93` (`_REBUILD_MODELS`) and
  `:108` (`rebuild_redis_from_rds`) — the projector is venue-keyed
  throughout: bulk-prefetch per family, then a per-venue loop. Events are not
  venue-keyed (an occurrence is the unit) and get a sibling pass, not an
  entry in that map.
- `app/services/venue_profile_photo_service.py:816-860` — the working
  precedent for "fetch bytes, content-hash, put to the media bucket, persist
  the CDN url on an RDS row, project the row".
- `plans/events_ui/mocks/events-ui-blueprint.html` (wrapper) — the surface
  this exists for. Its "Backend pendente" section names the projection, the
  date-range read and the recurrence expansion; the flyer-media and
  neighbourhood gaps below are NOT in that list and were found by reading the
  code. Two further needs come from reading the mockups themselves rather than
  its checklist: card option C's second shelf is per-venue ("Casa Bacurau esta
  semana"), which is why `events_venue_v1` exists; and card option A's last
  card is a "foto real do local" fallback for an event with no flyer, which is
  served by vibes_bot from the venue photo families it already reads and needs
  nothing from this repo.

## Current Behavior
- Extracted events live in `events.post_item` and are reachable only through
  the admin-authenticated `/api/events*` routes.
- No Redis key holds an event. `RedisVenueDAO` has no event key format and
  `_REBUILD_MODELS` has no event family.
- An event's flyer exists only as `cover_photo_key`, a data-lake object that
  is presigned per request for the admin console and can never be served to
  an end user.
- `venues.address.neighborhood` is null for every venue.
- A recurring announcement occupies exactly one day: its single computed next
  `starts_at`. A weekly forró night appears once and then disappears from any
  date-range read until it is re-crawled.

## Desired Behavior
- Every serving-eligible event occurrence within a rolling horizon is present
  in Redis, re-asserted from RDS each projection cycle, and disappears from
  Redis when it stops qualifying (rejected, superseded, unlinked, past, or the
  promoter-visibility flag hides it).
- Each projected occurrence carries the complete card + detail payload named
  in the Cross-Repo Contract below, so vibes_bot serves a list or a detail
  screen from Redis alone, with no RDS read and no S3 call on the request
  path.
- Every projected occurrence is reachable both by city window and by venue, so
  a per-venue shelf is one indexed read rather than a client-side filter over
  a whole city.
- A serving-eligible event with an archived flyer carries a stable, immutable,
  CDN-hosted `flyer_url` the app can load and cache directly. An event with no
  usable flyer carries `flyer_url: null` and is still projected — the card
  degrades, the event does not vanish.
- A venue whose Google Places enrichment has run carries its bairro on
  `venues.address.neighborhood`, and every occurrence at that venue carries it
  as `venue_neighborhood`. A venue not yet enriched carries null and the card
  drops the bairro from its location line.
- A recurring announcement is materialised into one occurrence per matching
  local day within the horizon, each with its own occurrence id and its own
  `starts_at`, so a day-grouped list shows a weekly night on every one of its
  nights.

## Implementation Approach

### Phase 1 — `event-flyers/` media prefix (terraform first, then code)
1. `infra/media/main.tf`: add `event-flyers/*` to the writer policy's resource
   list. Its own state key already isolates this stack from `infra/datalake`;
   this widens only the media policy. **Apply and verify before any code that
   writes the prefix ships enabled** — the same ordering rule
   `instagram_profile_photo_enabled` follows, and for the same reason: a write
   outside the policy fails only after the image bytes have been fetched.
2. `app/dao/venue_media_store.py`: add `EVENT_FLYER_ROOT = "event-flyers"`,
   an `event_flyer_key(post_item_id, content_hash)` helper and a
   `put_event_flyer` mirroring `put_profile_photo` exactly — content-addressed
   `<root>/<post_item_id>/<sha256[:16]>.jpg`, same immutable cache header, same
   `cdn_url`. Reuse the class; the compliance boundary this store exists to
   hold is data-lake-vs-media-bucket, and both prefixes are on the servable
   side of it.
3. A new `app/services/event_flyer_service.py` copies the flyer for events
   that qualify for serving and lack a current `flyer_url`: read the primary
   source's `cover_photo_key`, `MediaArchiveStore.get` the bytes, hash, put to
   the media bucket, persist. **The bytes move S3→S3 through cs-server, never
   through Instagram again** — the object is already paid for and archived, so
   this spends no Apify or vendor quota.
4. Persistence: `events.post_item.flyer_url`, `flyer_s3_key`,
   `flyer_content_hash`, `flyer_copied_at` (migration `0044`). Nullable, no
   back-fill — the copier fills them. Storing the hash is what makes the copy
   idempotent across cycles (unchanged bytes → identical key → no re-upload),
   the same property `_has_current_photo` relies on.
5. **Scheduling — note the phase dependency.** There is no existing shared
   "event tick" to attach this to: `main.py:184-356` registers only
   fixed-interval venue/enrichment jobs (none event-related), and event
   extraction runs inline inside the per-venue crawl jobs `CrawlScheduleSync`
   registers dynamically (`main.py:451-482`). The flyer copier therefore runs
   **inside the `project_events()` sibling pass Phase 4 creates**, on the
   existing `redis_projection` tick — so Phase 4's hook must land before this
   step can be wired. Build Phase 1's store, service and migration first, but
   wire the schedule with Phase 4. Bound it with a per-cycle cap so a first run
   over the backlog cannot monopolise a cycle.

### Phase 2 — neighbourhood from Google Places
1. Add `addressComponents` to `VIBE_FIELDS_MASK`. No new SKU tier (Evidence).
2. In `GooglePlacesEnrichmentService`, map the components to
   `venues.address.{street,neighborhood,city,postal_code}` and write them.
   Recife bairros arrive as `sublocality_level_1` (falling back to
   `sublocality`, then `administrative_area_level_2` for municipalities that
   publish no sublocality); `city` from `administrative_area_level_2`, or
   `locality` where present.
3. **Opportunistic only — no forced re-enrichment pass.** Coverage grows as
   venues come through the existing refresh cycle, and the incremental Google
   spend for this feature is therefore zero. A one-off backfill over the whole
   catalog would re-buy Place Details for every venue and is deliberately not
   part of this plan; if the operator wants faster coverage it is a separate,
   costed decision.
4. Never clobber a non-null stored component with a null response — absent
   components are "not answered", not "no bairro".

### Phase 3 — occurrence expansion
1. `app/services/event_occurrences.py` — pure, no I/O. Given one post_item row
   and a horizon, return the occurrences it contributes.
   - Non-recurring, `starts_at` present: exactly one occurrence, id
     `<post_item_id>`, `starts_at` unchanged.
   - Recurring, with a resolvable weekday set: one occurrence per matching
     local day in `[today, today + horizon]`, id `<post_item_id>_<YYYY-MM-DD>`
     (see the separator rule in the Data section — it must never be `#`),
     each `starts_at` = that local date at the announcement's own clock time
     (the resolved `starts_at`'s time-of-day, in Recife local time, converted
     back to UTC — so a DST-free zone stays exact and the stored instant is
     always UTC).
   - Recurring with NO computable day ("toda semana", "sempre" — the state
     `_is_no_computable_day_recurrence` already names): a single occurrence at
     the resolved `starts_at`, exactly as today. Inventing days for prose this
     repo has deliberately refused to parse would be a fabrication.
   - `starts_at` null: no occurrences. It cannot be placed on any day, so it
     cannot be served in a date-grouped list.
   - The weekday set comes from the resolver's OWN `_weekdays_from_*` helpers,
     exported for reuse. Re-parsing recurrence prose in a second place is
     exactly the duplication `app/models/event_kind.py`'s docstring warns
     about.
2. Horizon is a setting, `events_projection_horizon_days`, default 21 — long
   enough for the blueprint's "Escolher Data" picker to have content, short
   enough to bound the index.

### Phase 4 — the projection (cs-server is the sole writer)
1. `RedisVenueDAO` gains the event key family (formats in Data section) and
   `set_event_occurrence` / `delete_event_occurrence` / index writers,
   following the existing `*_KEY_FORMAT` + setter/deleter convention. Both
   indexes — city and venue — are written and pruned by the same code path, so
   one can never outlive the other.
2. `RedisProjectionService` gains `project_events()`, called from the same
   cycle as `rebuild_redis_from_rds` but as a **sibling pass**, not a
   `_REBUILD_MODELS` entry: the map's contract is one venue-keyed record per
   venue, and the unit here is an occurrence.
3. Selection (one bulk query, a sibling of `list_events`):
   `post_type = 'event'` AND `status IN ('accepted','confirmed')` AND
   `venue_id IS NOT NULL` AND the venue is in the servable set AND
   `starts_at >= now() - 1 day` (a night that started yesterday evening is
   still tonight's event to a user at 01:00) AND `superseded_by IS NULL`.
   Promoter-sourced items are excluded while
   `admin_config:hide_promoter_events` is true, reusing
   `promoter_event_visibility.is_promoter_only_item`'s unanimity rule rather
   than a second definition — but **not** its existing call pattern. That rule
   needs each item's `post_item_source` rows, and its only current caller
   (`admin_events_router._visible_rows`, lines 294-310) fetches them with
   `dao.list_event_sources(event_id)` inside a per-row loop: an N+1 that is
   fine for one admin page load and not fine once per projection cycle over
   the whole catalog. Fetch the sources for the selected event-id set in ONE
   grouped query (`list_all_event_sources` already reads the table in a single
   query and can be filtered/grouped), then apply the unanimity rule in
   Python. "One bulk query" must be true, not aspirational.

   A RECURRING row is exempted from the `starts_at >= now() - 1 day` rule
   above — its own stored `starts_at` goes stale between crawls, and without
   the exemption every weekly announcement would vanish the moment its
   single stored date fell into the past (Phase 3 re-derives its actual
   occurrence dates from today forward, from the weekday pattern, never from
   this stored value). That exemption is unbounded on its own, though: a
   "toda quinta" post whose venue stopped running that night a year ago, and
   that has not been re-crawled since, would otherwise generate a phantom
   Thursday occurrence every week, indefinitely. It is bounded instead by
   SOURCE FRESHNESS: a recurring row stops being selected once none of its
   sources has been seen within `events_recurring_max_source_age_days`
   (default 45) — `agg.last_seen_at`, the MAX `last_seen_at` across every
   attached `post_item_source` row, which `_EVENT_SELECT`'s own `agg` LEFT
   JOIN LATERAL already selects, so this needs no new join and no migration.
   A row whose `agg.last_seen_at` is NULL (no source row at all — should not
   occur in practice, every event is created FROM a source, but never
   assumed) is treated as maximally stale, not as a free pass — SQL's own
   NULL-propagation excludes it with no extra guard needed, matching
   `app.models.menu_lifecycle.is_menu_item_current`'s identical posture for
   the same shape of question. This bound applies ONLY to recurring rows; a
   non-recurring row's behaviour is exactly the `starts_at >= now() - 1 day`
   rule above, unchanged. `app/services/event_projection_selection.py`'s own
   `is_selectable` and `RdsVenueStore.list_events_for_projection`'s SQL
   express this identically, hand-kept in lockstep exactly as the rest of
   this predicate already is.
4. **Venue join — a genuinely new access pattern.** `_EVENT_SELECT` cannot
   supply `venue_lat`/`venue_lng`/`venue_neighborhood`: those columns were
   dropped from `venues.venue` (migration 0007) and live only on
   `venues.address`, which no events query has ever joined. Bulk-fetch one
   address row per selected `venue_id` with a query modelled on
   `_VENUE_SELECT`'s existing `LEFT JOIN venues.address a` — adding
   `a.neighborhood` to its column list — and join it to the event rows in
   Python by `venue_id`. Do NOT extend `_EVENT_SELECT`'s join to
   `venues.venue`: that yields null coordinates and a permanently null bairro,
   which is exactly the feature this plan exists to add.

5. **`city_slug` — derive it from the circles that already gate servability.**
   `city_slug` partitions the only index the app's primary list route reads,
   so it cannot be left to chance, and there is no per-venue city column to
   read: `venues.address.city` is null catalog-wide and Phase 2 fills it only
   opportunistically. Derive it instead from `admin.geo_fence_city`
   (`migrations/versions/0015_geofence_city_circles.py:63-71`) — a table
   cs-server already owns, whose `slug` is its primary key, and whose circles
   already decide whether a venue is servable at all via
   `serving.eligible_venue`. Every servable venue sits inside some circle **by
   construction**, so the derivation is total over exactly the population being
   projected: take the containing circle's slug, and on overlap pick the
   nearest centre so the result is deterministic.

   **Cross-repo invariant:** the slugs in `admin.geo_fence_city` ARE the
   serving city vocabulary. vibes_bot canonicalises an incoming `city` through
   its own `canonical_city_slug` (lowercase, `_`→`-`, then an alias table:
   `sao-paulo`→`sp`, `joao-pessoa`→`jp`), so a geo-fence slug that is not
   already canonical would key an index nothing ever reads. Add a validation to
   the admin geo-fence write path (`rds_venue_store.replace_geo_fence_cities`,
   `:1728`) rejecting a non-canonical slug, and record the invariant in the
   coordination plan. Copying the alias table into this repo is NOT the fix —
   that is exactly the two-definitions drift `app/models/event_kind.py`'s
   docstring warns about.

6. Expansion, then write: for each selected row, expand to occurrences, attach
   the joined venue fields and the derived `city_slug`, and write one JSON key
   per occurrence plus its two index memberships.
7. **Re-assert and prune — over an enumerable id space, never a keyspace walk.**
   The venue projection prunes by diffing two RDS-derived id sets
   (`redis_projection_service.py:256-266`: `rds_known` minus `servable_set`),
   which works because every venue id RDS can produce is enumerable. No such
   property holds for Redis KEYS, and the naive reading of "prune the stale
   ones" — revisit only the indexes appearing in this cycle's fresh selection —
   orphans a venue's ZSET and its payload keys **forever** the moment that
   venue drops to zero occurrences, which is the
   `Remove an occurrence once its event stops qualifying` scenario in
   miniature. Reaching instead for `_scan_venue_ids`' `client.keys(pattern)`
   (`redis_venue_dao.py:132-140` — a blocking full-keyspace walk, despite its
   misleading "SCAN via client.keys" comment) would walk production Redis every
   cycle. Do neither: iterate the **enumerable id spaces** — `rds_known`,
   already computed earlier in the same cycle, for every
   `events_venue_v1:<venue_id>`, and the small bounded `admin.geo_fence_city`
   slug set for every `events_index_v1:<city_slug>` — re-asserting each against
   this cycle's occurrence subset for that id. A venue or city that drops to
   zero gets visited and emptied like any other. No KEYS, no SCAN, no new
   registry. This is what makes a rejected, superseded, re-dated or expired
   event actually disappear, and what makes a Redis flush self-heal.
   A failed selection query aborts the cycle and leaves Redis intact — the
   same fail-safe posture `rebuild_redis_from_rds` takes on a serving-view
   read failure, and for the same reason: an empty read must never be
   mistaken for "there are no events".
8. Sizing: production Redis runs `maxmemory 0` / `noeviction` on a 3.9 GB box
   currently holding ~20 MB, so the ceiling that matters is the box, not a
   policy. Budget the new family explicitly and assert the measured per-cycle
   key count and byte total in the run summary, so growth is visible before it
   is a problem.

## Data, Config, And API Impact
**Migration `0044_event_flyer_media.py`** — `events.post_item` gains
`flyer_url text`, `flyer_s3_key text`, `flyer_content_hash text`,
`flyer_copied_at timestamptz`, all nullable, no back-fill. Downgrade drops the
four columns; no row is merged or deleted, so no destructive-refusal guard is
needed.

**`venues.address`** — no DDL. `street`/`neighborhood`/`city`/`postal_code`
finally get a writer.

**New Redis keys (cs-server is the sole writer):**
- `event_occurrence_v1:<occurrence_id>` → the occurrence JSON payload.
  `occurrence_id` is `<post_item_id>` for a one-off and
  `<post_item_id>_<YYYY-MM-DD>` for one materialised day of a recurring
  announcement.

  **The separator is `_` and must never be `#`.** This id is not internal: it
  is returned on every `EventCard` and is the path segment of vibes_bot's
  `GET /events/{occurrence_id}`. `#` is the RFC 3986 fragment delimiter, so
  any spec-compliant client (WHATWG `fetch`/`URL`, `NSURLSession`, OkHttp,
  `requests`, `httpx`) that builds that URL by concatenation silently drops
  everything from the `#` onward and requests the bare `<post_item_id>` —
  which, per Phase 3, a recurring event is NEVER projected under. That is a
  404 on every night of every recurring event, i.e. on the flagship feature,
  and it would ship green because no scenario builds a real URL.
  `post_item_id` is 32 lowercase hex characters
  (`app/services/event_identity.py:65`), so `_` can collide with neither the
  id nor the date, and the whole id stays in RFC 3986's unreserved set —
  no encoding needed anywhere. The internal `<venue_id>#<day_int>` convention
  (`app/dao/venue_repository.py:103`) is not a precedent: those ids never
  leave Redis.
- `events_index_v1:<city_slug>` → ZSET, member = occurrence id, score =
  `starts_at` epoch seconds (UTC). One key per city. A date range is one
  `ZRANGEBYSCORE`; day grouping is derived by the reader from each payload's
  own `occurrence_date`, so no key multiplies per day.
- `events_venue_v1:<venue_id>` → ZSET, same members and same scores, scoped to
  one venue. This exists for the blueprint's **card option C**, whose second
  shelf is "Casa Bacurau esta semana" — a per-venue rail. Without it that
  shelf can only be built by pulling the whole city window and filtering
  client-side, which is exactly how the venue list already lost its backend
  ordering guarantees. It is a second index over occurrences that are being
  written anyway: no extra payload, and both indexes are pruned in the same
  pass.

No existing key format changes; nothing already projected is touched.

**Occurrence payload (the cross-repo contract — vibes_bot reads exactly this):**
`occurrence_id`, `event_id`, `occurrence_date` (local `YYYY-MM-DD`),
`starts_at` (UTC ISO-8601), `ends_at`, `time_known`, `is_recurring`,
`recurrence_text`, `title`, `description`, `category`, `price_text`,
`ticket_info`, `ticket_url`, `lineup`, `attractions`
(`[{name,type,stage,styles}]`), `flyer_url`, `venue_id`, `venue_name`,
`venue_neighborhood`, `venue_lat`, `venue_lng`, `source_permalink`,
`source_handle`, `city_slug`, `status`, `updated_at`.

`price_text` and `category` travel as the raw extracted strings. This repo does
not classify them into enums here — the blueprint descoped that, and
`plans/260806_filter-label-casing.md` is on record that exact-matching an
LLM-produced value against a configured label zeroes the result.

`price_text` and `ticket_info` are both carried and are NOT interchangeable,
even though the blueprint renders the same sentence in both places ("Grátis
até 23:30" as the card's money chip and again under "Como Entrar" on the
detail screen). They are separate extracted columns that can disagree — one
card in the blueprint shows "R$100 em consumação" as its money chip — so both
travel and the client decides which to render where. Collapsing them here
would destroy a distinction this repo already stores.

**Settings:** `events_projection_enabled` (default false — ships dark, turned
on after the terraform apply is verified), `events_projection_horizon_days`
(21), `events_recurring_max_source_age_days` (45 — bounds a recurring row's
selection by source freshness so an abandoned recurring post does not
project forever; see Implementation Approach Phase 4 §3),
`event_flyer_copy_max_per_cycle`.

**Infra:** `infra/media/main.tf` writer policy widened to `event-flyers/*`
(a Resource-list addition to the existing `media_profile_photo_writer` policy;
its name/description stay untouched, since `aws_iam_policy.description` is
immutable and a rename forces a destroy-and-recreate). Apply before enabling
the flag. The bucket policy, OAC and CloudFront behaviour are all
bucket-wide/distribution-wide already and need no change.

**Retention — an explicit decision, not an oversight.** Events are inherently
time-bound (a weekly night gets a fresh post, and so a fresh `post_item_id`,
every week), and the media bucket's lifecycle config
(`infra/media/main.tf:96-122`) deliberately expires nothing — correct for
permanent venue profile photos, not obviously correct for `event-flyers/*`.
This plan **accepts unbounded growth for now** and makes it visible rather
than silent: emit `event_flyer_objects` and `event_flyer_bytes` gauges from
the copy path so the curve is observable from day one. Deleting a flyer when
its event stops qualifying is deliberately NOT done here — the object is
content-addressed and immutable, a superseded event can be un-rejected, and a
delete path against the media bucket is a new permission this feature does not
otherwise need. Revisit with a real number on the gauge.

## Error Handling And Observability
- A per-event failure is isolated and counted; the cycle continues and the run
  summary names the failing event ids, mirroring `rebuild_redis_from_rds`'s
  `error_venues`.
- A failed selection query aborts the events pass without deleting anything.
- A flyer copy failure (archive object gone, decode failure, S3 denied) leaves
  `flyer_url` null, is counted by outcome, and never blocks the occurrence from
  being projected.
- An `AccessDenied` on the flyer put is logged as a distinct, loud outcome —
  it means the terraform apply has not happened, and it must not read as an
  ordinary transient failure.
- Metrics (`app/metrics.py`): `events_projected_occurrences` (gauge),
  `events_projection_source_rows` (gauge), `events_projection_bytes` (gauge),
  `events_projection_errors_total`, `events_projection_duration_seconds`,
  `event_flyer_copy_total{outcome}` (`copied`, `unchanged`, `no_key`,
  `archive_missing`, `access_denied`, `failed`),
  `venue_address_components_total{outcome}`. Every `outcome` label is
  zero-filled at import so an ABSENT label proves the path never ran.
- No flyer URL, S3 key, presigned url or raw address payload is logged.

## Test Plan
Feature file: `tests/bdd/persistence/events-serving-projection.feature`
Second feature file: `tests/bdd/enrichment/venue-address-components.feature`

Two files, because `tests/README.md` assigns Google Places behavior to the
**enrichment** domain and Redis/DAO/migration behavior to **persistence**, and
this plan spans both. The Places address-component parsing, its fallback rungs
and the never-clobber rule are enrichment behavior and belong beside the
existing `tests/bdd/enrichment/venue-google-enrichment.feature`; everything
that merely *projects a stored value* stays in the persistence file. Splitting
along the repo's own classification line, rather than filing everything under
one convenient domain.

Scenarios:
- Project an accepted, venue-linked event and read back every contract field
  from `event_occurrence_v1:<id>`, with its index membership scored by
  `starts_at`.
- Exclude a pending_review, a rejected, a superseded and a `post_type != event`
  row from the projection.
- Exclude an event whose venue is not in the servable set.
- Exclude promoter-sourced events while `hide_promoter_events` is true, and
  include them when an admin flips it — without a deploy.
- Project an event whose `starts_at` was yesterday evening and is still tonight
  to a user at 01:00; drop one that is genuinely past.
- Expand a weekly recurring announcement into one occurrence per matching local
  day inside the horizon, each with its own id and its own `starts_at`, and
  none beyond the horizon.
- Stop projecting a recurring announcement once every one of its sources has
  gone stale beyond `events_recurring_max_source_age_days`; keep expanding one
  whose sources are still fresh.
- Project a "toda semana"/"sempre" recurrence as a single occurrence at its
  resolved `starts_at`, inventing no days.
- Project no occurrence at all for a row whose `starts_at` is null.
- Index an occurrence in both its city index and its venue index, with the
  same score in each.
- Read one venue's window from `events_venue_v1` without touching the city
  index.
- Prune: an occurrence projected last cycle whose event was then rejected is
  gone from BOTH indexes and its JSON key on the next cycle.
- Fail-safe: a failing selection query leaves the previous cycle's projection
  intact and reports an error.
- Carry `price_text` and `ticket_info` independently when the two disagree.
- Copy an archived flyer to the media bucket and project the CDN url; re-run
  the cycle and re-upload nothing (identical hash → identical key).
- Project an event with no archived cover with `flyer_url: null`, still
  indexed.
- An `AccessDenied` on the flyer put leaves `flyer_url` null, records the
  `access_denied` outcome, and still projects the occurrence.
- (persistence) Project the stored bairro as `venue_neighborhood`, and project
  null for an unenriched venue, without dropping the occurrence.
- (enrichment) Write `neighborhood` from a Places response carrying
  `sublocality_level_1`; fall back to `sublocality`; leave a stored value
  untouched when the response carries no component at all.
- Derive `city_slug` from the containing `admin.geo_fence_city` circle; pick
  the nearest centre when circles overlap; reject a non-canonical slug at the
  admin geo-fence write path.
- Prune a venue that had occurrences last cycle and has none this cycle: its
  venue index is emptied and its payload keys deleted, with no keyspace scan.
- Build a recurring occurrence's id and confirm it round-trips through a real
  URL — no `#`, nothing dropped, no encoding required.

Pytest unit tests:
- `app/services/event_occurrences.py`: the expansion matrix — one-off,
  weekly, daily, weekend, no-computable-day, null `starts_at`, horizon
  boundary (inclusive first day, exclusive past the horizon), clock time
  preserved across every generated day, and the occurrence-id format. Assert
  the id contains no RFC 3986 reserved character — exhaustively over the
  character set, not by enumerating a few examples, since "no `#` anywhere" is
  precisely the claim a handful of passing cases cannot establish.
- `city_slug` derivation: inside one circle, inside two overlapping circles
  (nearest centre wins), and the canonical-slug validation.
- `app/dao/venue_media_store.py`: `event_flyer_key` shape, idempotence of the
  hash slice, cache header.
- Address-component mapping: each fallback rung, and the never-clobber rule.
- Projection selection predicate, as a pure function over rows — including
  the recurring source-freshness bound: a stale-sourced recurring row
  excluded, a fresh-sourced one selected, the exact boundary day, a NULL
  `last_seen_at` (no source rows at all) excluded, and confirmation that a
  non-recurring row's own behaviour is unaffected by the bound.
- A dedicated parity test (`tests/test_events_selection_parity.py`) proving
  `list_events_for_projection`'s SQL and `is_selectable`'s Python agree on
  the same fixtures — the fake store always, the real store too once
  `RDS_TEST_URL` points at a migrated scratch Postgres — mirroring
  `test_eligibility_serving_view_parity.py`'s own shape for the analogous
  eligibility/serving-view pair.

Manual or integration checks:
- Real-Postgres migration check for `0044` against a throwaway `postgres:16`
  migrated through `0043`, matching `.github/workflows/tests.yml`'s
  scratch-Postgres step: `upgrade head`, confirm the four columns nullable,
  then `downgrade -1`.
- Redis integration run of a full events pass against a real redis:7 —
  assert key count, index cardinality and measured payload bytes, and record
  the measured total against the 20 MB current footprint.
- Terraform: `plan` on `infra/media` must show ONLY the writer-policy
  document changing, and must not propose any change to `infra/datalake`.
- After apply, verify one real flyer copy end-to-end and confirm the CDN url
  returns 200 with the immutable cache header.

## Acceptance Criteria
- `events.post_item` rows that qualify for serving are present in Redis as
  occurrences carrying every field in the contract, re-asserted each cycle.
- A rejected, superseded, re-dated, expired or newly-hidden event disappears
  from both indexes and its payload key within one cycle.
- Every occurrence is reachable by city window and by venue, with identical
  scores in both indexes.
- Every projected occurrence carries a non-null `city_slug` drawn from
  `admin.geo_fence_city`, and every slug in that table is canonical.
- Every projected occurrence carries its venue's coordinates, and its bairro
  whenever the venue has been enriched.
- A venue or city that drops to zero occurrences is emptied, and no projection
  cycle issues a `KEYS`/`SCAN` against production Redis.
- No occurrence id contains a URI-reserved character.
- A weekly recurring announcement occupies every one of its nights inside the
  horizon; a "toda semana" one occupies exactly one.
- A recurring announcement stops being projected once none of its sources has
  been seen within `events_recurring_max_source_age_days`; a non-recurring
  event's own temporal rule is unaffected by that bound.
- A copied flyer is reachable at a stable CDN url with the immutable cache
  header, and re-running the cycle uploads nothing.
- A venue enriched after this change carries its bairro on
  `venues.address.neighborhood` and in every occurrence at that venue.
- The events pass adds no Google, Apify, BestTime or OpenAI spend.
- Every new metric label is zero-filled, so an absent outcome proves a path
  never ran.
- `make test-unit` and `make test-bdd` pass; the feature file's `@wip` tag is
  removed.

## Open Questions
- None.
