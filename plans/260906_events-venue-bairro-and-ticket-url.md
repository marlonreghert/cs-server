# Events venue bairro, ticket URL and nightlife-day projection — fix all three at the system of record

> **Revision 2 (2026-09-06)** — revised in response to the three-lens review of
> the Events UI v1 plan set (contract reconciliation, completeness, adversarial
> execution). Findings assigned to this repo: **F01, F02, F09, F10, F17, F18,
> F29, F30, F37**. Every one is dispositioned in
> [Review findings — disposition](#review-findings--disposition); F01 EXPANDS
> this plan's scope with a third defect (C3), and F02 hardens the city
> vocabulary vibes_bot is about to validate against.

## Branch
fix/events-venue-bairro-and-ticket-url

## Goal
Three defects visible on every served event card, all fixed in cs-server so the
2-minute projector re-asserts the corrected values without a Redis migration
and without either downstream repo changing a field:

- **C1 — `venue_neighborhood` carries a venue/complex name instead of a
  bairro** for 7 of 53 live occurrences (13.2%): Downtown Beer Garden serves
  `"Quintal Espinheiro"` where the real bairro is `"Espinheiro"`.
- **C2 — `ticket_url` is projected with no URL scheme.** The single production
  value is the bare string `"evenyx.com"`, which `Linking.openURL` rejects.
- **C3 (added by review finding F01) — the projection deletes the previous
  nightlife day's recurring occurrences at local midnight.** vibes_bot's B1
  widens the served window to `[nightlife-day 00:00, +1d 06:00)` precisely so a
  party that started at 22:00 is still listed at 00:30 — but for the 77.4% of
  live inventory that is recurring, that occurrence no longer exists in Redis
  by then, because this repo regenerates recurring occurrences from the current
  CALENDAR date forward and prunes everything else. B1 is inert without this
  change.

Fixes must leave the events projection's field set byte-for-byte identical —
only the VALUES change (C1, C2) and the SET of occurrence ids present in the
index changes (C3).

## Non-goals
- Adding, removing, renaming or retyping any field on `EventOccurrence`. The
  cross-repo contract in `plans/260905_events-serving-backend.md` is frozen.
- Every vibes_bot defect in the Events UI brief (B1–B9). `ticket_url`
  normalisation is ALSO landing in vibes_bot at serve time (B4) as deliberate
  belt-and-braces for released clients; that is not this plan's work and is not
  a reason to weaken this one.
- Event favorites, genre facets, price classification, per-event vibe
  classification. All descoped by the brief.
- **Changing the address text parser's heuristics.** Measured below: the parser
  is 394/395 clean over the whole 488-venue Recife-metro catalog. A heuristic
  that "fixes" the one bad row puts 204 legitimately multi-word bairros at
  risk. The parser stays exactly as shipped.
- Adding an `operator` writer for `venues.address`. The top precedence rung has
  no caller today (`grep` finds no `source="operator"` in `app/`) — a real gap,
  recorded here, deliberately not closed in a PR that also flips a production
  data path. **F37 makes this a named contingency**: if Downtown Beer Garden
  turns out to have no stored `google_place_id`, the operator writer is the
  fallback and becomes its own plan.
- Enabling `address_backfill_geocoding_enabled` or running the sweep in
  production. This plan ships the reachable path; turning the switch on and
  driving the batches is an operator-gated rollout step, listed under
  Acceptance Criteria as a **named release gate** (F10).
- Removing the confirmed-dead `geocode_by_place_id` / `GoogleGeocodingError` /
  `GOOGLE_GEOCODING_API_BASE` (kept deliberately by
  `plans/260906_address-components-via-place-details.md` correction (B)).
- **Repairing the production `admin.geo_fence_city` circles** (the mispointed
  `rio` / `porto` centres and the five truncated slugs). Explicitly deferred to
  a named follow-up — see
  [Follow-up: geo-fence circle correction](#follow-up-geo-fence-circle-correction-not-this-plan).
  Nothing in this plan writes, validates or renames a fence row.
- **Adding a distance limit to `nearest_city_slug`.** Recorded as a residual
  risk with its real blast radius below, not changed here: today a limit could
  only turn a mislabelled occurrence into a DROPPED one (the projector's
  per-event `except` counts `city_slug cannot be derived` as an error and skips
  the event), which is a worse failure than a wrong label.

## Evidence

### The two data defects, observed live (2026-09-06, read-only)

`GET /events?city=recife&from=2026-09-06&to=2026-10-30&page_size=100` →
`total_count: 53`, `has_more: false`. Neighborhood by venue:

| occurrences | venue | served `neighborhood` | correct? |
|---|---|---|---|
| 15 | Entre Amigos Praia | Boa Viagem | yes |
| 11 | Sala de Reboco | Cordeiro | yes |
| 10 | Club Metrópole | Boa Vista | yes |
| **7** | **Downtown Beer Garden** | **Quintal Espinheiro** | **NO** |
| 6 | Casa Bacurau | Santo Amaro | yes |
| 3 | BeerDock Boa Viagem | Boa Viagem | yes |
| 1 | Tatu Bola | Pina | yes |

7/53 = 13.2%, matching the brief. `GET /venues/ven_3870…496843` gives that
venue's stored address verbatim:

`"Rua Quarenta e Oito, 490 - Quintal Espinheiro Recife - PE 52020-060, Brazil"`

Independent corroboration that the true bairro is **Espinheiro**: OSM/Nominatim
reverse geocode of that venue's own stored coordinates (-8.0415371,
-34.8932571) returns `suburb: "Espinheiro"`, `road: "Rua Quarenta e Oito"`,
`postcode: 52020-060`. `"Quintal Espinheiro"` is the venue complex's own name.
17 other catalog venues already parse to a clean `"Espinheiro"`.

### C1 root cause — a SELECTION defect, not a missing writer and not a broken precedence rule

Traced end to end:

1. **The projection is innocent.** `redis_projection_service.py:449` sets
   `venue_neighborhood=addr.get("neighborhood")` from
   `rds_store.get_address_bulk(venue_ids)` (`rds_venue_store.py:1238-1257`),
   a live `SELECT venue_id, lat, lng, neighborhood FROM venues.address` every
   cycle. Nothing is cached, transformed or defaulted. Whatever RDS holds is
   what the card shows, and a corrected RDS row reaches the app within one
   `redis_projection_minutes` (**2**, `app/config.py:114`) cycle.

2. **The value was written by the text parser, and the parser's own guard
   cannot see this shape.** Traced through `venue_address_parser.parse_address`
   on the exact stored string: UF `PE` → tail → `clean_tail` strips the trailing
   `-` → `split_street_and_blob` takes the LAST standalone dash, so
   `street = "Rua Quarenta e Oito, 490"` and `blob = "Quintal Espinheiro
   Recife"`. No comma in the blob → Format A: the 1-word suffix `"Recife"` hits
   the city vocabulary, and **every remaining leading token becomes the bairro**
   (`bairro_candidate = " ".join(leading) if leading else None`,
   `venue_address_parser.py:290`). Step 8's plausibility guard is gated on
   `street is None` (`:294`) and street is not None here, so it never runs — and
   it would have PASSED anyway (no digit, 2 words ≤ 3, neither "quintal" nor
   "espinheiro" in `STOPLIST_BAIRRO_WORDS`).
   *(Revision 2 corrects the stale line refs `:302`/`:305` used in revision 1;
   the reviewer's `:285-291`/`:294` were right.)*

3. **The Google rung that outranks the parser has NEVER written a single value
   in production.** Prod Prometheus (`prometheus.apivibesensemiddleware.click`,
   read-only):
   - `max_over_time(venue_address_components_total[30d])` = **0** for both
     `written` and `unchanged` → zero google-sourced address writes, ever.
   - `max_over_time(venue_geocoding_requests_total[30d])`:
     `disabled` = **3600**, `success`/`api_error`/`no_place_id` = **0** → the
     full-catalog sweep ran and every one of 3,600 venues hit the
     `address_backfill_geocoding_enabled` early return. Zero paid calls.
   - `max_over_time(venue_address_parsed_total[30d])`: `written` = **3452**,
     `unchanged` = **148**.
   - `venue_address_backfill_remaining{field="neighborhood"}` transitions
     **547 → 0 at 2026-09-06 18:20 UTC** and reads 0 for all four fields now.

4. **Therefore the correcting path is now unreachable, permanently.**
   `list_address_backfill_candidates` (`rds_venue_store.py:273-299`) selects
   only rows where `street IS NULL OR neighborhood IS NULL OR city IS NULL OR
   postal_code IS NULL`. Every row is now non-null. Flipping the switch on and
   resetting `address_backfill_cursor` today selects **zero rows** and changes
   **nothing**. The precedence rule itself is correct and would win
   (`_PROVENANCE_RANK` google 2 ≥ parsed 1, `rds_venue_store.py:207`); it simply
   never gets a candidate. This is the defect.

   The daily cron cannot rescue it either: `enrich_all_venues`
   (`google_places_enrichment_service.py:612-615`) skips any venue that already
   has a `vibe_attributes` row, running only the `businessStatus`-only recheck —
   never `addressComponents`.

5. **The call chain has one more hop than revision 1 recorded (F17).** The
   service does NOT call the store directly:
   `venue_address_backfill_service.py:183`
   `rows = self.venue_repository.list_address_backfill_candidates(after_id, effective_limit)`
   → `venue_repository.py:75-79` `def list_address_backfill_candidates(self,
   after_venue_id, limit: int)`, which forwards **positionally**. The same is
   true of the remaining-count read used for the gauges
   (`venue_address_backfill_service.py:208` →
   `venue_repository.py:82-86`). Any new keyword or new counting method needs
   BOTH layers plus `tests/rds_fake.py`.

### Why the parser is NOT the thing to change (measured, not asserted)

The shipped `parse_address` was run offline against **488 real production
venue addresses** (the whole Recife-metro catalog, pulled read-only from
`POST /venues` and paged by `offset`) with the shipped `STATE_CAPITALS`
vocabulary:

- 395 rows produce a non-null bairro, 93 produce None.
- **204 of the 395 are legitimately multi-word** — "Boa Viagem" (77),
  "Boa Vista" (34), "Casa Forte", "Ilha do Leite", "Poço da Panela",
  "Alto José do Pinho", "Cidade Universitária", "Brasília Teimosa"…
- Searching every multi-word candidate for the over-capture signature (a
  trailing sub-phrase that is itself a bairro seen ≥2× elsewhere in the same
  corpus) finds **exactly one hit: `"Quintal Espinheiro" → "Espinheiro"`.**

So a parser heuristic would repair 1 row out of 395 (0.25%) while exposing 204
correct rows to regression. One adjacent, different failure mode was also
observed and is likewise left alone (`"Carrefour Torre"` for
`"…1315 Boxe 01 - Torre - Carrefour Torre Recife - PE…"`, where the true bairro
is the MIDDLE dash segment) — Google answers it correctly for free; no dash
heuristic does.

`fetch_address_components` (Place Details, minimal `id,addressComponents` mask,
Essentials SKU) is already shipped and already measured 15/15 successful
against real residual servable venues in
`plans/260906_address-components-via-place-details.md`. Google's
`sublocality_level_1` IS the Brazilian bairro. The fix is to let that answer
reach these rows.

### The neighborhood-equals-city pollution class, and where the guard actually belongs (F09/F29)

`venue_address_components._NEIGHBORHOOD_TYPES` ends with
`administrative_area_level_2` and `_CITY_TYPES` BEGINS with it. For any
municipality that publishes no `sublocality*` component,
`map_address_components` therefore returns `neighborhood == city` — and the card
would render the city name as the bairro ("Igarassu · 2,3 km").

Revision 1 put the suppression inside `map_address_components`. **That is the
Google-only path** (`write_mapped_components` is reached from `enrich_venue` and
from `_maybe_fetch_google_address` alone), while the parser writes through
`self.venue_repository.update_venue_address_components(venue_id,
source="parsed", **parsed)` (`venue_address_backfill_service.py:128`) and the
parser has no city comparison at all. 3,452 rows are parser-sourced today and
every venue Google cannot answer stays parser-sourced permanently, so revision
1's guarantee ("`venue_neighborhood` is never equal to the venue's own city")
was true only for the Google half. The shared write boundary
`RdsVenueStore.update_venue_address_components` already enforces the sibling
half of the same guarantee for every writer — `NULLIF(:x, '')`
(`rds_venue_store.py:251-256`) is exactly where "never the empty string" is
made total. The city guard belongs in the same place.

### C2 root cause

`ticket_url` is stored verbatim from the extraction model:
`openai_event_extraction_client.py:527` maps it through `_text()` (strip →
None if empty) and nothing else, matching that module's explicit "this parser
stays dumb: it records exactly what the model said" contract (`:531-539`). The
prompt asks for "a ticketing link if present, else null" (`:211`, `:281`) and
the model answered `"evenyx.com"` — a bare host. `redis_projection_service.py:443`
passes `row.get("ticket_url")` through untouched. `EventOccurrence.ticket_url`
is `Optional[str]` with no validation. Exactly 1 of 21 production event details
carries a `ticket_url` at all, and its value is that bare host.

The projection ALREADY substitutes a serving-shaped value over the RDS one for
a sibling field: `flyer_url = flyer_urls.get(event_id, row.get("flyer_url"))`
(`:421`) replaces the source URL with the CDN one. Normalising `ticket_url` at
the same place is the established shape, not a new one.

### C3 root cause — the projection's start bound is the CALENDAR day (F01, verified here)

Verified against this worktree, not taken on the reviewer's word:

1. `app/services/event_occurrences.py:126`
   `today = _as_aware_utc(reference_time).astimezone(RECIFE_TZ).date()`, then
   `:129` `for offset in range(0, horizon_days + 1)` — recurring occurrences are
   generated from the current Recife **calendar** date forward only. At
   2026-09-10 00:30 local, `today` is 2026-09-10 and the Wednesday-night
   occurrence `evt_X_2026-09-09` (which started at 22:00, 2.5 hours earlier) is
   simply not generated.
2. `app/services/redis_projection_service.py:496-529` prunes `current - fresh`
   from both `events_index_v1:<slug>` and `events_venue_v1:<venue_id>` and then
   `delete_event_occurrence(occ_id)` for every pruned id. So at the first cycle
   after local midnight (`redis_projection_minutes = 2`) the index membership
   AND the payload key are both gone.
3. The **non-recurring** half survives, which is why this was invisible:
   `rds_venue_store.list_events_for_projection` keeps
   `starts_at >= now - PAST_GRACE` with `PAST_GRACE = timedelta(days=1)`
   (`event_projection_selection.py:37`), and `_single_occurrence` re-emits it
   unchanged. The shipped scenario "Keep last night's event visible into the
   small hours" (`tests/bdd/persistence/events-serving-projection.feature:64-69`)
   pins exactly that non-recurring case — and passes — while the recurring case
   it does not cover is broken. Brief field census: `is_recurring` is true on
   **77.4%** of live cards.
4. The index SCORE is already correct: `score = occ.starts_at.timestamp()`
   (`redis_projection_service.py:428`), so vibes_bot's widened `ZRANGEBYSCORE`
   window would find the occurrence — the data is simply absent.
5. vibes_bot's cutoff is a module constant today:
   `app/services/events_service.py:39 _EFFECTIVE_DAY_CUTOFF_HOUR = 6`, used by
   `_effective_local_date` as `if now_local.hour < _EFFECTIVE_DAY_CUTOFF_HOUR:
   return (now_local - timedelta(days=1)).date()`. Its plan promotes it to
   `Settings.EVENTS_NIGHTLIFE_CUTOFF_HOUR` (env, default 6). **6 is the value
   this plan pins**, and the contract below freezes it cross-repo.

### The city vocabulary the index is keyed on, and what is durable about it (F02)

- `redis_projection_service.py:416` `city_slug = nearest_city_slug(lat, lng,
  cities)` where `cities = fence.get("cities")` from
  `rds_store.get_geo_fence()` → `SELECT slug, name, lat, lng, radius_km FROM
  admin.geo_fence_city` (`rds_venue_store.py:2003-2010`). **The index key is
  literally whatever string that column holds** — `events_index_v1:<slug>`.
- `nearest_city_slug` (`app/services/event_city_slug.py:30-39`) picks the
  nearest circle CENTRE by haversine with **no distance limit** and no
  containment requirement.
- `events_known_cities_v1` (`app/dao/redis_venue_dao.py:102`) is a plain Redis
  SET, written by `remember_city_slug` (`:1444-1459`), which is called from
  inside `index_event_occurrence` (`:1442`) — i.e. **once per occurrence write,
  every cycle**, and read back by the projector's own city prune (`:514`).
  There is no TTL and no delete path anywhere in `app/` (`grep`), so it is
  durable for as long as the Redis dataset is.
- **Verified gap (the actionable half of F02):** because the only writer is
  `index_event_occurrence`, a configured city that currently holds ZERO
  occurrences is NOT in the set — and a whole city's occurrences ending
  (or being rejected) does not remove it, but a fresh Redis dataset would come
  back holding only the cities that have live events. vibes_bot's B5 is about to
  422 an incoming slug that is in neither `list_cities()` nor
  `events_known_cities_v1`, which would turn "a configured city with no events
  tonight" into a hard 422 instead of a 200-empty. This plan closes that: the
  projector remembers **every configured geo-fence slug** on every cycle,
  whether or not it has events.
- **Sub-claim of F02 that does not survive checking, and why it does not change
  the fix.** F02's evidence says cs-server indexes under the `STATE_CAPITALS`
  slugs (`belo-horizonte`, `sao-paulo`, `joao-pessoa`). That is what the WRITE
  PATH allows — `validate_geo_fence` (`venue_eligibility.py:527-590`) rejects
  any slug outside `CAPITALS_BY_SLUG` and re-resolves coordinates from the
  server-owned catalog, and migration `0015_geofence_city_circles` seeds only
  `recife`. But the operator's own production enumeration of
  `admin.geo_fence_city` shows TRUNCATED slugs (`belo`, `boa`, `campo`, `sao`,
  `rio`, `porto`) with coordinates that are not the capitals' — rows that
  cannot have come through the admin API and must have been inserted directly.
  Both statements cannot be true of the same table, and the plan does not need
  to resolve it to be correct: the index key is whatever the column holds, and
  the ONE enumerable, authoritative answer to "which slugs does the events index
  actually use" is `events_known_cities_v1`. That is exactly why hardening it,
  rather than asserting a slug list, is the fix. The pre-merge check below
  records the real mapping.

## Current Behavior

- `venues.address.neighborhood` is 100% populated, 100% `parsed`-sourced. One
  catalog row in 395 holds a venue-complex name; that row backs 13.2% of served
  event occurrences because it is a high-frequency events venue.
- The address backfill job selects only rows with a NULL structured column, so
  it now selects nothing and the Google rung is dead code in production.
- Nothing at the shared write boundary stops a `neighborhood` equal to the
  city from being stored, for any writer.
- `ticket_url` reaches the serving projection exactly as the extraction model
  wrote it, including scheme-less hosts and non-URL prose.
- Recurring occurrences are expanded from the current Recife CALENDAR date, so
  every `evt_*_<yesterday>` occurrence is pruned and deleted at the first
  projection cycle after local midnight, and stays absent until 06:00 has no
  meaning at all — it never comes back.
- `events_known_cities_v1` holds only the slugs that have had at least one
  occurrence indexed.

## Desired Behavior

**Bairro.** The address backfill job accepts a selection **mode**. In `upgrade`
mode it selects rows whose structured columns are filled but provenance-ranked
below `google`, so the already-shipped Place Details rung can correct them.
Google's answer replaces a `parsed` neighborhood and stamps
`neighborhood_source = 'google'`. An `operator` value is never replaced. A
Google response that answers nothing leaves the stored value exactly as it is —
never nulled, never poisoned. **Any** incoming neighborhood that fold-compares
equal to that write's city (incoming, else stored) is dropped before it is
written, for every writer and every source. The events projection re-asserts the
corrected bairro on its next 2-minute cycle with no code change of its own.

**Ticket URL.** The events projection normalises `ticket_url` into a
scheme-qualified absolute `http`/`https` URL, or `null` when the stored value is
not a usable web URL. RDS keeps the verbatim extraction. The correction
re-asserts every cycle, so the existing corpus self-heals on deploy with no
migration and no re-extraction.

**Nightlife day.** Recurring occurrences are expanded from the current
**nightlife** day — the Recife calendar date, rolled back one day while the
local hour is below the frozen cutoff (6) — through the unchanged forward
horizon. Last night's 22:00 occurrence is therefore still indexed (and still
has its payload key) at 00:30, and is pruned on the first cycle at or after
06:00 local. The forward edge of the horizon does not move.

**City vocabulary.** Every configured `admin.geo_fence_city` slug is written
into `events_known_cities_v1` on every projection cycle, so the set is a
complete, durable, enumerable list of every slug the events index can be keyed
on — the vocabulary vibes_bot validates incoming `city` params against.

**None of these alters the projection's field set.**

## Implementation Approach

### 1. Provenance-aware candidate selection (the C1 fix)

`RdsVenueStore.list_address_backfill_candidates` gains a keyword-only
`mode: str = "fill"`:

- `"fill"` — today's predicate, byte-for-byte unchanged: at least one of the
  four columns `IS NULL`. This stays the default so no existing caller, test or
  scheduled behaviour changes.
- `"upgrade"` — a row qualifies when any of the four columns is NULL **or** its
  `<field>_source` is `'parsed'`. Expressed as an OR over the same four fields,
  `ORDER BY venue_id ASC LIMIT :limit`, keeping the single-column cursor
  contract that `VenueAddressBackfillService.backfill_batch`'s resume depends
  on. An unknown mode raises `ValueError` (same posture as
  `update_venue_address_components`'s source guard) so a typo cannot silently
  fall back to the wrong population.

**The repository hop (F17).** `VenueRepository.list_address_backfill_candidates`
(`app/dao/venue_repository.py:75-79`) is the method the service actually calls,
and it forwards positionally. It gains the same keyword-only `mode` and
forwards it by keyword. `VenueAddressBackfillService.backfill_batch` gains
`mode` and passes it through to the repository, not to the store.
`InMemoryRdsVenueStore` (`tests/rds_fake.py`) mirrors the new parameter with the
same semantics — it is the contract this store's docstring already promises to
follow.

`_run_address_components_backfill` reads `cfg.get("mode")`, defaulting to
`"fill"`, mirroring `instagram_profile_photos`' existing
`"default_config": {"mode": "backfill"}` knob (`admin_trigger_router.py:321`).
`JOB_REGISTRY["address_components_backfill"]["default_config"]` becomes
`{"limit": "", "mode": "fill"}` and its `description` documents both modes plus
the existing cursor-reset instruction.

Cost containment is unchanged and load-bearing: `upgrade` mode still runs behind
`address_backfill_geocoding_enabled` (default OFF — `AdminConfigService.get`
returns None for an unset key and `bool(None)` is False), still processes one
bounded batch per trigger (`settings.address_backfill_batch_size = 200`), still
advances the cursor monotonically so a full sweep is a finite number of
operator-driven triggers. A full upgrade sweep is ≈3,452 Place Details calls at
the Essentials SKU against a 10,000/month free allowance — **$0**, and billed
under a different SKU from the daily vibe-enrichment cron's
Enterprise+Atmosphere Place Details calls, so the two do not compete. Rows
Google cannot answer stay in the `upgrade` population; that is correct (they are
still unimproved) and costs one `no_place_id`/`api_error` outcome per sweep, not
a paid call in the `no_place_id` case.

### 2. Suppress a neighborhood that is just the city — at the SHARED write boundary (F09)

**Changed from revision 1**: the guard moves OUT of
`venue_address_components.map_address_components` (Google-only) and into
`RdsVenueStore.update_venue_address_components`, the single boundary every
writer already passes through — `google` (`write_mapped_components`), `parsed`
(`apply_parser_fallback`) and any future `operator` writer.

Inside that method, before the UPDATE is built:

- Resolve the city this write compares against: the incoming `city` when
  non-empty, otherwise the row's stored `city` (one `SELECT city FROM
  venues.address WHERE venue_id=:venue_id` inside the same
  `engine.begin()` transaction — the method already opens one).
- If `fold_text(neighborhood) == fold_text(comparison_city)` and both are
  non-empty, set the outgoing `neighborhood` to `None`. `NULLIF(:x,'')`/the
  existing per-field guard then makes it a no-op for that column; every other
  column writes normally.
- `app.utils.text_norm.fold_text` (`:18-25`) is the repo's existing
  accent-fold + casefold + punctuation-strip comparator, so "SÃO PAULO" and
  "sao paulo" compare equal.

Exactly one definition, so there is no drift between a pure mapper and a write
path. A None never clobbers under the precedence-guarded write, so this can only
ever prevent a bad write — it can never erase a good stored value, and it
deliberately does NOT repair rows already stored (see the contract's honest
wording and F29).

`map_address_components` stays pure and unchanged.

### 3. `ticket_url` normalisation at projection (the C2 fix)

New pure module `app/services/event_ticket_url.py` exposing
`normalize_ticket_url(value: Optional[str]) -> Optional[str]`. No I/O, no
config, fully unit-testable. Applied in `RedisProjectionService.project_events`
where the payload is built (`redis_projection_service.py:443`), in the same
place and the same spirit as the existing `flyer_url` substitution.

The rules, in order (this is the contract vibes_bot and mobile plan against, so
it is pinned here):

1. Not a string, or None → `None`.
2. Strip surrounding whitespace, then strip trailing `.` `,` `;` `!` and
   whitespace again (caption-scraped links routinely end in sentence
   punctuation). Empty result → `None`.
3. Longer than 2048 characters → `None`.
4. Contains any internal whitespace → `None` (it is prose, not a URL).
5. Case-insensitively begins `http://` or `https://` → returned as-is (after
   step 2's trim). Never rewritten, never upgraded http→https.
6. **(Revised — F30.)** Begins with any OTHER scheme → `None`. A leading token
   counts as a scheme only when the text before the FIRST `:` matches
   `^[A-Za-z][A-Za-z0-9+.-]*$` **and contains no `.`**. So `mailto:`, `tel:`,
   `whatsapp:`, `javascript:`, `data:` are still dropped, while a scheme-less
   `host:port` (`evenyx.com:8080/lote2`) is NOT mistaken for a foreign scheme —
   revision 1's `^[A-Za-z][A-Za-z0-9+.\-]*:` matched it because `.` is inside
   the character class, and dropped a legitimate ticket link.
7. A leading `//` is stripped, then the value is treated as scheme-less.
8. **(Revised — F30.)** Scheme-less: take the authority = text up to the first
   `/`, `?` or `#`. If the authority carries a `:`, everything after the LAST
   `:` must be 1–5 digits (a port) — otherwise → `None`; the port is stripped
   for the host check and preserved in the output. Accept the remaining host
   only when it contains no `@`, contains at least one `.`, and its last
   dot-separated label is ≥2 characters and entirely alphabetic. On acceptance
   return `"https://" + value` (the ORIGINAL value, port, path, query and
   fragment preserved exactly). `https` and not `http`: a purchase page served
   over plaintext draws a browser interstitial, and every real ticketing host
   redirects http→https anyway.
9. Anything else → `None`.

Verified against the only production value: `"evenyx.com"` → `"https://evenyx.com"`.
Verified against F30's counter-example: `"evenyx.com:8080/lote2"` →
`"https://evenyx.com:8080/lote2"` (revision 1 returned `None`).

RDS is deliberately NOT rewritten. The extraction client's "records exactly what
the model said" contract stays intact, the admin console keeps showing the
source value for auditing, and the serving correction re-asserts on every cycle
— so the fix reaches production the moment the code deploys, with no migration
and no backfill.

### 4. Expand from the NIGHTLIFE day, not the calendar day (the C3 fix — F01)

In `app/services/event_occurrences.py`:

- New frozen module constant, the SOLE definition in this repo:

  `NIGHTLIFE_CUTOFF_HOUR = 6` — "before this Recife local hour, the nightlife
  'today' is still last night's date". Its docstring names
  `vibes_bot app/services/events_service.py::_EFFECTIVE_DAY_CUTOFF_HOUR`
  (promoted by that repo's plan to `Settings.EVENTS_NIGHTLIFE_CUTOFF_HOUR`,
  default 6) and mobile's `RECIFE_NIGHTLIFE_CUTOFF_HOUR`, and states that the
  three must change together or the projection, the served window and the
  client's day labels desynchronise.
- New pure helper `nightlife_date(reference_time) -> date`: the Recife local
  date of `reference_time`, minus one day when the local hour is
  `< NIGHTLIFE_CUTOFF_HOUR`. Same rule, same direction, same strict `<` as
  vibes_bot's `_effective_local_date`. Exported in `__all__` so a future caller
  (or a test) never re-derives it.
- `expand_occurrences` replaces its single `today` with two bounds:
  - `start_day = nightlife_date(reference_time)`
  - `end_day = <Recife calendar date of reference_time> + horizon_days`

  and iterates the CLOSED interval `[start_day, end_day]`.

**Deliberate deviation from F01's literal prescription, recorded here.** F01
prescribed keeping `range(0, horizon_days + 1)` from the rolled-back day, which
would also roll the FORWARD edge back by one day between 00:00 and 06:00. That
makes every occurrence exactly 21 days out disappear at midnight and reappear
at 06:00 — a nightly delete/re-create of the horizon-edge payload keys and a
user-visible flap at the tail of the "Todos os eventos" window. Anchoring
`end_day` to the calendar date keeps the forward horizon EXACTLY as shipped
(`horizon_days = 21` from today, 22 calendar days) and only ever ADDS the one
extra day at the near end, for six hours a night. Cost: at most one additional
expanded day per recurring event during those six hours; the 53-occurrence live
corpus makes that a rounding error against the 20 MB Redis working set.

Everything else in the module is untouched: the weekday filter, the clock-time
carry-over, the `<event_id>_<YYYY-MM-DD>` id format, the "no computable day →
single occurrence" branch. `redis_projection_service` needs NO change for C3 —
it already passes `reference_time=now` and scores by `starts_at` — and the prune
then keeps yesterday's occurrence simply because it is in the fresh set again.

Interaction with selection, checked: a recurring row stays selected by
`list_events_for_projection` on SOURCE freshness
(`agg.last_seen_at >= now - events_recurring_max_source_age_days`), not on
`starts_at`, so nothing else has to change for the D-1 occurrence to be
generated.

### 5. Make `events_known_cities_v1` the complete city vocabulary (F02)

In `RedisProjectionService.project_events`, immediately after the preparation
block that reads the fence (`redis_projection_service.py:354`), remember
every configured slug:

```
for city in cities:
    self.redis_only_dao.remember_city_slug(city["slug"])
```

wrapped in its own `try/except` that logs and continues — the same posture as
every other best-effort read/write in that method; the set is an optimisation
for the prune and a vocabulary for vibes_bot, never a reason to lose a cycle.

`remember_city_slug`'s docstring is updated: the set is now "every slug ever
INDEXED under **or ever CONFIGURED in the fence**". Nothing else changes — same
key, same type, no TTL, no delete path, and the prune already reads
`configured_slugs | known_slugs`, so the union it walks is unchanged in
practice.

Effect for vibes_bot's B5: after one projection cycle (≤ 2 minutes) the set
contains every slug the events index can be keyed on, including cities that hold
no events tonight — so validating an incoming `city` against it 200-empties a
configured-but-quiet city instead of 422-ing it, and a Redis dataset rebuilt
from scratch re-populates the complete vocabulary on the next cycle rather than
only the cities that happen to have live events.

### 6. Files touched (explicit, so no hop is skipped — F17)

**Modified**
- `app/dao/rds_venue_store.py` — keyword-only `mode` on
  `list_address_backfill_candidates` (+ `ValueError` on an unknown mode); the
  neighborhood-equals-city suppression inside
  `update_venue_address_components`; new `count_address_source_rows` and
  `count_address_neighborhood_equals_city`.
- `app/dao/venue_repository.py` — forward `mode` by keyword on
  `list_address_backfill_candidates`; new `count_address_source_rows` and
  `count_address_neighborhood_equals_city` wrappers. **This is the hop the
  service actually calls; missing it is F17.**
- `app/services/venue_address_backfill_service.py` — `mode` on
  `backfill_batch`, passed to the REPOSITORY; the two new gauges set at
  batch end alongside `VENUE_ADDRESS_BACKFILL_REMAINING`.
- `app/routers/admin_trigger_router.py` — `_run_address_components_backfill`
  (`:105-115`) reads `cfg.get("mode")`; the `JOB_REGISTRY` entry (`:200-214`)
  widens its `default_config` and description.
- `app/services/event_occurrences.py` — `NIGHTLIFE_CUTOFF_HOUR`,
  `nightlife_date`, the two-bound expansion, `__all__`.
- `app/services/redis_projection_service.py` — apply
  `normalize_ticket_url` at the payload build; remember every configured
  fence slug each cycle; bump the two new counters.
- `app/dao/redis_venue_dao.py` — `remember_city_slug`'s docstring only (the
  set's meaning widens; its key, type and lifecycle do not).
- `app/metrics.py` — the four new series, every label value pre-initialised.
- `tests/rds_fake.py` — `mode` and both new counting methods on
  `InMemoryRdsVenueStore`, mirroring the real store's semantics.

**Added**
- `app/services/event_ticket_url.py` — `normalize_ticket_url`, pure.
- `tests/bdd/persistence/events-venue-bairro-and-ticket-url.feature` and its
  step module (reusing the existing projection harness — see Test Plan).
- `tests/test_event_ticket_url.py`.

**Read but not modified**
- `app/services/venue_address_parser.py` — deliberately unchanged (Non-goals).
- `app/services/venue_address_components.py` — `map_address_components` stays
  pure; revision 1's suppression is NOT added here (F09).
- `app/services/event_city_slug.py` — unchanged (Non-goals; the follow-up owns
  any distance limit).

## Data, Config, And API Impact

- **Migration:** none. No schema change.
- **Redis / projection contract:** no field added, removed, renamed or retyped.
  `EventOccurrence` is untouched. The VALUES of `venue_neighborhood` and
  `ticket_url` change; the SET of members in `events_index_v1:<slug>` /
  `events_venue_v1:<venue_id>` gains the previous nightlife day's recurring
  occurrences while the local clock is before 06:00; `events_known_cities_v1`
  gains configured-but-eventless slugs.
- **RDS writes:** `venues.address.{neighborhood,street,city,postal_code}` and
  their `*_source` columns, only through the existing precedence-guarded
  `update_venue_address_components`. `events.post_item.ticket_url` is not
  written at all.
- **Config:** no new setting and no new admin-config key. `mode` is a per-run
  trigger config value, defaulting to today's behaviour.
  `NIGHTLIFE_CUTOFF_HOUR` is a frozen module constant, deliberately NOT a
  setting and NOT admin-flippable: the same value must hold for this repo's
  projection window, vibes_bot's serving window and mobile's day labels within
  one release train.
- **Feature flags:** none added. `address_backfill_geocoding_enabled` (existing,
  default OFF) still gates every paid call. `events_projection_enabled` is
  unchanged.
- **Spend:** a full `upgrade` sweep is ≈3,452 Place Details Essentials calls,
  one-time, inside the 10,000/month free allowance. $0/month. No new billed
  surface; the paid call is the one PR #223 already shipped. C3 and the
  known-cities write add zero external calls.
- **API:** `GET /admin/jobs` shows the widened `default_config` and description
  for `address_components_backfill`. No other endpoint changes.

## Contract out — what this repo hands downstream

Everything below is what vibes_bot (and, through it, mobile) may depend on
AFTER this plan lands. Nothing here is a new field.

**Frozen cross-repo constant.**
`NIGHTLIFE_CUTOFF_HOUR = 6` (`app/services/event_occurrences.py`), timezone
`America/Recife`. Rule: while the Recife local hour is `< 6`, the nightlife day
is the local calendar date **minus one day**; from 06:00 it is the local
calendar date. This MUST equal vibes_bot's `_EFFECTIVE_DAY_CUTOFF_HOUR` /
`Settings.EVENTS_NIGHTLIFE_CUTOFF_HOUR` (default 6) and mobile's
`RECIFE_NIGHTLIFE_CUTOFF_HOUR`. **Changing it in any one repo without the other
two silently breaks the window**: cs-server would stop projecting hours that
vibes_bot still queries, or keep projecting hours mobile labels as yesterday.
Treat it as frozen for the Events UI v1 release train; a change is a
coordinated, three-repo release.

**Projection window (what the index can contain).**
- Recurring occurrences: every matching local day in the CLOSED interval
  `[nightlife_date(now), local_calendar_date(now) + events_projection_horizon_days]`.
- `events_projection_horizon_days = 21` (`app/config.py:891`) — **so the index
  can never contain a recurring occurrence more than 21 days past today,
  regardless of any wider request window** (F18). The live census matches
  exactly: dates run 09-06 → 09-27 (= 09-06 + 21), then only sparse
  non-recurring events beyond. Any "roughly two months of everything" framing
  downstream is structurally unfillable; the tail of a 59-day request is
  expected to be sparse BY CONSTRUCTION, not by a bug.
- Non-recurring occurrences: selected while `starts_at >= now - 1 day`
  (`PAST_GRACE`), unchanged.
- Index score is always `starts_at` epoch seconds (UTC).
- Projection cadence: `redis_projection_minutes = 2`.

**Redis keys (unchanged shapes).**
- `event_occurrence_v1:<occurrence_id>` — the payload (JSON), no TTL,
  re-asserted every cycle, deleted when the occurrence leaves both indexes.
- `events_index_v1:<city_slug>` — ZSET, member = occurrence id, score =
  `starts_at` epoch.
- `events_venue_v1:<venue_id>` — ZSET, same members and scores, venue-scoped.
- `events_known_cities_v1` — **plain Redis SET (read it with `SMEMBERS`)**, no
  TTL, no expiry, no delete path in `app/`. Members are exactly the `<city_slug>`
  strings used to key `events_index_v1:<slug>`. Lifecycle after this plan:
  re-asserted on EVERY projection cycle (≤ 2 min) for (a) every slug in
  `admin.geo_fence_city` and (b) every slug an occurrence was indexed under this
  cycle; never pruned, so a slug removed from the fence stays a member. It is
  the authoritative, enumerable events-city vocabulary — safe for vibes_bot to
  validate an incoming `city` against, and safe to read on the request path
  (one SMEMBERS over a set bounded by the number of cities ever configured, 27
  capitals at most today). Read failures must fail OPEN downstream: an empty
  read means "Redis had a bad moment", never "no cities exist".
- `city_slug` derivation: the slug of the NEAREST `admin.geo_fence_city` circle
  CENTRE to the venue's stored coordinates (`nearest_city_slug`, haversine, no
  distance limit, no containment requirement). It is therefore NOT guaranteed to
  be the slug of the city the venue is administratively in — see Residual risks.

**`venue_neighborhood` (honest wording — replaces revision 1's absolute
guarantee; F09/F29).**
- It is never the empty string (`NULLIF(:x,'')` at the write boundary).
- For every value **written after this change lands**, by any writer
  (`google`, `parsed`, future `operator`): it is never equal, accent- and
  case-insensitively, to that write's city.
- It is NOT retro-repaired: rows written before this change are untouched until
  something rewrites them. The `venue_address_neighborhood_equals_city` gauge
  below reports how many such rows exist, so the claim is measured, not assumed.
- For `google`-sourced values it is Google's `sublocality_level_1` (the
  Brazilian bairro) with `sublocality` and `administrative_area_level_2` as
  fallback rungs.
- For `parser`-sourced values it remains a **best-effort display hint**: it may
  still be a venue or complex name (`"Quintal Espinheiro"`, `"Carrefour Torre"`)
  and is only guaranteed distinct from the city for values written after this
  change. vibes_bot must echo this qualified wording to mobile, NOT an
  unconditional guarantee.

**`ticket_url`.** Either `null`, or an absolute `http`/`https` URL, per the nine
rules in §3. Never a bare host, never a `mailto:`/`tel:`/`javascript:`/`data:`
value, never prose. RDS keeps the verbatim extraction; the normalisation is a
serving concern and re-asserts every cycle.

**Field set.** Unchanged. `EventOccurrence` gains and loses nothing.

## Error Handling And Observability

- `normalize_ticket_url` never raises: every non-conforming input returns None.
  It is called inside `project_events`' existing per-event try/except, so even a
  pathological value cannot cost the cycle.
- New counter **`events_projection_ticket_url_total{outcome}`**, bumped once per
  projected occurrence:
  `absent` (nothing stored) | `passthrough` (already absolute http/https) |
  `scheme_added` (a scheme-less host was qualified) | `rejected` (stored, but
  not a usable URL — the operator's signal that extraction is emitting prose).
  All four outcomes pre-initialised at import, so a zero is a measured zero.
- New gauge **`venue_address_source_rows{field,source}`** — the count of
  `venues.address` rows per structured field per provenance
  (`operator`/`google`/`parsed`/`none`), refreshed alongside
  `VENUE_ADDRESS_BACKFILL_REMAINING` at the end of every backfill batch. This
  closes a real blind spot: `venue_address_backfill_remaining` reads 0 whether
  the stored data is right or wrong, which is precisely why this defect was
  invisible. It is also the only way an operator can watch a `parsed → google`
  sweep progress and prove it finished. **Both layers plus the fake (F17):**
  new `RdsVenueStore.count_address_source_rows` (one `GROUP BY` over field and
  source), new `VenueRepository.count_address_source_rows` forwarding to it, and
  the `InMemoryRdsVenueStore` equivalent — the service reads it through the
  repository exactly like `count_address_backfill_remaining`.
- New gauge **`venue_address_neighborhood_equals_city`** (no labels) —
  the number of `venues.address` rows whose stored `neighborhood` equals its
  stored `city` under `lower(btrim(...))`. Read through the same two layers
  (`RdsVenueStore.count_address_neighborhood_equals_city` →
  `VenueRepository.count_address_neighborhood_equals_city` → the fake) and
  refreshed by the same batch-end call.
  This is what makes the contract's neighborhood-vs-city claim MEASURED rather
  than asserted, and it is how the operator sees the residual (pre-change) rows
  drain as the sweep rewrites them. Documented limitation: SQL-side comparison
  is case-insensitive but not accent-folding (no `unaccent` extension in this
  database), so it can undercount a purely accent-differing pair; the write-time
  guard itself DOES fold accents via `fold_text`.
- New counter **`events_projection_nightlife_rollback_total`** — bumped once per
  projection cycle that ran with the nightlife day rolled back (local hour < 6).
  One cheap, unambiguous production signal that C3's code path is actually live
  and firing in the six hours where it matters; an absent/zero series after a
  night in production means the deploy did not take (the diagnostic pattern the
  vibes_bot `/metrics` playbook already relies on).
- An unknown backfill `mode` raises `ValueError` at the DAO boundary and is
  surfaced by the existing trigger error path — never a silent fallback to the
  wrong population.
- Existing `venue_geocoding_requests_total` / `venue_address_components_total`
  labels are unchanged, so the PR #223 dashboards keep working.

## Test Plan

Feature file: `tests/bdd/persistence/events-venue-bairro-and-ticket-url.feature`

The nightlife-day scenarios drive the REAL projector
(`RedisProjectionService.project_events(now=…)`) over the shared BDD harness
that `events_serving_projection_steps.py` already builds — RDS fake + Redis
fake DAO + a controllable "current time in Recife" — and assert on the index and
payload keys the projector itself wrote. **No scenario may hand-seed
`events_index_v1:*` or `event_occurrence_v1:*`**; F01's whole point is that a
hand-seeded index would go green while production stayed broken.

Scenarios (ticket URL) — 9:
- **Qualify a scheme-less ticket host with a scheme** — a stored
  `"evenyx.com"` is projected as `"https://evenyx.com"`, counted `scheme_added`.
- **Preserve the path, query and fragment when adding the scheme.**
- **Qualify a scheme-less ticket host that carries a port** (F30) —
  `"evenyx.com:8080/lote2"` → `"https://evenyx.com:8080/lote2"`.
- **Project no ticket url for a host whose pseudo-port is not numeric** (F30) —
  `"evenyx.com:lote2"` → null, so rule 8's port branch cannot become a hole.
- **Leave an already absolute ticket url exactly as stored** — `passthrough`.
- **Do not upgrade a stored plaintext ticket url to https.**
- **Project no ticket url for caption prose** — counted `rejected`.
- **Project no ticket url for a non-web scheme** — `mailto:`.
- **Project no ticket url when the extraction found none** — counted `absent`.
- **Leave the stored ticket url verbatim in the system of record.**
- **Re-assert the normalised ticket url on the next projection cycle.**

Scenarios (bairro):
- **Select a fully populated parsed address row in upgrade mode.**
- **Do not select a fully populated parsed address row in the default mode.**
- **Reject an unknown selection mode instead of falling back.**
- **Replace a parsed venue-complex name with Google's bairro** —
  `neighborhood_source` becomes `google`.
- **Never replace an operator-set bairro.**
- **Never null a stored bairro when Google answers nothing.**
- **Suppress a Google neighbourhood that is only the city name.**
- **Drop a parsed bairro that is only the city name** (F09) — the PARSER path,
  through the same shared write boundary, which is the half revision 1 missed.
- **Fold case and accents when comparing a bairro to the stored city** (F09) —
  proves the guard folds AND that it compares against the STORED city when the
  write itself carries no city.
- **Store a bairro that genuinely differs from the city** — the guard can only
  ever suppress the equal case; it must not become a blanket drop.
- **Write the other address fields even when the bairro is dropped** — street
  still lands; the suppression is per-column, not per-write.
- **Make no address lookup at all while the free-tier switch is off.**
- **Serve the corrected bairro on the next projection cycle.**
- **Report the address provenance distribution after a batch.**
- **Report how many stored bairros are still just the city name** — the
  `venue_address_neighborhood_equals_city` gauge, which is what makes the
  contract's wording measured rather than asserted.

Scenarios (nightlife day — C3/F01):
- **Keep last night's recurring occurrence in the index at 00:30 local** — a
  "toda quarta" 22:00 announcement; at 2026-09-10 00:30 the projector still
  writes `…_2026-09-09`, its payload key exists, and it is scored by its own
  `starts_at` (2026-09-09 22:00 Recife).
- **Keep last night's recurring occurrence in the venue index too** — both
  indexes are written together, so both must be asserted.
- **Prune last night's recurring occurrence after 06:00 local** — the same
  event, projected again at 06:30; the 09-09 member is gone from the city AND
  venue indexes and its payload key is deleted.
- **Roll the near edge back only while the local hour is below the cutoff** —
  05:59 still includes 09-09, pinning the strict `<` boundary vibes_bot uses.
- **Keep the forward horizon unchanged during the small hours** — a "todo dia"
  announcement at 00:30 reaches 2026-10-01 (= calendar 09-10 + 21) and not
  2026-10-02 (the deviation recorded in §4).
- **Keep the same forward horizon after the cutoff has passed** — 06:30 gives
  the identical forward edge, which is what "the forward edge does not move"
  means.
- **Keep last night's non-recurring occurrence in the index as well** — the
  shipped `PAST_GRACE` behaviour still holds, so C3 is additive.
- **Count the cycles that ran with the nightlife day rolled back** /
  **Do not count a rollback for a cycle that ran after the cutoff** — the
  production signal that the code path is live.

Scenarios (city vocabulary — F02):
- **Remember a configured geo-fence city that holds no events.**
- **Keep remembering a city after its last occurrence is gone** — the fence is
  reduced and the slug survives, because the prune depends on it.
- **Rebuild the whole vocabulary on the next cycle after the set is erased.**
- **Keep projecting when the known-cities write fails** — the §5 write is
  best-effort and must never cost a cycle.

Existing coverage that must stay green unchanged:
`tests/test_events_redis_dao.py::test_no_known_city_slugs_before_anything_is_ever_indexed`
(`:164-167`) asserts the key does not exist before anything is indexed. §5
changes the PROJECTOR, not the DAO, so `remember_city_slug` keeps its exact
semantics and that test is unaffected — if it goes red, the guard was put in
the wrong layer.

Pytest unit tests:
- `tests/test_event_ticket_url.py` — a table-driven pass over
  `normalize_ticket_url`: bare host, `www.` host, host with path/query/fragment,
  **host with a numeric port** and **host with a non-numeric pseudo-port**
  (F30), absolute http, absolute https, uppercase `HTTPS://`,
  protocol-relative `//`, `mailto:`, `tel:`, `javascript:`, `data:`, an
  email-shaped value, a bare word with no dot, a trailing-period link, a
  whitespace-containing sentence, an over-length string, `""`, `"   "`, `None`,
  and a non-string. Includes the literal production value `"evenyx.com"` and
  F30's `"evenyx.com:8080/lote2"`.
- `tests/test_event_occurrences.py` — extend for the nightlife day: at
  05:59 local the expansion starts on yesterday, at 06:00 it starts today, the
  forward edge is `calendar_date + horizon_days` in BOTH cases, and
  `nightlife_date` is asserted directly at 00:00, 05:59, 06:00 and 23:59 (the
  strict-`<` boundary vibes_bot uses).
- `tests/test_venue_address_backfill_service.py` — extend for the `mode`
  pass-through **through the repository wrapper** (F17) and for "upgrade mode
  with the switch off makes no client call" (guard the assertion against
  vacuity the way
  `plans/260906_address-components-via-place-details.md` correction (C) requires:
  the spy must be on the method production code actually calls,
  `fetch_address_components`).
- `tests/test_rds_venue_store_address.py` (or the existing store test module) —
  the neighborhood-equals-city suppression at the write boundary: incoming
  city, stored-city fallback, accent/case folding, and proof that a genuinely
  different neighborhood still writes and that the other three fields are
  unaffected when the neighborhood is dropped.
- A DAO-level test of the `upgrade` predicate against the in-memory store,
  asserting default-mode selection is unchanged and an unknown mode raises, plus
  `count_address_source_rows` and `count_address_neighborhood_equals_city` at
  both the store and repository layers and in the fake.

Manual or integration checks:
- `make test-unit` and `make test-bdd` green.
- Replay the shipped `parse_address` over the 488-address production sample
  captured for this plan and confirm the parser's output is IDENTICAL to the
  pre-change run — this change must not move a single parsed value.
- One events projection pass against a real `redis:7`, confirming the payload
  byte size and key count are unchanged from the
  `plans/260905_events-serving-projection.md` baseline (the field set did not
  change; only two values did), and that `SMEMBERS events_known_cities_v1`
  returns every configured fence slug.

## Acceptance Criteria

### Pre-merge (all read-only or local; nothing here needs the paid switch)
- `make test-unit` and `make test-bdd` are green and the feature file's `@wip`
  tag is removed.
- The events serving projection's field set is unchanged — a field-by-field diff
  of `EventOccurrence` against `plans/260905_events-serving-backend.md`'s
  contract table shows no addition, removal, rename or retype.
- A stored `ticket_url` of `"evenyx.com"` is projected as `"https://evenyx.com"`;
  `"evenyx.com:8080/lote2"` as `"https://evenyx.com:8080/lote2"`; a stored
  non-URL as `null`; an already-absolute URL unchanged. The RDS row is
  unmodified in all cases.
- The default backfill mode selects exactly the population it selects today —
  proven by a test that the pre-change predicate and the `fill` predicate return
  the same rows.
- `upgrade` mode selects a fully-populated `parsed` row, and makes zero Place
  Details calls while `address_backfill_geocoding_enabled` is off.
- At 00:30 Recife the projector writes and keeps yesterday's recurring
  occurrence; at 06:30 it prunes it and deletes its payload key; the forward
  horizon edge is identical in both runs.
- `SMEMBERS events_known_cities_v1` contains every configured geo-fence slug
  after one cycle, including a city with zero events.
- `venue_address_source_rows{field,source}`,
  `venue_address_neighborhood_equals_city`,
  `events_projection_ticket_url_total{outcome}` and
  `events_projection_nightlife_rollback_total` are exported with every label
  value pre-initialised.
- **F37 — the two-part read-only probe, BEFORE merge, not before the sweep:**
  (a) confirm `ven_3870…496843` (Downtown Beer Garden) has a NON-NULL
  `google_places.vibe_attributes.google_place_id` — without one,
  `_maybe_fetch_google_address` returns `no_place_id`
  (`venue_address_backfill_service.py:82-87`), writes nothing, and the entire
  C1 half of this PR changes no served value; and (b) probe Place Details with
  the minimal `id,addressComponents` mask for that `place_id` and confirm
  `sublocality_level_1 == "Espinheiro"`.
  **Fallback if either fails, decided now so execution does not stall:** stop,
  do NOT invent a parser heuristic, and open the deferred `operator` writer as
  its own plan (a one-venue `PUT`-style correction through
  `update_venue_address_components(source="operator")`, which the precedence
  rule already protects from being overwritten). Merging the rest of this PR is
  still correct — C2, C3 and the guard stand alone — but the C1 rollout gate
  below must then be re-pointed at the operator write.
- **F02 — record the real mapping before merge:** enumerate
  `admin.geo_fence_city` (slug, name, lat, lng, radius_km) and
  vibes_bot's `GET /cities`, and paste both lists into the PR description
  alongside `SMEMBERS events_known_cities_v1` from a real cycle. This is the
  evidence vibes_bot's B5 validation is built on, and it is what settles the
  contradiction recorded in the Evidence section. Read-only; no write.

### Post-merge rollout — an OPERATOR-GATED, NAMED RELEASE GATE (F10)
Not part of the PR, and **explicitly a gate on the mobile 1.4.0 release
dispatch**, not a paragraph buried in this repo's PR:

1. Enable `address_backfill_geocoding_enabled`.
2. `PUT /admin/config/address_backfill_cursor` with `{"last_venue_id": null}`.
3. Drive `address_components_backfill` with `{"mode": "upgrade"}` until `done`
   is true, watching `venue_geocoding_requests_total{outcome="success"}` stay far
   below the 10,000/month Essentials allowance and
   `venue_address_source_rows{field="neighborhood",source="google"}` climb.
4. Confirm via the live API that Downtown Beer Garden's event cards carry
   `neighborhood: "Espinheiro"` and that no other venue's neighborhood regressed
   against the table in Evidence.
5. **Only then** may the operator dispatch the mobile EAS release. Until step 4
   passes, 13.2% of event cards still read "Quintal Espinheiro" — shipping the
   Events UI before it is shipping the exact defect the UI round exists to fix.

This gate must also appear as a checklist item in the wrapper coordination plan
and in the mobile plan's pre-release device-validation list; this repo cannot
enforce it alone, and F10's whole point is that no plan sequenced it.

## Follow-up: geo-fence circle correction (NOT this plan)

**Recorded so it cannot be lost. Operator has approved the correction; this
plan deliberately does not take it.**

The evidence handed to this revision: production `admin.geo_fence_city` holds
circles whose slugs are truncated labels (`belo`, `boa`, `campo`, `sao`, and
`porto`/`rio`), and two of them are centred ~1,300 km from the cities they are
named after — `rio` at `-16.4409, -55.4986` and `porto` at `-19.3983, -57.5608`,
both in Mato Grosso. Neither slug nor coordinate pair can have been written
through the admin API: `validate_geo_fence` (`venue_eligibility.py:527-590`)
rejects any slug outside `CAPITALS_BY_SLUG` and re-resolves coordinates from
the server-owned `STATE_CAPITALS` catalog, where `rio-de-janeiro` is
`-22.9068, -43.1729` and `porto-alegre` is `-30.0346, -51.2177`. So these rows
were inserted out-of-band, and the correction is a DATA repair (plus possibly a
slug rename), not a code change this plan could carry.

**Why it is a separate plan, not this one:**

1. **Different blast radius.** `admin.geo_fence_city` is read by
   `serving.eligible_venue` — the SQL view that decides whether a venue is
   served AT ALL. Re-pointing a circle re-partitions serving, not just labels:
   venues currently inside the bogus Mato Grosso circles LOSE servability, and
   venues near the real Rio/Porto Alegre gain it. That is a catalog-wide
   serving change; this plan changes two field values and one date bound.
2. **It is cross-repo in a way this round cannot absorb.** Correcting the slugs
   (`rio` → `rio-de-janeiro`) renames the `events_index_v1:<slug>` keys and
   changes the vocabulary vibes_bot's B5 validates against — in the same release
   train that is introducing that validation. Sequencing a vocabulary change
   into that is how a 422-on-every-city outage happens.
3. **The projection re-asserts, so nothing is lost by waiting.** A corrected
   fence takes effect on the next 2-minute cycle whenever it lands.

**What the follow-up plan must show (do not let it skip these):**

- A read-only enumeration of `admin.geo_fence_city` BEFORE the change, and a
  per-venue `nearest_city_slug` computation over the whole catalog under both
  the current and the corrected circle sets — i.e. the exact list of venues
  whose `city_slug` would FLIP, produced before anything is written.
- The servability delta from `serving.eligible_venue` under both fences (count
  and venue ids gained and lost), because a re-pointed circle can remove venues
  from serving entirely. Zero unexplained losses, or an explicit operator
  decision per lost venue.
- What happens to the orphaned `events_index_v1:<old-slug>` ZSETs: they are
  pruned by the projector's own city prune only because `events_known_cities_v1`
  keeps remembering the old slug (this plan's §5 makes that stronger, not
  weaker) — the follow-up must confirm the old key drains to empty rather than
  being left with stale members, and must NOT delete the known-cities member.
- Coordination with vibes_bot: any slug rename is announced before it lands, and
  the mapping is re-recorded in both plans.
- Whether `nearest_city_slug` should gain a distance limit at the same time —
  and if so, what happens to a venue beyond the limit (today it would raise,
  be counted as a per-event error, and DROP the event from serving; a follow-up
  wanting a limit must add a "no city" disposition rather than an exception).

## Residual risks

- **A parser-sourced bairro can still be a venue or complex name** for any
  venue Google cannot answer (93/488 measured unanswered). The contract wording
  above says so explicitly rather than promising otherwise; vibes_bot must echo
  the qualified wording (F29).
- **Rows already stored with `neighborhood == city` are not repaired** by the
  write-time guard. `venue_address_neighborhood_equals_city` measures how many
  exist; the upgrade sweep rewrites them only where Google answers.
- **`nearest_city_slug` has no distance limit**, so `city_slug` is "nearest
  configured circle centre", not "the city this venue is in". With the fence
  ENABLED this is mostly self-limiting — `serving.eligible_venue` only serves a
  venue inside SOME circle's radius (max 200 km), so a venue 342 km from every
  centre is not served at all and contributes no occurrences. The exposure is
  the fail-open states: fence disabled, or a mispointed circle (above). Not
  changed here; owned by the follow-up.
- **The six-hour near-edge widening** (§4) adds at most one expanded day per
  recurring event between 00:00 and 06:00 local. Bounded, measured by
  `EVENTS_PROJECTED_OCCURRENCES` / `EVENTS_PROJECTION_BYTES`, and negligible
  against the 20 MB production Redis working set.
- **`events_known_cities_v1` lives only in Redis.** A flushed dataset loses it
  until the next cycle (≤ 2 min), which after §5 restores the COMPLETE
  configured vocabulary rather than only cities with live events. Downstream
  readers must fail open on an empty read.
- **The C1 user-visible correction still depends on an operator sweep.** The
  PR makes it possible; the gate above makes it sequenced; nothing in code can
  force it.

## Review findings — disposition

Every finding assigned to cs-server, verified against this worktree's real code
before acting.

| id | verdict | what changed |
|---|---|---|
| **F01** | **FIXED** (with one recorded deviation) | Verified: `event_occurrences.py:126` uses the calendar date; `redis_projection_service.py:496-529` prunes and deletes anything not regenerated. §4 expands from the nightlife day (`NIGHTLIFE_CUTOFF_HOUR = 6`, frozen and named in the contract) while anchoring the forward edge to the calendar date — the deviation from F01's literal `range(0, horizon_days+1)`, with its reason (horizon-edge flapping) recorded in §4. Three new projector-driven scenarios plus the explicit "no hand-seeded index" rule in the Test Plan. |
| **F02** | **FIXED** (my half), with one sub-claim corrected | Verified: `remember_city_slug` IS called on every occurrence write via `index_event_occurrence` (`redis_venue_dao.py:1442`), the key has no TTL and no delete path, so it is durable. Verified GAP: a configured city with zero occurrences is never remembered, which would make vibes_bot's B5 422 a quiet city. §5 remembers every configured fence slug every cycle; the contract states the key, type and lifecycle. F02's parenthetical that the index uses `STATE_CAPITALS` slugs is contradicted by the operator's own production enumeration (truncated slugs) — recorded in Evidence, and the pre-merge check records the real mapping. |
| **F09** | **FIXED** | Verified: the parser writes through `venue_address_backfill_service.py:128` → `venue_repository` → `RdsVenueStore.update_venue_address_components`, which has the `NULLIF` non-empty guard but no city comparison, and `venue_address_parser.py:290` builds the bairro with no city check (the `:294` plausibility guard is gated on `street is None`). The suppression MOVES from `map_address_components` to the shared write boundary, covering `google`, `parsed` and any future `operator` writer, with `fold_text` folding. Two new scenarios and store-level unit tests. |
| **F10** | **FIXED** (my half) | The sweep is restated as a named, numbered release gate that blocks the mobile 1.4.0 EAS dispatch, with the metric to watch and the per-venue regression check — and an explicit statement that it must ALSO appear in the wrapper coordination plan and the mobile pre-release list, since this repo cannot enforce it alone. |
| **F17** | **FIXED** | Verified: `venue_address_backfill_service.py:183` calls `self.venue_repository.list_address_backfill_candidates(after_id, effective_limit)` and `venue_repository.py:75-79` forwards positionally; the remaining-count read takes the same hop at `:208`/`:82-86`. §1 adds the repository hop for `mode`, and Observability adds `count_address_source_rows` at BOTH layers plus `tests/rds_fake.py`. |
| **F18** | **FIXED** | Verified: `app/config.py:891 events_projection_horizon_days = 21`, `event_occurrences.py:129 range(0, horizon_days + 1)` = 22 calendar days, which exactly explains the live census (09-06 → 09-27). Recorded in the contract with the explicit downstream consequence: the index can never hold a recurring occurrence more than 21 days out, so a 59-day window is sparse by construction. |
| **F29** | **FIXED** (my half) | The contract's absolute "never equals the city" is replaced with the honest, source-aware and time-aware wording, and vibes_bot is told explicitly to echo the qualified version. The write-time guard makes the forward-looking half true for ALL writers (F09), and `venue_address_neighborhood_equals_city` measures the un-repaired residue instead of assuming it away. |
| **F30** | **FIXED** (and extended) | Verified by inspection: `^[A-Za-z][A-Za-z0-9+.-]*:` matches `evenyx.com:` because `.` is inside the class, so rule 6 dropped a legitimate `host:port` link. Rule 6 now requires the pre-colon token to contain no `.`. **Additionally**, rule 8 as written would ALSO have rejected `evenyx.com:8080` (its last dot-separated label would be `com:8080`, not alphabetic) — so rule 8 now strips and validates a numeric port. Both the BDD scenario and the unit table carry `evenyx.com:8080/lote2`. |
| **F37** | **FIXED** | Verified: `_maybe_fetch_google_address` (`venue_address_backfill_service.py:82-87`) returns `no_place_id` and writes nothing when `vibe_attributes.google_place_id` is absent, so the whole C1 path is a no-op for such a venue. The probe moves into PRE-MERGE acceptance as a two-part read-only check (place_id present, then Place Details answers `Espinheiro`), with the named fallback (the deferred `operator` writer) decided in advance. |

**Rejected: none.** Every finding assigned to this repo reproduced against the
real code. Two sub-claims inside otherwise-correct findings did not survive
checking and are corrected in place rather than silently inherited: F02's
`STATE_CAPITALS`-slug parenthetical (contradicted by the production
enumeration, above) and revision 1's own stale line references for the parser
(`:302`/`:305` → `:290`/`:294`), which the reviewer had right.

## Open Questions
- None.
