# Events venue bairro and ticket URL — fix both at the system of record

## Branch
fix/events-venue-bairro-and-ticket-url

## Goal
Two defects visible on every served event card, both fixed in cs-server so the
2-minute projector re-asserts the corrected values without a Redis migration
and without either downstream repo changing a field:

- **C1 — `venue_neighborhood` carries a venue/complex name instead of a
  bairro** for 7 of 53 live occurrences (13.2%): Downtown Beer Garden serves
  `"Quintal Espinheiro"` where the real bairro is `"Espinheiro"`.
- **C2 — `ticket_url` is projected with no URL scheme.** The single production
  value is the bare string `"evenyx.com"`, which `Linking.openURL` rejects.

Both fixes must leave the events projection's field set byte-for-byte
identical — only the VALUES change.

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
  data path.
- Enabling `address_backfill_geocoding_enabled` or running the sweep in
  production. This plan ships the reachable path; turning the switch on and
  driving the batches is an operator-gated rollout step, listed under
  Acceptance Criteria as a post-merge action.
- Removing the confirmed-dead `geocode_by_place_id` / `GoogleGeocodingError` /
  `GOOGLE_GEOCODING_API_BASE` (kept deliberately by
  `plans/260906_address-components-via-place-details.md` correction (B)).

## Evidence

### The two defects, observed live (2026-09-06, read-only)

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
   on the exact stored string: UF `PE` → tail →`clean_tail` strips the trailing
   `-` → `split_street_and_blob` takes the LAST standalone dash, so
   `street = "Rua Quarenta e Oito, 490"` and `blob = "Quintal Espinheiro
   Recife"`. No comma in the blob → Format A: the 1-word suffix `"Recife"` hits
   the city vocabulary, and **every remaining leading token becomes the bairro**
   (`bairro_candidate = " ".join(leading)`, `:302`). Step 8's plausibility guard
   is gated on `street is None` (`:305`) and street is not None here, so it
   never runs — and it would have PASSED anyway (no digit, 2 words ≤ 3, neither
   "quintal" nor "espinheiro" in `STOPLIST_BAIRRO_WORDS`).

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
   `list_address_backfill_candidates` (`rds_venue_store.py:277-299`) selects
   only rows where `street IS NULL OR neighborhood IS NULL OR city IS NULL OR
   postal_code IS NULL`. Every row is now non-null. Flipping the switch on and
   resetting `address_backfill_cursor` today selects **zero rows** and changes
   **nothing**. The precedence rule itself is correct and would win
   (`_PROVENANCE_RANK` google 2 ≥ parsed 1, `rds_venue_store.py:206`); it simply
   never gets a candidate. This is the defect.

   The daily cron cannot rescue it either: `enrich_all_venues`
   (`google_places_enrichment_service.py:612-615`) skips any venue that already
   has a `vibe_attributes` row, running only the `businessStatus`-only recheck —
   never `addressComponents`.

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

### A latent second pollution class the fix must not introduce

`venue_address_components._NEIGHBORHOOD_TYPES` ends with
`administrative_area_level_2` and `_CITY_TYPES` BEGINS with it. For any
municipality that publishes no `sublocality*` component, `map_address_components`
therefore returns `neighborhood == city` — and the card would render the city
name as the bairro ("Igarassu · 2,3 km"). Harmless today because Google has
never written a component; the moment the sweep runs catalogue-wide it becomes
real, on exactly the field this plan exists to clean up.

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
(`:420`) replaces the source URL with the CDN one. Normalising `ticket_url` at
the same place is the established shape, not a new one.

## Current Behavior

- `venues.address.neighborhood` is 100% populated, 100% `parsed`-sourced. One
  catalog row in 395 holds a venue-complex name; that row backs 13.2% of served
  event occurrences because it is a high-frequency events venue.
- The address backfill job selects only rows with a NULL structured column, so
  it now selects nothing and the Google rung is dead code in production.
- `map_address_components` can return `neighborhood == city` for a municipality
  with no sublocality component.
- `ticket_url` reaches the serving projection exactly as the extraction model
  wrote it, including scheme-less hosts and non-URL prose.

## Desired Behavior

**Bairro.** The address backfill job accepts a selection **mode**. In `upgrade`
mode it selects rows whose structured columns are filled but provenance-ranked
below `google`, so the already-shipped Place Details rung can correct them.
Google's answer replaces a `parsed` neighborhood and stamps
`neighborhood_source = 'google'`. An `operator` value is never replaced. A
Google response that answers nothing leaves the stored value exactly as it is —
never nulled, never poisoned. A Google neighborhood identical to the same
response's city is suppressed to None rather than written as a bairro. The
events projection re-asserts the corrected bairro on its next 2-minute cycle
with no code change of its own.

**Ticket URL.** The events projection normalises `ticket_url` into a
scheme-qualified absolute `http`/`https` URL, or `null` when the stored value is
not a usable web URL. RDS keeps the verbatim extraction. The correction
re-asserts every cycle, so the existing corpus self-heals on deploy with no
migration and no re-extraction.

**Neither change alters the projection's field set.**

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

`InMemoryRdsVenueStore` (`tests/rds_fake.py`) mirrors the new parameter with
the same semantics — it is the contract this store's docstring already promises
to follow.

`VenueAddressBackfillService.backfill_batch` gains `mode` and passes it
through. `_run_address_components_backfill` reads `cfg.get("mode")`, defaulting
to `"fill"`, mirroring `instagram_profile_photos`' existing
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

### 2. Suppress a Google neighborhood that is just the city

In `venue_address_components.map_address_components`, after both fields are
resolved, drop `neighborhood` to None when it fold-compares equal to the same
response's `city` (`app.utils.text_norm.fold_text`, the module already used for
this kind of comparison across the repo). Fixes the
`administrative_area_level_2`-shared-rung degeneracy at the one place both
writers (`enrich_venue`'s opportunistic path and the backfill) go through. A
None never clobbers under the precedence-guarded write, so this can only ever
prevent a bad write, never erase a good stored value.

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
6. Begins with any other scheme (`^[A-Za-z][A-Za-z0-9+.\-]*:`) — `mailto:`,
   `tel:`, `whatsapp:`, `javascript:`, `data:` — → `None`. The field is typed
   as a ticketing web link on both downstream repos; a non-web scheme is safer
   as absent than as an unopenable or dangerous link.
7. A leading `//` is stripped, then the value is treated as scheme-less.
8. Scheme-less: take the authority = text up to the first `/`, `?` or `#`.
   Accept only when it contains no `@`, contains at least one `.`, and its last
   dot-separated label is ≥2 characters and entirely alphabetic. On acceptance
   return `"https://" + value`, preserving path, query and fragment exactly.
   `https` and not `http`: a purchase page served over plaintext draws a browser
   interstitial, and every real ticketing host redirects http→https anyway.
9. Anything else → `None`.

Verified against the only production value: `"evenyx.com"` → `"https://evenyx.com"`.

RDS is deliberately NOT rewritten. The extraction client's "records exactly what
the model said" contract stays intact, the admin console keeps showing the
source value for auditing, and the serving correction re-asserts on every cycle
— so the fix reaches production the moment the code deploys, with no migration
and no backfill.

## Data, Config, And API Impact

- **Migration:** none. No schema change.
- **Redis / projection contract:** no field added, removed, renamed or retyped.
  `EventOccurrence` is untouched. Only the VALUES of `venue_neighborhood` and
  `ticket_url` change.
- **RDS writes:** `venues.address.{neighborhood,street,city,postal_code}` and
  their `*_source` columns, only through the existing precedence-guarded
  `update_venue_address_components`. `events.post_item.ticket_url` is not
  written at all.
- **Config:** no new setting and no new admin-config key. `mode` is a per-run
  trigger config value, defaulting to today's behaviour.
- **Feature flags:** none added. `address_backfill_geocoding_enabled` (existing,
  default OFF) still gates every paid call. `events_projection_enabled` is
  unchanged.
- **Spend:** a full `upgrade` sweep is ≈3,452 Place Details Essentials calls,
  one-time, inside the 10,000/month free allowance. $0/month. No new billed
  surface; the paid call is the one PR #223 already shipped.
- **API:** `GET /admin/jobs` shows the widened `default_config` and description
  for `address_components_backfill`. No other endpoint changes.

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
  sweep progress and prove it finished.
- An unknown backfill `mode` raises `ValueError` at the DAO boundary and is
  surfaced by the existing trigger error path — never a silent fallback to the
  wrong population.
- Existing `venue_geocoding_requests_total` / `venue_address_components_total`
  labels are unchanged, so the PR #223 dashboards keep working.

## Test Plan

Feature file: `tests/bdd/persistence/events-venue-bairro-and-ticket-url.feature`

Scenarios:
- **Qualify a scheme-less ticket host** — a stored `"evenyx.com"` is projected
  as `"https://evenyx.com"` and counted `scheme_added`.
- **Preserve path, query and fragment when adding the scheme** — the only part
  added is the scheme.
- **Leave an already-absolute ticket URL untouched** — an `https://` value is
  projected byte-for-byte and counted `passthrough`; an `http://` value is not
  upgraded.
- **Project no ticket URL for caption prose** — "ingressos na portaria" is
  projected as null and counted `rejected`.
- **Project no ticket URL for a non-web scheme** — a `mailto:`/`javascript:`
  value is projected as null.
- **Project no ticket URL when none was extracted** — null in, null out,
  counted `absent`.
- **Leave the stored ticket URL verbatim in RDS** — normalisation is a serving
  concern; the event row still holds what extraction wrote.
- **Re-assert the normalised ticket URL on the next cycle** — no re-extraction
  and no RDS write are needed for the correction to persist.
- **Select a fully-populated parsed row in upgrade mode** — a row with all four
  columns filled and every `*_source` = `parsed` IS a candidate.
- **Do not select a fully-populated parsed row in the default mode** — the
  shipped `fill` predicate is unchanged; the same row is NOT a candidate.
- **Replace a parsed bairro with Google's answer** — the venue-complex name is
  replaced by the bairro and `neighborhood_source` becomes `google`.
- **Never replace an operator bairro** — an `operator`-sourced value survives an
  upgrade sweep untouched.
- **Never null a stored bairro when Google answers nothing** — an empty
  address-components response leaves the parsed value exactly as it was.
- **Suppress a Google neighborhood that is only the city name** — a response
  whose sole neighborhood rung is `administrative_area_level_2` writes no
  neighborhood rather than writing the city.
- **Make zero Place Details calls while the free-tier switch is off** — an
  upgrade-mode batch with the switch off performs no address lookup at all.
- **Serve the corrected bairro on the next projection cycle** — after the RDS
  correction the events projection carries the bairro with no projection code
  change.
- **Report the address provenance distribution after a batch** — the
  per-field/per-source gauge reflects the rows actually stored.

Pytest unit tests:
- `tests/test_event_ticket_url.py` — a table-driven pass over
  `normalize_ticket_url`: bare host, `www.` host, host with path/query/fragment,
  absolute http, absolute https, uppercase `HTTPS://`, protocol-relative `//`,
  `mailto:`, `tel:`, `javascript:`, `data:`, an email-shaped value, a bare word
  with no dot, a trailing-period link, a whitespace-containing sentence, an
  over-length string, `""`, `"   "`, `None`, and a non-string. Includes the
  literal production value `"evenyx.com"`.
- `tests/test_venue_address_backfill_service.py` — extend for the `mode`
  pass-through and for "upgrade mode with the switch off makes no client call"
  (guard the assertion against vacuity the way
  `plans/260906_address-components-via-place-details.md` correction (C) requires:
  the spy must be on the method production code actually calls,
  `fetch_address_components`).
- `tests/test_venue_address_components.py` — the neighborhood-equals-city
  suppression, including the fold-compare (accent/case) case, and proof that a
  genuinely different `administrative_area_level_2` neighborhood still writes.
- A DAO-level test of the `upgrade` predicate against the in-memory store,
  asserting default-mode selection is unchanged and an unknown mode raises.

Manual or integration checks:
- `make test-unit` and `make test-bdd` green.
- Replay the shipped `parse_address` over the 488-address production sample
  captured for this plan and confirm the parser's output is IDENTICAL to the
  pre-change run — this change must not move a single parsed value.
- Before the production sweep: probe Place Details with the minimal
  `id,addressComponents` mask for Downtown Beer Garden's stored `place_id` and
  confirm `sublocality_level_1 == "Espinheiro"`. If Google does not answer it,
  **stop and escalate** — do not invent a parser heuristic to cover the gap.
- One events projection pass against a real `redis:7`, confirming the payload
  byte size and key count are unchanged from the
  `plans/260905_events-serving-projection.md` baseline (the field set did not
  change; only two values did).

## Acceptance Criteria
- `make test-unit` and `make test-bdd` are green and the feature file's `@wip`
  tag is removed.
- The events serving projection's field set is unchanged — a field-by-field diff
  of `EventOccurrence` against `plans/260905_events-serving-backend.md`'s
  contract table shows no addition, removal, rename or retype.
- A stored `ticket_url` of `"evenyx.com"` is projected as `"https://evenyx.com"`;
  a stored non-URL is projected as `null`; an already-absolute URL is projected
  unchanged. The RDS row is unmodified in all three cases.
- The default backfill mode selects exactly the population it selects today —
  proven by a test that the pre-change predicate and the `fill` predicate return
  the same rows.
- `upgrade` mode selects a fully-populated `parsed` row, and makes zero Place
  Details calls while `address_backfill_geocoding_enabled` is off.
- `venue_address_source_rows{field,source}` and
  `events_projection_ticket_url_total{outcome}` are exported with every label
  value pre-initialised.
- **Post-merge, operator-gated rollout** (not part of the PR): enable
  `address_backfill_geocoding_enabled`, `PUT /admin/config/address_backfill_cursor`
  with `{"last_venue_id": null}`, then drive
  `address_components_backfill` with `{"mode": "upgrade"}` until `done` is true,
  watching `venue_geocoding_requests_total{outcome="success"}` stay far below the
  10,000/month Essentials allowance. Then confirm via the live API that
  Downtown Beer Garden's event cards carry `neighborhood: "Espinheiro"` and that
  no other venue's neighborhood regressed against the table in Evidence.

## Open Questions
- None.
