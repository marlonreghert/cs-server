# Venue address-hash cache can permanently link a new venue to the wrong existing one

## Branch
fix/venue-address-cache-integrity

## Goal
The add-venue flow must never permanently cache a venue match it isn't
geographically confident in. When it can't confidently link a submission to an
existing venue, it must fall through to a normal create (or `created_google_only`)
rather than silently short-circuiting to an unrelated venue — no BestTime link
is an acceptable outcome; a wrong link is not. Also repair the specific
already-poisoned cache entries discovered this session so those 7 venues can be
added cleanly.

## Non-goals
- A catalog-wide audit for other possibly-poisoned `venue_lookup_by_address_v1:*`
  entries beyond the 7 already found. Real risk, but a separate, larger effort —
  tracked as an open question below, not built here.
- Redesigning BestTime's own venue-identity model. One of the 7 cases (see
  Evidence) may reflect BestTime's own geocode-derived id scheme rather than a
  cs-server bug; this plan does not assume it must be "fixed" the same way.
- Any change to the Google Places photo-resolution `search_place_id` name-match
  guard's behavior for its existing callers (photo archival, etc.) — the fix is
  scoped to the add-venue geo-fallback path's use of the resolved coordinate,
  not to `search_place_id` itself.

## Evidence
All found and verified live during this session (2026-08-16), operating a
274-venue add-venue campaign (`plans/260814_venue-discovery-nordeste-sp.md`).

- 7 confirmed-live Redis entries, each a permanent (`TTL -1`) mapping from a
  submitted `venue_name|venue_address` hash to the WRONG existing venue's id —
  verified via `GET /admin/venues/inventory?q=<venue_id>` resolving to a
  different real-world venue than what was submitted:
  - `Buteco` (Fortaleza, CE) → **Buteco São Bastião**, Parnamirim, RN (~500 km away)
  - `Bambu Bar Eventos` (São Luís, MA) → **"Bar"**, Jatiúca, Maceió, AL (~1,500 km away)
  - `Boteco Cia do Chopp` (Recife, PE) → **Cia do Chopp**, same neighborhood, different street
  - `boteco beer` (Recife, PE) → **Boteco**, Aldeota, Fortaleza, CE
  - `Boteco da Orla` (Salvador, BA) → same **Boteco**, Fortaleza, CE (identical wrong target as the row above, despite a different submitted city)
  - `Boteco Senador` (São Paulo, SP) → same **Boteco**, Fortaleza, CE (again)
  - `Riva Bar de Praia` (Fortaleza, CE) → **Boteco do Illa**, same exact address in Fortaleza but a different name (no name overlap at all — see Open Questions)
- The cache mechanism: `VENUE_LOOKUP_BY_ADDRESS_KEY_V1 = "venue_lookup_by_address_v1:{hash}"`
  (`app/handlers/add_venue_handler.py:43`), `hash = sha1(name.lower()+"|"+address.lower())`.
  Written by `_save_address_cache` (line 596), read by `_lookup_cached_venue_id`
  (line 584) and `BatchAddService._already_active_id`
  (`app/services/batch_add_service.py:174`) as a free pre-check that returns
  `already_exists` without any Google/BestTime call — so once a bad value is
  cached, every future attempt at that exact name+address short-circuits to the
  wrong venue forever, with no expiry and no operator-visible signal that
  anything is wrong.
- The geo-fallback matcher (`_find_name_match`, line 1378) is correctly
  distance-bounded in design — it only considers candidates from
  `venue_dao.get_nearby_venues(lat, lng, radius_m / 1000.0)` at
  `DEFAULT_FALLBACK_RADIUS_M = 50` meters (line 63) — and name-length-gated
  (`MIN_CONTAINMENT_MATCH_LEN = 5`, line 73, added 2026-07-02 in `02eb36d`,
  refined 2026-07-11 in `dabff37` — over a month before this session, so a
  fresh `"bar"` (3 chars) containment match, like the Maceió case, cannot be
  produced by today's code; that specific entry predates the guard or bypassed
  it some other way).
- The actual gap is upstream, in coordinate resolution:
  `GooglePlacesAPIClient.search_place_id` (`app/api/google_places_client.py:159`)
  queries Text Search with `f"{venue_name} {venue_address}"` (includes the
  city/state in the query text) and only accepts a candidate whose Google name
  matches via `names_match(venue_name, candidate_name)` (line 239) — **there is
  no geographic check at all**. When `lat`/`lng` bias isn't supplied (true for
  every row in this session's batch submissions — they carried only
  `venue_name`/`venue_address`, no `bias_lat`/`bias_lng`), Google's own
  relevance ranking can still occasionally surface a same-or-similar-named
  venue in a completely different city above the intended target — plausible
  for generic name patterns like "Bar", "Boteco X", "Bambu Bar Eventos". Once
  that wrong place_id's coordinates come back, `_geo_fallback`'s 50 m nearby
  search is *correctly* bounded around the *wrong* point, finds a real nearby
  venue there, and (if containment/exact also lines up) permanently caches the
  link. The system's individual pieces are each behaving as designed; the gap
  is that nothing checks whether the resolved coordinate itself was ever
  trustworthy before it's used to gate a permanent cache write.

## Current Behavior
`AddVenueByAddressRequest` always carries a caller-supplied `venue_lat`/`venue_lng`
today for the synchronous endpoint, but `BatchAddService._resolve_coords`
(`app/services/batch_add_service.py:191`) will resolve coordinates from a bare
`place_id` or an **unbiased Text Search** when a batch row supplies neither —
which is exactly the path every row in this session's campaign took. Whatever
coordinate comes back, correct or not, is trusted equally: it's used directly
for the 50 m geo-fallback nearby search, and a resulting match (exact or
containment) is cached with no expiry and no record of how confidently the
underlying coordinate was resolved.

## Desired Behavior
1. The add-venue flow must distinguish a **trusted** coordinate (caller-supplied
   `venue_lat`/`venue_lng`, or resolved from a caller-supplied `place_id`) from
   an **unanchored** one (resolved from a bare Text Search with no bias and no
   place_id). Geo-fallback matching (both `_geo_fallback`'s nearby search and
   any resulting `_save_address_cache` write) must only run against a trusted
   coordinate.
2. When coordinates were resolved without a trusted anchor, the add must skip
   geo-fallback matching entirely and proceed straight to a normal create (or
   `created_google_only` if BestTime can't forecast it) — same outcome shape a
   genuinely brand-new venue already gets today. No address-hash cache entry is
   written for a skipped geo-fallback attempt.
3. An admin can clear one specific poisoned `venue_lookup_by_address_v1:*` entry
   by supplying the venue_name + venue_address that produced it (not a raw Redis
   key), through the same DAO/handler boundary the rest of add-venue uses — no
   ad hoc Redis access. The action is logged (old value, who/when).
4. After the above ship, the 7 venues in Evidence are re-submitted through the
   normal add-venue pipeline (not special-cased) and each resolves to a create,
   a `created_google_only`, or a *correct* nearby match — never the previously
   wrong venue.

## Implementation Approach
- Thread a trust signal for the resolved coordinate through
  `BatchAddService._resolve_coords` → `AddVenueByAddressRequest` (or an internal
  parameter alongside it) into `AddVenueHandler.add()`, so `_geo_fallback` can
  check it before calling `get_nearby_venues`/`_find_name_match`/
  `_save_address_cache`. The synchronous `/admin/venues/by-address` endpoint
  already requires caller-supplied lat/lng, so it stays trusted by construction;
  only the batch path's Text-Search-without-bias case needs the new gate.
- Skip path: when untrusted, follow the same branch the code already takes when
  `_geo_fallback` finds no match at all (falls through to BestTime create /
  `created_google_only`) — reuse that branch rather than adding a new one.
- New small admin endpoint (naming/route to be finalized during Gherkin
  authoring, e.g. `POST /admin/venues/address-cache/clear` with
  `{"venue_name", "venue_address"}` in the body) that computes the same
  `_address_hash`, reads the current value for logging, deletes the key via the
  existing Redis client the handler already holds, and returns what was
  cleared (or a 404-shaped "nothing cached" if there was nothing to clear).
- Apply it to the 7 known entries as part of this fix's own rollout (a one-time
  admin action after merge+deploy, not a migration script), then re-submit
  those 7 venue_name+venue_address pairs through the normal batch-add endpoint
  as the final verification step (see Test Plan).

## Data, Config, And API Impact
- New admin route (exact path decided in Gherkin) for clearing one address-cache
  entry by name+address. No RDS schema change. No change to the
  `venue_lookup_by_address_v1:{hash}` key format itself — same keys, just no
  longer written when the underlying coordinate wasn't trustworthy, and
  individually correctable when already wrong.
- `AddVenueByAddressRequest` / the batch row model may gain an internal
  trusted-coordinate flag — not a new field on the public request contract if
  it can be derived from "was place_id or explicit lat/lng present," which
  should be the common case; confirm during implementation whether a new
  explicit field is actually needed or whether it's inferable from existing
  inputs.

## Error Handling And Observability
- Log (info level) whenever geo-fallback is skipped for lack of a trusted
  coordinate, with venue_name — distinct from today's "no match found" log line,
  so an operator can see how often this path is taken versus a genuine
  zero-result.
- Add a Prometheus counter for geo-fallback-skipped-untrusted-coordinate,
  labeled separately from the existing add-venue outcome metrics, so this
  doesn't silently blend into "created" counts.
- The new admin cache-clear endpoint logs the venue_name/venue_address, the
  hash, and the value it deleted (never silently no-ops without a log line),
  matching this repo's "background jobs must log failures with enough context"
  standard.

## Test Plan
Feature file: `tests/bdd/api/venue-address-cache-integrity.feature`

Scenarios:
- Add-venue with no caller-supplied coordinates and no place_id, where Text
  Search resolves to a location far from any real nearby match for the
  submitted name, results in a create (or `created_google_only`) — not a
  geo-fallback link — and writes no address-hash cache entry for a rejected
  geo-fallback attempt.
- Add-venue with a caller-supplied place_id or explicit lat/lng still uses
  geo-fallback matching exactly as today (regression guard — trusted-coordinate
  path is unchanged).
- Admin clears a specific address-cache entry by venue_name+venue_address;
  a subsequent add for that exact name+address no longer short-circuits to the
  old (wrong) venue_id.
- Clearing an address-cache entry that was never set returns a clear "nothing
  to clear" result, not an error.

Pytest unit tests:
- `AddVenueHandler`/`_geo_fallback`: geo-fallback is not attempted when the
  resolved coordinate is untrusted, is attempted when trusted — isolated from
  the Google/BestTime clients (fakes), covering both branches directly.
- The new cache-clear method: computes the expected hash, deletes only that key,
  returns the prior value, handles a missing key cleanly.

Manual or integration checks:
- After merge and deploy, clear the 7 known-bad entries listed in Evidence via
  the new admin endpoint, then resubmit those exact venue_name+venue_address
  pairs through `POST /admin/venues/batch-add` and confirm each now resolves to
  `created`, `created_google_only`, or a genuinely correct nearby match — never
  the previously wrong venue_id. Verify via
  `GET /admin/venues/inventory?q=<result venue_id>` that the resolved venue's
  name/address actually matches what was submitted (or is a defensible
  same-location match, not an unrelated venue).

## Acceptance Criteria
- An add-venue attempt whose coordinates were resolved from an unbiased Text
  Search (no place_id, no explicit lat/lng) can no longer produce a permanent
  address-hash cache entry pointing at an unrelated existing venue — proven by
  the new BDD scenario, not just manual spot-checks.
- The trusted-coordinate path (caller-supplied place_id or lat/lng) is
  unaffected — existing add-venue BDD scenarios still pass unchanged.
- All 7 known-bad entries from Evidence are cleared and their venues
  successfully re-added (or confirmed `created_google_only`) with no wrong link,
  verified live against prod inventory after deploy.
- Full `make test` (unit + BDD) stays green.

## Open Questions
- **`Riva Bar de Praia` / `Boteco do Illa`** (same exact Fortaleza address, zero
  name overlap): this didn't come through `_find_name_match` at all (neither
  folded name contains the other), so it isn't explained by the containment/geo
  gap above. Two live possibilities: (a) a genuinely separate bug in the
  `recovered_from_timeout` inventory-reconcile path, or (b) BestTime's own
  venue_id is geocode-derived independent of name, so two different submitted
  names at the identical physical point legitimately resolve to BestTime's same
  internal venue — in which case this isn't a cs-server bug to fix, just a
  characteristic to document. Needs its own quick investigation at execute time
  before deciding whether it's in scope for the same fix or a separate,
  possibly-unfixable note. Do not assume it's part of the same bug until
  confirmed.
- Should a broader one-time audit of all existing `venue_lookup_by_address_v1:*`
  keys for other poisoned entries be scoped as a near-term follow-up? Real risk
  (this session found 7 out of ~280 attempts — roughly 2.5% — purely by
  coincidence while retrying a specific campaign, not by looking for this),
  but a full audit is a distinct, larger effort deliberately left out of this
  plan's Non-goals. Flagging for the operator to decide, not blocking this fix.
