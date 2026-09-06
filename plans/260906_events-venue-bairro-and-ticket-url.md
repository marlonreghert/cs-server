# Events venue bairro, ticket URL and nightlife-day projection — fix all three at the system of record

> **Revision 2 (2026-09-06)** — revised in response to the three-lens review of
> the Events UI v1 plan set (contract reconciliation, completeness, adversarial
> execution). Findings assigned to this repo: **F01, F02, F09, F10, F17, F18,
> F29, F30, F37**. Every one is dispositioned in
> [Review findings — disposition](#review-findings--disposition); F01 EXPANDS
> this plan's scope with a third defect (C3), and F02 hardens the city
> vocabulary vibes_bot is about to validate against.
>
> **Revision 3 (2026-09-06)** — a third review round, plus a **production
> correction measured directly** through this repo's own
> `RdsVenueStore.get_geo_fence()` over SSM (read-only). Findings assigned to
> this repo: **R08, R10, R11, R15**, every one dispositioned in the same table.
> The correction RETRACTS revision 2's attribution of the truncated city slugs
> and the mispointed `rio`/`porto` centres to `admin.geo_fence_city`: that table
> holds exactly ONE row, `recife`. The coordinate defect is real, but it lives
> in another table and the follow-up below is re-pointed rather than deleted.
> Everywhere this plan reasons about `city_slug`, the measured one-circle state
> is now stated outright — including what it means for this repo's own contract
> out and for anyone adding a second city.

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
- **Adding, removing or re-pointing an `admin.geo_fence_city` circle.**
  Production holds exactly ONE circle (`recife`, 40 km) — measured, see
  Evidence — and this plan neither adds a second nor touches the one that
  exists. Nothing here writes, validates or renames a fence row. The mispointed
  `rio` / `porto` coordinates are a real defect **in a different table** and are
  deferred to a named follow-up — see
  [Follow-up: the mispointed rio/porto centres](#follow-up-the-mispointed-rioporto-centres-not-this-plan).
- **Adding a second events city.** Out of scope, and sequenced explicitly in
  Contract out so that nobody adds a non-Recife crawl target first: with one
  circle and no distance cap, every venue on Earth resolves to `recife` today,
  so a São Paulo venue's events would be served inside Recife's feed.
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
### What production actually holds — measured, not inferred (revision 3)

Read-only, 2026-09-06, over SSM, through this repo's own
`RdsVenueStore.get_geo_fence()` and a `SCAN` of the events key families:

```
admin.geo_fence        enabled = False
admin.geo_fence_city   1 row:  recife | Recife | -8.0476 | -34.8770 | 40.0 km

events_index_v1:recife          61 occurrences   (the ONLY events_index_v1 key)
events_known_cities_v1 (SET)    {recife}
events_venue_v1:*               8 keys
event_occurrence_v1:*           61 payloads
```

`DEFAULT_GEO_FENCE` (`venue_eligibility.py:132-134`) is recife-only as well, so
the seeded fallback and the real table agree; the reading is the same whichever
path `get_geo_fence` takes.

**1. `city_slug` is a CONSTANT today, not a derivation.** `nearest_city_slug`
takes `min` over the circle list with no distance cap and no containment
requirement, and `min` over a one-element list is that element. **Every venue on
Earth therefore resolves to `city_slug = "recife"`.** A São Paulo venue's events
would be indexed into `events_index_v1:recife` and served inside Recife's feed —
wrong data in the wrong city's feed, which looks like a crawl success and which
nothing downstream can detect. That is the real prerequisite for expanding past
Recife, and it is why Contract out below carries an explicit second-city
sequence.

**2. The fence is not self-limiting today, because it is disabled.**
`serving.eligible_venue`'s geo term — migration `0019_venue_closure_signal`
(`:124-131`), the LATEST definition of the view, byte-identical to `0015`'s — is
explicitly fail-open:

```sql
AND ( fence.enabled IS NOT TRUE
      OR g.lat IS NULL OR g.lng IS NULL
      OR NOT EXISTS (SELECT 1 FROM admin.geo_fence_city)
      OR EXISTS (SELECT 1 FROM admin.geo_fence_city c WHERE <haversine> <= c.radius_km) )
```

With `admin.geo_fence.enabled = False` the first disjunct is true for every row,
so the fence imposes **no serving restriction at all** and the 40 km Recife
radius currently bounds nothing. This settles the question the correction
document left open, and it refutes revision 2's residual-risk wording ("with the
fence ENABLED this is mostly self-limiting") — corrected in Residual risks.

**3. Revision 2's attribution of truncated slugs to this table is WITHDRAWN.**
Revision 2 recorded an operator enumeration showing `belo`, `boa`, `campo`,
`sao`, `rio`, `porto` in `admin.geo_fence_city`, with `rio` and `porto` centred
~1,300 km away in Mato Grosso, and concluded that `validate_geo_fence` must
therefore have been bypassed. **That attribution was wrong.** Those rows came
from vibes_bot's **discovery points** (`GET /cities`, `POST /venues`), whose
slugs are derived as `point_id.split("-", 1)[0]` — a different table, in a
different repo, that this plan never reads. `admin.geo_fence_city` is exactly
consistent with migration `0015_geofence_city_circles`'s single seeded `recife`
row and with `validate_geo_fence` (`venue_eligibility.py:527-590`, which rejects
any slug outside `CAPITALS_BY_SLUG` and re-resolves coordinates from the
server-owned `STATE_CAPITALS` catalog). There is no evidence of a bypass, and
the bypass claim is retracted. The coordinate defect itself is real and stays a
follow-up — re-pointed, not deleted, below.

**4. So F02's dashed-capital mismatch does not exist in production today.** The
only fence slug is `recife`, which has no dash, so nothing can currently
mismatch vibes_bot's `list_cities()` vocabulary. The two vocabularies genuinely
ARE separate code paths, and the mismatch becomes real the moment a dashed
capital (`sao-paulo`, `belo-horizonte`) is added to the fence — which is exactly
what expanding past Recife requires. Validating against `events_known_cities_v1`
remains the right fix; it is a **pre-second-city requirement, not a v1
blocker**, and §5 below is what makes it safe.

**5. What that means for §5's honesty.** With one configured circle, "remember
every configured fence slug on every cycle" writes a slug that is already in the
set: **§5 is a no-op in production right now.** It is not wasted — it is the
prerequisite that puts a newly-configured city into the vocabulary *before* its
first occurrence is indexed — but this plan does not claim it changes a served
byte today.

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

- **`source="operator"` is exempt** — return immediately without touching the
  incoming `neighborhood` (R08, rationale below).
- Read the row's current city AND its provenance: one
  `SELECT city, city_source FROM venues.address WHERE venue_id=:venue_id`
  inside the same `engine.begin()` transaction the method already opens.
- Compute the **effective post-write city** — the city the row will actually
  hold once the statement commits. That is the incoming `city` when it is
  non-empty *and* it wins its own per-column precedence guard
  (`city_source IS NULL` or `rank(source) >= rank(city_source)`); otherwise the
  stored `city`. It is the same predicate `_field_clause("city")` already emits,
  evaluated in Python over the two values just read.
- Drop the incoming `neighborhood` (set it to `None`) when it is non-empty and
  `fold_text`-equals **either** the incoming `city` (when that is non-empty)
  **or** the effective post-write city. Two comparisons, not one.
  `NULLIF(:x,'')`/the existing per-field guard then makes it a no-op for that
  column; every other column writes normally.
- `app.utils.text_norm.fold_text` (`:18-25`) is the repo's existing
  accent-fold + casefold + punctuation-strip comparator, so "SÃO PAULO" and
  "sao paulo" compare equal.

Exactly one definition, so there is no drift between a pure mapper and a write
path. A None never clobbers under the precedence-guarded write, so this can only
ever prevent a bad write — it can never erase a good stored value, and it
deliberately does NOT repair rows already stored (see the contract's honest
wording and F29).

**Why two comparisons and not revision 2's one (R11).**
`update_venue_address_components` guards EVERY column independently
(`rds_venue_store.py:249-262`): the incoming `city` is written only when it wins
its OWN precedence check. Revision 2 compared the bairro against "the incoming
city when non-empty, else the stored city", so a write whose city LOSES could
still leave `neighborhood == the city the row keeps` — satisfying the letter of
revision 2's contract clause while being counted by the new
`venue_address_neighborhood_equals_city` gauge. **The gauge and the contract
clause must measure the same property**, and today they do not.

The reachable shape is created by this very plan. After the `upgrade` sweep a
row can hold `city_source = 'google'` while its `neighborhood_source` is still
`parsed` or NULL — precisely the rows §2 itself produces (Google answered a city
but its neighborhood was suppressed as equal-to-city, or the municipality
publishes no `sublocality*` at all). `apply_parser_fallback` then runs against
such a row — it is called from `enrich_venue` as well as from the backfill
(`venue_address_backfill_service.py:106`, "shared by the bulk job AND
enrich_venue") — carrying, say, `city="Igarassu"` and `neighborhood="Recife"`
against a stored `city="Recife"` sourced `google`. The neighborhood column
writes (`parsed` beats NULL, and ties with `parsed`); the city column does not
(`parsed` 1 < `google` 2). Result: `neighborhood == stored city`, exactly the
state the contract promises does not happen.

**Deliberate deviation from R11's literal prescription, recorded here.** R11
prescribed dropping when the bairro equals the incoming city **or the STORED
city**. That over-drops the mirror case, where the incoming city legitimately
WINS: a `google` write carrying `city="Igarassu"`, `neighborhood="Recife"`
against a stored `parsed` `city="Recife"` would lose a correct bairro even
though the row commits with `city="Igarassu"` and no equality at all. Comparing
against the EFFECTIVE post-write city is a strict refinement — it catches every
case R11 named and introduces no new false positive. Both directions get a
scenario.

**Why `operator` is exempt (R08).** A bairro whose name equals its city is not
always wrong. **Bairro do Recife** (Recife Antigo) is a real, central
neighbourhood *of the city of Recife*; the same shape exists elsewhere in
Brazil. This guard is a heuristic over machine-produced values, and the one
writer that can legitimately assert "yes, this bairro really is called Recife"
is a human. `operator` is also the top precedence rung (`_PROVENANCE_RANK`
operator 3) and the rung this plan's own **F37 contingency writes through** — a
guard that silently swallowed that write would break the named fallback the
Acceptance Criteria depend on. So `google` and `parsed` writes are guarded and
an `operator` write is trusted verbatim; the operator write is the documented
escape hatch.

**Named, accepted cost of the guard (R08).** For `google` and `parsed` writes, a
bairro that is legitimately named after its city IS dropped — the venue then
shows no bairro rather than a wrong one, which is the strictly safer failure of
the two, and it is repaired by one operator write. The affected population is
bounded and small: over the 488-address production sample measured for this plan
(Recife metro), **zero** parsed rows produce `neighborhood == city`, so the guard
drops nothing that exists today; the exposure is future `google` writes for
venues in Bairro do Recife and its analogues. Recorded in Residual risks with
that measurement, per R08's second option, in addition to the operator exemption.

`map_address_components` stays pure and unchanged.

### 3. `ticket_url` normalisation at projection (the C2 fix)

New pure module `app/services/event_ticket_url.py` exposing
`normalize_ticket_url(value: Optional[str]) -> Optional[str]`. No I/O, no
config, fully unit-testable. Applied in `RedisProjectionService.project_events`,
in the same spirit as the existing `flyer_url` substitution.

**Called once per SOURCE ROW, not once per occurrence (R15).** It goes in the
per-event `try` block next to the existing `flyer_url = flyer_urls.get(...)`
resolution (`redis_projection_service.py:420-421`), *before* the
`for occ in expand_occurrences(...)` loop, and the single normalised value is
reused for every occurrence that row expands into. It is a pure function of the
row, so calling it inside the loop would recompute an identical answer up to 22
times (23 while the nightlife day is rolled back) — and, worse, would scale its
counter by recurrence multiplicity. The metric consequence is spelled out in
Observability.

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
  `backfill_batch`; the two new gauges plus their freshness timestamp set at
  batch end alongside `VENUE_ADDRESS_BACKFILL_REMAINING` (`:208-210`), read
  through the REPOSITORY.
- `app/routers/admin_trigger_router.py` — `_run_address_components_backfill`
  (`:105-115`) reads `cfg.get("mode")`; the `JOB_REGISTRY` entry (`:200-214`)
  widens its `default_config` and description.
- `app/services/event_occurrences.py` — `NIGHTLIFE_CUTOFF_HOUR`,
  `nightlife_date`, the two-bound expansion, `__all__`.
- `app/services/redis_projection_service.py` — apply `normalize_ticket_url`
  ONCE PER SOURCE ROW in the per-event `try` block, before the occurrence loop
  (R15), and reuse the result for every occurrence; remember every configured
  fence slug each cycle; bump the two new counters.
- `app/dao/redis_venue_dao.py` — `remember_city_slug`'s docstring only (the
  set's meaning widens; its key, type and lifecycle do not).
- `app/metrics.py` — the five new series (the two address gauges, their
  shared freshness-timestamp gauge, the ticket-url counter and the nightlife
  rollback counter), every label value pre-initialised.
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

**`city_slug` as measured in production today (revision 3) — read this before
depending on the bullet above.**
- `admin.geo_fence_city` holds exactly ONE circle (`recife`), so
  `nearest_city_slug` returns `"recife"` for **every venue on Earth**, and
  `admin.geo_fence.enabled = False` means `serving.eligible_venue`'s geo term is
  fail-open and bounds nothing. `city_slug` is a constant, not a derivation.
- What vibes_bot and mobile may rely on for the Events UI v1 release train:
  `events_index_v1:recife` is the ONLY index key, `events_known_cities_v1` is
  the one-element set `{recife}`, and B5's validation vocabulary therefore
  accepts exactly one slug. A `city` param that is not `recife` is genuinely
  unserved today — that is correct behaviour, not a vocabulary bug, and B5's
  422 is the right answer for it.
- F02's dashed-capital vocabulary mismatch is **latent, not live**: the single
  fence slug has no dash. It becomes real the moment a dashed capital is
  configured. Treat vibes_bot's slug-vocabulary fix as a **pre-second-city**
  requirement rather than a v1 blocker.
- §5 ("remember every configured fence slug every cycle") is consequently a
  **no-op in production right now** — it re-writes a slug already in the set. It
  is the prerequisite for step 3 below, not a change to any served byte today.

**What this repo owes anyone adding a second city — the sequence, so nobody
adds a crawl target first.**
1. Add the geo-fence circle using the CANONICAL, non-truncated slug from the
   `STATE_CAPITALS` catalog (`sao-paulo`, never `sao`). `validate_geo_fence`
   (`venue_eligibility.py:527-590`) already enforces this for any write through
   the admin API and re-resolves the coordinates server-side.
2. Understand that the moment a second circle exists, **every** venue's
   `city_slug` is re-derived by nearest centre — no containment check, no
   distance cap. Venues silently move between city feeds, and a venue far from
   both centres still lands in one. Recompute `nearest_city_slug` over the whole
   catalog offline and record the flip list BEFORE the circle is written.
3. §5 guarantees the new slug reaches `events_known_cities_v1` within one
   2-minute cycle — before any occurrence is indexed under it — so vibes_bot's
   validation cannot 422 a freshly-configured, still-empty city.
4. Land vibes_bot's slug-vocabulary fix BEFORE step 1, not after: step 1 is what
   turns F02's latent mismatch into a live one.
5. Only then add non-Recife crawl targets. Adding them earlier produces events
   that are extracted, projected and served under `recife` — wrong data in the
   wrong city's feed, indistinguishable from a crawl success.
   Adding Recife crawl targets has none of these prerequisites.

**`venue_neighborhood` (honest wording — replaces revision 1's absolute
guarantee; F09/F29).**
- It is never the empty string (`NULLIF(:x,'')` at the write boundary).
- For every value **written after this change lands** by a `google` or `parsed`
  writer: it is never equal, accent- and case-insensitively, to the city that
  write asserts, **nor to the city the row holds after the write** (R11 —
  revision 2 promised only the first half, which the per-column precedence guard
  can violate).
- **`operator` writes are exempt (R08).** A human may deliberately store a
  bairro that is named after its city — Bairro do Recife is a real neighbourhood
  of Recife. So the guarantee downstream may state is "no MACHINE-written bairro
  equals its city", not "no bairro equals its city". The operator write is also
  the documented repair path when the guard drops a legitimate value.
- It is NOT retro-repaired: rows written before this change are untouched until
  something rewrites them. The `venue_address_neighborhood_equals_city` gauge
  below reports how many such rows exist **as of the last address backfill
  batch** (it is a batch-scoped snapshot with its own freshness stamp — R10), so
  the claim is measured rather than assumed, but it is not a live reading and it
  counts intentional operator exemptions too.
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
- New counter **`events_projection_ticket_url_total{outcome}`**, bumped **once
  per projected SOURCE ROW — not once per occurrence (R15)**:
  `absent` (nothing stored) | `passthrough` (already absolute http/https) |
  `scheme_added` (a scheme-less host was qualified) | `rejected` (stored, but
  not a usable URL — the operator's signal that extraction is emitting prose).
  All four outcomes pre-initialised at import, so a zero is a measured zero.
  `expand_occurrences` emits one occurrence per matching local day across the
  closed horizon (up to 22, 23 while the nightlife day is rolled back), so a
  per-occurrence bump would scale the counter by recurrence multiplicity and
  make ONE badly-extracted recurring row read as 22 rejections.
  **How to read it, stated so it is not oversold.** `project_events` rebuilds
  every payload on every cycle (`redis_projection_minutes = 2`), so each
  selected source row contributes one increment per cycle — ≈720/day per row at
  steady state. This counter is a **per-cycle census of the projected corpus**,
  not a count of distinct bad extractions. Use a rate or a ratio:
  `increase(events_projection_ticket_url_total{outcome="rejected"}[10m]) > 0`
  means "at least one currently-selected row is emitting prose", and `rejected`
  divided by the per-cycle total across all four outcomes is the share of the
  corpus affected. A raw counter value divided by nothing is meaningless.
- New gauge **`venue_address_source_rows{field,source}`** — the count of
  `venues.address` rows per structured field per provenance
  (`operator`/`google`/`parsed`/`none`), refreshed alongside
  `VENUE_ADDRESS_BACKFILL_REMAINING` at the end of every backfill batch.
  **BATCH-SCOPED, not continuous — see the staleness note below (R10).** This
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
  refreshed by the same batch-end call. **BATCH-SCOPED (R10).**
  This is what makes the contract's neighborhood-vs-city claim measured rather
  than asserted **as of the last backfill batch**, and it is how the operator
  sees the residual (pre-change) rows drain as the sweep rewrites them.
  Documented limitations: (a) the SQL-side comparison is case-insensitive but
  not accent-folding (no `unaccent` extension in this database), so it can
  undercount a purely accent-differing pair — the write-time guard itself DOES
  fold accents via `fold_text`; and (b) it counts intentional `operator`
  exemptions (R08) as well as defects, so its floor is the number of
  deliberately city-named bairros, and it should be read as "rows to inspect",
  never as "rows that are wrong".
- **Both address gauges above are BATCH-SCOPED snapshots, and that is now said
  out loud (R10).** They are recomputed only at the end of an
  `address_components_backfill` batch, and that job has **no scheduler entry at
  all**: `grep -rn address_components_backfill app/` finds it only in
  `app/services/job_lock.py:34` and `app/routers/admin_trigger_router.py`
  (`:105`, `:200`, `:214`) — a `JOB_REGISTRY` trigger runner with no
  `scheduler.add_job` registration (`grep -n address main.py` returns nothing,
  while every scheduled job in this repo is registered there). It runs ONLY when
  an operator POSTs `/admin/trigger/address_components_backfill/run`. Outside a
  sweep the series is a frozen snapshot, and a Prometheus gauge carries no
  staleness marker of its own — months after a sweep the operator would read a
  value that has not been recomputed since, indistinguishable from a live one.
  So a third series is added: **`venue_address_gauges_refreshed_timestamp_seconds`**
  (no labels), set to the Unix timestamp at the same moment the other two are
  refreshed. That is the standard Prometheus idiom for a snapshot gauge: the
  operator can see and alert on the reading's age
  (`time() - venue_address_gauges_refreshed_timestamp_seconds`), and a deploy
  where no batch has ever run reads `0` instead of looking like a measurement.
  Contract out is worded to match: the neighborhood claim is measured **as of
  the last backfill batch**, not continuously.
  **Why not refresh them from the 2-minute projection cycle instead** (R10's
  other option): that couples an address-quality metric to the serving
  projector's hot path and adds an RDS `GROUP BY` to the job whose preparation
  read is deliberately all-or-nothing and aborts the whole cycle on failure. The
  operational use of both gauges is watching the upgrade sweep drain — which is
  exactly when batches ARE running — and the freshness stamp covers the rest of
  the time at the cost of one series.
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
- **Count a recurring event's ticket url once, not once per occurrence**
  (R15) — a "todo dia" event expanding to many occurrences bumps the outcome
  exactly once per cycle, which is the assertion the existing non-recurring
  fixtures cannot make.
- **Count the ticket url again on the next projection cycle** (R15) — pins the
  per-cycle-census semantics the Observability section documents, so nobody
  later reads the raw counter as a count of distinct bad extractions.

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
- **Trust an operator's bairro that is named after its city** (R08) — an
  `operator` write of `neighborhood="Recife"` against a row whose city is
  "Recife" IS stored, sourced `operator`. This is the equal-and-CORRECT case,
  which revision 2 had no scenario for at all.
- **Drop a bairro that equals the city the row will keep, even when the write's
  own city is different** (R11) — a `parsed` write carrying `city="Igarassu"`
  and `neighborhood="Recife"` against a row whose `city` is "Recife" sourced
  `google`: the neighborhood is dropped and the row still holds
  `city="Recife"` sourced `google`.
- **Keep a bairro when the write's own city wins and differs from it** (R11,
  the deviation guard) — a `google` write carrying `city="Igarassu"` and
  `neighborhood="Recife"` against a row whose `city` is "Recife" sourced
  `parsed`: the city becomes "Igarassu" and the bairro "Recife" is STORED,
  because the row does not end up with `neighborhood == city`. This is the
  false positive R11's literal "incoming OR stored" rule would have caused.
- **Write the other address fields even when the bairro is dropped** — street
  still lands; the suppression is per-column, not per-write.
- **Make no address lookup at all while the free-tier switch is off.**
- **Serve the corrected bairro on the next projection cycle.**
- **Report the address provenance distribution after a batch.**
- **Report how many stored bairros are still just the city name** — the
  `venue_address_neighborhood_equals_city` gauge, which is what makes the
  contract's wording measured rather than asserted.
- **Report no refresh timestamp before any batch has ever run** and **Stamp
  when the address gauges were last refreshed** (R10) — before any batch,
  `venue_address_gauges_refreshed_timestamp_seconds` is 0; after one it carries
  that batch's time, so a never-refreshed snapshot can never be mistaken for a
  live reading.

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
  unaffected when the neighborhood is dropped. Plus the revision-3 cases:
  the `operator` exemption (R08); the effective-post-write-city comparison in
  both directions (R11) — dropped when the incoming city LOSES and the kept
  city equals the bairro, stored when the incoming city WINS and differs from
  it; and a `parsed` write against a row with `city_source='google'` and
  `neighborhood_source` NULL, which is the exact shape this plan's own upgrade
  sweep creates.
- `tests/test_redis_projection_events.py` (or the existing projection test
  module) — `normalize_ticket_url` is called once per source row, not once per
  occurrence (R15): a recurring row expanding to N occurrences bumps
  `events_projection_ticket_url_total` by exactly 1, and every one of its N
  payloads carries the same normalised value.
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
  `venue_address_gauges_refreshed_timestamp_seconds`,
  `events_projection_ticket_url_total{outcome}` and
  `events_projection_nightlife_rollback_total` are exported with every label
  value pre-initialised.
- A recurring event expanding to N occurrences bumps
  `events_projection_ticket_url_total` by exactly **1** per cycle, not N (R15).
- An `operator` write of a bairro equal to its city is STORED; the same write
  from `google` or `parsed` is dropped (R08).
- A write whose incoming city loses its precedence check cannot leave the row
  with `neighborhood == city`, and a write whose incoming city wins keeps a
  bairro that only matched the OLD city (R11).
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
- **F02 — re-record the real mapping at merge time:** enumerate
  `admin.geo_fence_city` (slug, name, lat, lng, radius_km) and vibes_bot's
  `GET /cities`, and paste both lists into the PR description alongside
  `SMEMBERS events_known_cities_v1` from a real cycle. The measured answer as of
  2026-09-06 is already in Evidence — one circle, `recife`; one known city,
  `recife` — so this is a re-confirmation that nothing changed under the plan
  between writing and merging, not an open question. Read-only; no write. If the
  enumeration comes back with more than one circle, STOP and re-read the
  second-city sequence in Contract out before merging: vibes_bot's
  slug-vocabulary fix has to precede that circle, not follow it.

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

## Follow-up: the mispointed rio/porto centres (NOT this plan)

**Recorded so it cannot be lost. The defect is real and evidence-backed; what
changed in revision 3 is its ATTRIBUTION, which was wrong.**

### The correction (revision 3)

Revision 2 recorded this follow-up as a repair to production
`admin.geo_fence_city` — truncated slugs (`belo`, `boa`, `campo`, `sao`, `rio`,
`porto`) and two circles centred ~1,300 km away in Mato Grosso (`rio` at
`-16.4409, -55.4986`, `porto` at `-19.3983, -57.5608`) — and concluded that,
since `validate_geo_fence` (`venue_eligibility.py:527-590`) rejects any slug
outside `CAPITALS_BY_SLUG` and re-resolves coordinates from the server-owned
`STATE_CAPITALS` catalog, those rows must have been inserted out-of-band,
bypassing validation.

**That attribution is wrong, and the bypass conclusion is WITHDRAWN.** Measured
directly on 2026-09-06 over SSM through this repo's own
`RdsVenueStore.get_geo_fence()`: production `admin.geo_fence_city` holds
**exactly one row — `recife` (-8.0476, -34.8770, 40 km)** — exactly what
migration `0015_geofence_city_circles` seeds, and exactly what
`validate_geo_fence` would allow. There is no truncated slug in that table, no
Mato Grosso centre in it, and no evidence that anything bypassed validation.

The truncated slugs and the Mato Grosso coordinates came from **vibes_bot's
discovery points** — the rows behind `GET /cities` and `POST /venues`, whose
slugs are derived as `point_id.split("-", 1)[0]`, which is what produces `sao`
from `sao-paulo` and `rio` from a `rio-…` point id. That is a **different table,
in a different repo**, which this plan neither reads nor writes.

### What survives, and is still a real defect

Two discovery points named after Rio de Janeiro and Porto Alegre carry
coordinates roughly 1,300 km from those cities, in Mato Grosso. Nothing in
revision 3 refutes the coordinates themselves — only where they live. So the
follow-up stands, with one step added at the front:

**Step 0 (new, and mandatory): identify the table that actually holds these
rows.** It is not `admin.geo_fence_city`. The measurement that produced the
numbers was `GET /cities` + `POST /venues` against vibes_bot, so the owning
store is on the vibes_bot side (or a shared store vibes_bot reads); the
follow-up must name the table, the repo, the writer that created the rows and
the validation — if any — that writer passes through, and must re-verify the two
coordinate pairs against that store directly before proposing any change.

**The operator's approval to fix `rio`/`porto` was given on the mis-attributed
premise** (that these were geo-fence circles re-partitioning cs-server's serving
set). It must be re-confirmed once step 0 identifies the real owner, because the
blast radius of the change depends entirely on which table it is.

### Why it stays a separate plan

1. **Unknown blast radius until step 0 lands.** If the rows are discovery
   points, re-pointing them changes where the crawl/add pipelines look, not
   which venues `serving.eligible_venue` returns. If they turn out to live
   somewhere this repo reads, the radius is larger. Either way it is not
   knowable from here, and this plan changes two field values and one date
   bound.
2. **Any slug change is cross-repo in a release train that is introducing city
   validation.** Renaming a slug anything downstream keys on, in the same train
   that adds vibes_bot's B5 vocabulary check, is how a 422-on-every-city outage
   happens.
3. **Nothing is lost by waiting.** cs-server's projection re-asserts every 2
   minutes, so a correction takes effect on the next cycle whenever it lands.

### What the follow-up plan must show (do not let it skip these)

- Step 0's answer: the table, the repo, the writer, and a direct read of the two
  coordinate pairs from that store.
- A read-only enumeration of whatever table it is, BEFORE the change.
- If (and only if) the change touches `admin.geo_fence_city`: a per-venue
  `nearest_city_slug` computation over the whole catalog under both the current
  and the corrected circle sets — the exact list of venues whose `city_slug`
  would FLIP, produced before anything is written — plus the servability delta
  from `serving.eligible_venue` under both fences (counts and venue ids gained
  and lost), with zero unexplained losses or an explicit operator decision per
  lost venue. Note that with `admin.geo_fence.enabled = False` today the geo
  term is fail-open, so a fence change alters servability only if the flag is
  also flipped — and flipping it is its own decision with its own delta.
- What happens to any orphaned `events_index_v1:<old-slug>` ZSET: it is pruned
  by the projector's own city prune only because `events_known_cities_v1` keeps
  remembering the old slug (this plan's §5 makes that stronger, not weaker) —
  the follow-up must confirm the old key drains to empty rather than being left
  with stale members, and must NOT delete the known-cities member.
- Coordination with vibes_bot: any slug rename is announced before it lands, and
  the mapping is re-recorded in both plans.
- Whether `nearest_city_slug` should gain a distance limit at the same time —
  and if so, what happens to a venue beyond the limit (today it would raise, be
  counted as a per-event error, and DROP the event from serving; a follow-up
  wanting a limit must add a "no city" disposition rather than an exception).
- Whether `admin.geo_fence.enabled` should be `True` at all. It reads `False` in
  production; `nearest_city_slug` ignores the flag by design, but
  `serving.eligible_venue`'s geo term is fail-open on it, so today the fence
  restricts nothing. That is a separate, deliberate decision — not something to
  flip as a side effect of a coordinate repair.

## Residual risks

- **A parser-sourced bairro can still be a venue or complex name** for any
  venue Google cannot answer (93/488 measured unanswered). The contract wording
  above says so explicitly rather than promising otherwise; vibes_bot must echo
  the qualified wording (F29).
- **Rows already stored with `neighborhood == city` are not repaired** by the
  write-time guard. `venue_address_neighborhood_equals_city` measures how many
  exist; the upgrade sweep rewrites them only where Google answers.
- **`nearest_city_slug` has no distance limit, and in production it has nothing
  to choose between** (corrected in revision 3). `city_slug` is "nearest
  configured circle centre", not "the city this venue is in" — and with exactly
  one circle configured, it is the constant `"recife"` for every venue. Revision
  2 called this "mostly self-limiting with the fence ENABLED"; **the fence is
  DISABLED in production** (`admin.geo_fence.enabled = False`), and
  `serving.eligible_venue`'s geo term is fail-open on that flag (migration
  `0019`, `:124-131`), so the 40 km radius bounds nothing and no distance
  self-limits anything. Today the risk is latent because there is only one city
  to be wrong about; it becomes live the moment a second circle is added, which
  is why Contract out carries the second-city sequence. Not changed here; owned
  by the follow-up.
- **A bairro legitimately named after its city is dropped for `google` and
  `parsed` writes** (R08, accepted). Bairro do Recife is the named real-world
  case. Measured exposure today: **zero** of the 488 production addresses in
  this plan's sample parse to `neighborhood == city`, so the guard drops nothing
  that currently exists; the exposure is future `google` writes for venues in
  such a bairro. The failure is safe (no bairro shown rather than a wrong one),
  it is visible in `venue_address_neighborhood_equals_city` only after an
  operator repairs it, and the repair is one `operator` write, which the guard
  exempts.
- **Both address gauges are batch-scoped snapshots** (R10). Outside an
  operator-triggered `address_components_backfill` sweep they do not move;
  `venue_address_gauges_refreshed_timestamp_seconds` is what makes that visible.
  An operator who reads either gauge without checking the stamp can mistake a
  months-old snapshot for a live measurement.
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

### Revisions 1–2 (F-series)

| id | verdict | what changed |
|---|---|---|
| **F01** | **FIXED** (with one recorded deviation) | Verified: `event_occurrences.py:126` uses the calendar date; `redis_projection_service.py:496-529` prunes and deletes anything not regenerated. §4 expands from the nightlife day (`NIGHTLIFE_CUTOFF_HOUR = 6`, frozen and named in the contract) while anchoring the forward edge to the calendar date — the deviation from F01's literal `range(0, horizon_days+1)`, with its reason (horizon-edge flapping) recorded in §4. Three new projector-driven scenarios plus the explicit "no hand-seeded index" rule in the Test Plan. |
| **F02** | **FIXED** (my half), with one sub-claim corrected | Verified: `remember_city_slug` IS called on every occurrence write via `index_event_occurrence` (`redis_venue_dao.py:1442`), the key has no TTL and no delete path, so it is durable. Verified GAP: a configured city with zero occurrences is never remembered, which would make vibes_bot's B5 422 a quiet city. §5 remembers every configured fence slug every cycle; the contract states the key, type and lifecycle. F02's parenthetical that the index uses `STATE_CAPITALS` slugs is neither confirmed nor contradicted by production: **revision 3 measured `admin.geo_fence_city` directly and it holds exactly ONE row, `recife`** — the truncated slugs revision 2 cited were vibes_bot discovery points, not fence rows (see Evidence and the follow-up). So the dashed-capital mismatch is LATENT, not live, and §5 is a no-op today that becomes load-bearing at the second city. |
| **F09** | **FIXED** | Verified: the parser writes through `venue_address_backfill_service.py:128` → `venue_repository` → `RdsVenueStore.update_venue_address_components`, which has the `NULLIF` non-empty guard but no city comparison, and `venue_address_parser.py:290` builds the bairro with no city check (the `:294` plausibility guard is gated on `street is None`). The suppression MOVES from `map_address_components` to the shared write boundary, covering `google`, `parsed` and any future `operator` writer, with `fold_text` folding. Two new scenarios and store-level unit tests. |
| **F10** | **FIXED** (my half) | The sweep is restated as a named, numbered release gate that blocks the mobile 1.4.0 EAS dispatch, with the metric to watch and the per-venue regression check — and an explicit statement that it must ALSO appear in the wrapper coordination plan and the mobile pre-release list, since this repo cannot enforce it alone. |
| **F17** | **FIXED** | Verified: `venue_address_backfill_service.py:183` calls `self.venue_repository.list_address_backfill_candidates(after_id, effective_limit)` and `venue_repository.py:75-79` forwards positionally; the remaining-count read takes the same hop at `:208`/`:82-86`. §1 adds the repository hop for `mode`, and Observability adds `count_address_source_rows` at BOTH layers plus `tests/rds_fake.py`. |
| **F18** | **FIXED** | Verified: `app/config.py:891 events_projection_horizon_days = 21`, `event_occurrences.py:129 range(0, horizon_days + 1)` = 22 calendar days, which exactly explains the live census (09-06 → 09-27). Recorded in the contract with the explicit downstream consequence: the index can never hold a recurring occurrence more than 21 days out, so a 59-day window is sparse by construction. |
| **F29** | **FIXED** (my half) | The contract's absolute "never equals the city" is replaced with the honest, source-aware and time-aware wording, and vibes_bot is told explicitly to echo the qualified version. The write-time guard makes the forward-looking half true for ALL writers (F09), and `venue_address_neighborhood_equals_city` measures the un-repaired residue instead of assuming it away. |
| **F30** | **FIXED** (and extended) | Verified by inspection: `^[A-Za-z][A-Za-z0-9+.-]*:` matches `evenyx.com:` because `.` is inside the class, so rule 6 dropped a legitimate `host:port` link. Rule 6 now requires the pre-colon token to contain no `.`. **Additionally**, rule 8 as written would ALSO have rejected `evenyx.com:8080` (its last dot-separated label would be `com:8080`, not alphabetic) — so rule 8 now strips and validates a numeric port. Both the BDD scenario and the unit table carry `evenyx.com:8080/lote2`. |
| **F37** | **FIXED** | Verified: `_maybe_fetch_google_address` (`venue_address_backfill_service.py:82-87`) returns `no_place_id` and writes nothing when `vibe_attributes.google_place_id` is absent, so the whole C1 path is a no-op for such a venue. The probe moves into PRE-MERGE acceptance as a two-part read-only check (place_id present, then Place Details answers `Espinheiro`), with the named fallback (the deferred `operator` writer) decided in advance. |

### Revision 3 (R08, R10, R11, R15 + the production correction)

| id | verdict | what changed |
|---|---|---|
| **PROD CORRECTION** | **APPLIED** | Measured over SSM through `RdsVenueStore.get_geo_fence()`: `admin.geo_fence_city` holds exactly ONE row (`recife`, -8.0476/-34.8770, 40 km) and `admin.geo_fence.enabled = False`. Revision 2's attribution of truncated slugs and the Mato Grosso `rio`/`porto` centres to that table is **withdrawn**, along with its "`validate_geo_fence` must have been bypassed" conclusion — those rows are vibes_bot **discovery points** (`GET /cities`, `POST /venues`, slugs derived as `point_id.split("-",1)[0]`). The coordinate defect is kept as a real follow-up with a new mandatory step 0: identify the table that actually holds it. Everywhere the plan reasons about `city_slug`, the one-circle state is now stated: `min` over one element means **every venue resolves to `recife`**, so `city_slug` is a constant, `events_index_v1:recife` is the only index key, F02's dashed-slug mismatch is latent rather than live, and §5 is a no-op today. Contract out gains an explicit second-city sequence. Independently verified and added: `serving.eligible_venue`'s geo term is fail-open on `fence.enabled` (migration `0019:124-131`), so the disabled fence restricts **nothing** — which refutes revision 2's "with the fence ENABLED this is mostly self-limiting" residual risk and closes the question the correction document left open. |
| **R08** | **FIXED** (both options taken) | Verified: `_PROVENANCE_RANK` puts `operator` at 3, `update_venue_address_components` has no source exemption today, and the plan's own F37 contingency writes through `source="operator"`, so an unconditional guard would swallow the named fallback. Bairro do Recife is a real neighbourhood named after its city. §2 now exempts `source="operator"` outright, AND the `google`/`parsed` false positive is recorded in Residual risks with its measured size (**zero** of the 488 sampled production addresses parse to `neighborhood == city`, so nothing that exists today is dropped). Two scenarios added, including the equal-and-CORRECT case revision 2 had no coverage for. Contract out and the gauge's wording both say "no MACHINE-written bairro", not "no bairro". |
| **R10** | **FIXED** (option b, plus a staleness stamp) | Verified: `grep -rn address_components_backfill app/` finds it only in `job_lock.py:34` and `admin_trigger_router.py:105,200,214` — a `JOB_REGISTRY` runner with **no** `scheduler.add_job` registration (`grep -n address main.py` returns nothing, and every scheduled job in this repo is registered in `main.py`). Both gauges are therefore operator-trigger-scoped. Observability and Contract out now say BATCH-SCOPED plainly, name the trigger, and stop calling the neighborhood claim continuously measured; a third series `venue_address_gauges_refreshed_timestamp_seconds` makes the snapshot's age visible and alertable, which is the actual defect ("a Prometheus gauge carries no staleness marker"). Refreshing from the 2-minute projection cycle was considered and rejected in writing: it would couple an address-quality metric to the serving projector's all-or-nothing preparation read for no gain during the sweep, which is when the gauges are actually used. |
| **R11** | **FIXED** (with a recorded refinement) | Verified: `update_venue_address_components` (`rds_venue_store.py:249-262`) guards every column independently, so the incoming `city` can lose while the `neighborhood` writes. Verified the reachable shape is created BY THIS PLAN: after the upgrade sweep a row can hold `city_source='google'` with `neighborhood_source` NULL/`parsed`, and `apply_parser_fallback` runs against it from `enrich_venue` as well as from the backfill (`venue_address_backfill_service.py:106`). §2 now compares the bairro against the incoming city AND the **effective post-write city** (the city the row will actually hold). **Refinement of R11's literal prescription, recorded in §2:** R11 said "incoming OR stored", which over-drops the mirror case where the incoming city legitimately WINS — comparing against the effective city catches everything R11 named and invents no new false positive. Both directions get a scenario, plus store-level unit tests. |
| **R15** | **FIXED** (bump per source row, and the semantics documented) | Verified: the payload build sits inside `for occ in expand_occurrences(...)` (`redis_projection_service.py:~423-450`), so a bump there is per occurrence; `expand_occurrences` emits up to 22 (23 while rolled back) per source row and `project_events` rebuilds every payload every `redis_projection_minutes = 2`. §3 moves the call — and the bump — into the per-event `try` block before the loop, reusing one normalised value for every occurrence (it is a pure function of the row, so the per-occurrence call was also redundant work). Observability additionally states the residual honestly: even per row, the counter is a **per-cycle census**, ≈720 increments/day per row, to be read as a rate or a ratio and never as a count of distinct bad extractions. Two scenarios pin both halves. |

**Rejected: none.** Every finding assigned to this repo reproduced against the
real code. Three sub-claims inside otherwise-correct findings did not survive
checking and are corrected in place rather than silently inherited: F02's
`STATE_CAPITALS`-slug parenthetical (neither half of the revision-2
contradiction was true of `admin.geo_fence_city`, which holds one `recife` row —
see the production correction above); revision 1's own stale line references for
the parser (`:302`/`:305` → `:290`/`:294`), which the reviewer had right; and
R11's literal "incoming OR stored city" rule, refined to "incoming OR effective
post-write city" in §2 because the literal form drops a correct bairro whenever
the incoming city legitimately wins.

**One claim this plan itself made and now retracts:** revision 2's assertion
that production `admin.geo_fence_city` contains truncated slugs and mispointed
`rio`/`porto` circles, and that `validate_geo_fence` must therefore have been
bypassed. Measured false. The rows are vibes_bot discovery points; the follow-up
is re-pointed, not deleted.

## Open Questions
- None.
