# Address components backfill: Google-authoritative, parser fallback, provenance-guarded

## Branch
feature/address-components-backfill

## Goal
Fill `venues.address.{street,neighborhood,city,postal_code}` — empty on all 3,600
catalog rows today — for the existing backlog and for every venue going forward, so
the events UI card's "`<Bairro>` · 2,3 km" line stops rendering with no bairro. Do
it with Google Places (authoritative) as the primary source and a new, pure,
data-derived text parser over `raw_text` as the fallback for whatever Google cannot
resolve, and make the two sources compose safely: an authoritative answer may
correct a parsed guess, nothing may silently overwrite an operator-entered value,
and a parsed guess must never be able to block Google's later, better answer.

## Non-goals
- Text-Search-based `place_id` resolution for venues that have never been
  Google-enriched (no stored `google_place_id`). This backfill reuses the
  already-persisted `place_id` only, exactly as costed in discovery. Resolving the
  remaining un-enriched venues is a separate, explicitly costed decision (mirrors
  how `plans/260905_events-serving-projection.md` Phase 2 §3 deferred its own
  backfill for the identical reason).
- The minimal-mask ($13.31) or full-mask ($66.53) Place Details backfill routes.
  Geocoding-by-`place_id` ($0, pending the verification this plan defines in
  Phase 3) is the only Google route this plan authorizes. If verification fails,
  the fallback is the parser alone — not an automatic escalation to a paid route.
  A paid route stays a separate, explicit spend decision per this repo's $10/mo
  unapproved-increase rule.
- An operator-facing UI or endpoint for hand-correcting one venue's address. The
  `operator` provenance tier is designed for and protected by the precedence rule
  in Phase 2, but nothing in this plan writes it — see Open Questions.
- Deriving `city_slug` (the events projection's city partition) from
  `venues.address.city`. That derivation deliberately stays sourced from
  `admin.geo_fence_city` (`plans/260905_events-serving-projection.md` Phase 4 §5);
  this plan does not touch it and does not make `venues.address.city` load-bearing
  for anything beyond display.
- Re-deriving venue eligibility/servability from the new address fields.
- Any change to `raw_text`, `lat`, `lng`, or the events serving projection's
  payload shape. This plan only fills columns the projection already reads.

## Evidence
- **The write paths, today.** `RdsVenueStore.upsert_venue`
  (`app/dao/rds_venue_store.py:131-199`) is the only writer of
  `venues.address.raw_text/lat/lng`, sourced from BestTime's echoed address
  string, and explicitly never touches the four structured columns (comment at
  191-193). `RdsVenueStore.update_venue_address_components`
  (`rds_venue_store.py:202-235`) already exists — added by commit `200f9f0`
  (`plans/260905_events-serving-projection.md` Phase 2, merged to `main`
  2026-09-05/06) — and is a **blind, per-field COALESCE**: any non-empty value
  always overwrites any prior value, with **no provenance tracking at all**.
  `tests/test_venue_address_components.py::test_a_fresh_non_null_answer_does_update_a_previously_stored_value`
  asserts this explicitly ("Boa Viagem" overwrites "Santo Amaro"). This is
  correct for Google re-enriching itself, but means a parsed guess written
  through this same method would permanently block Google's later, better
  answer — confirmed, not hypothetical, and exactly the risk the task named.
- **The Google half is already wired end to end for the opportunistic path.**
  `VIBE_FIELDS_MASK` already requests `addressComponents`
  (`app/api/google_places_client.py:31-46`, comment confirms this adds no SKU
  tier). `app/services/venue_address_components.py::map_address_components`
  (pure, no I/O) maps Google's raw `addressComponents` through a documented
  fallback chain (`sublocality_level_1` → `sublocality` →
  `administrative_area_level_2` for neighborhood;
  `administrative_area_level_2` → `locality` for city). `enrich_venue`
  (`app/services/google_places_enrichment_service.py:435`) calls
  `self._apply_address_components(venue_id, details.address_components)`
  (`:213-235`), which maps and writes via
  `update_venue_address_components`, and increments
  `VENUE_ADDRESS_COMPONENTS_TOTAL{outcome}` (`app/metrics.py:2025-2042`,
  zero-filled for `written`/`unchanged`). `tests/bdd/enrichment/venue-address-components.feature`
  and `tests/test_venue_address_components.py` cover the mapping and the
  blind-overwrite write path (18 tests, all passing on `main`).
- **The opportunistic path structurally never reaches the existing backlog.**
  `enrich_all_venues` (`google_places_enrichment_service.py:516+`) skips any
  venue with an existing `vibe_attributes` row unless `force_refresh=True`
  (`:563`); the daily cron (`app/config.py:246`, `main.py:539-542`) always
  calls it with the default `force_refresh=False`. `force_refresh=True` fires
  only at brand-new-venue creation (`add_venue_handler.py:1361`) or an
  admin-triggered force-refresh. `plans/260905_events-serving-projection.md`
  Phase 2 §3 says this outright: *"Opportunistic only — no forced
  re-enrichment pass... A one-off backfill over the whole catalog would
  re-buy Place Details for every venue and is deliberately not part of this
  plan; if the operator wants faster coverage it is a separate, costed
  decision."* This plan is that follow-up, via the free route the prior
  plan's own Evidence section had already identified but did not spend on.
- **Production census (read-only, whole catalog, 3,600 rows;
  `.../scratchpad/DISCOVERY.md` + `census_result_v3.txt` +
  `address_census_v3.py`).** All four structured columns are 0/3,600, 0
  exceptions. 3,575 rows have non-empty `raw_text`; two coexisting formats:
  comma-delimited ("…- Bairro, City - UF, CEP, Brazil", 370 rows, unambiguous
  split) and space-joined ("…- Bairro City - UF CEP Brazil", 2,941 rows,
  needs city-name recognition to find the bairro/city boundary at all).
  Anchored on the codebase's own 27-capital list
  (`app/services/venue_eligibility.STATE_CAPITALS`, which already carries a
  `lat`/`lng` per capital — reused directly in Phase 1), a capitals-only parse
  resolves 2,987/3,474 Brazilian rows (86.0%). The 487 failures: 324 have the
  right shape but the city isn't a capital (largest: Niterói 55, Olinda 50,
  Camaragibe 40, Maringá 31, Jaboatão dos Guararapes 28, Lauro de Freitas 21,
  plus smaller pockets in Fortaleza/Natal/Salvador/São Luís/Rio's Baixada
  Fluminense — **not** just the Recife metro); 163 have no parseable shape at
  all (missing bairro-separator dash, or a space/comma substituting for it
  before the UF). Only 302/487 failures are in the 2,661-venue servable set.
- **`place_id` is already persisted, so the Google half of the backfill is one
  call per venue.** `google_places_enrichment_service.py:361-366` sets
  `vibe_attrs.google_place_id` on every enrichment (Redis,
  `VibeAttributes.google_place_id`). A backfill over already-enriched venues
  needs no new Text Search.
- **Cost (`.../scratchpad/DISCOVERY.md`, LENS: google-capability, verified
  against `developers.google.com` Sept 2026 — not from anything on disk).**
  Reusing the full `VIBE_FIELDS_MASK` = $66.53 over 2,661 venues (Enterprise +
  Atmosphere tier, already the ceiling tier this mask pays for every other
  vibe attribute). A minimal `"id,addressComponents"` Place Details mask =
  $13.31 (Essentials tier). **Geocoding API by `place_id` = $0** — Essentials
  tier, but Google's own FAQ states 10,000 free requests/month, and cs-server
  calls no Google Geocoding endpoint anywhere today (confirmed by grep — the
  "geocode" hits elsewhere are BestTime's own internal geocoder). 2,661 <
  10,000. **This external-docs claim is not independently verifiable from this
  repo and must be verified cheaply before any bulk run — see Phase 3.**
- **Reusable infrastructure already in this repo**, so this plan adds no new
  concurrency/observability primitives, only new entries in existing ones:
  `app/services/job_lock.py` (shared scheduler+admin concurrency guard,
  `LOCKED_JOB_NAMES` frozenset), `app/services/pipeline_run_registry.py`
  (`run_scope`, ULID run ids, `PIPELINE_RUN_INFO` metric, bounded ring —
  generic, already used by every `JOB_REGISTRY` entry), `app/routers/admin_trigger_router.py`
  `JOB_REGISTRY` + `_run_job`/`trigger_job`/`stop_job` (generic dispatch by
  registry entry — a new job needs only a new dict entry, no new endpoint),
  `app/services/admin_config_service.py` + `GET/PUT /admin/config/{key}`
  (`admin_trigger_router.py:1207-1230`, generic RDS-truth/Redis-mirror KV with
  per-key validators registered in `app/container.py:634-660` — already the
  mechanism `ADMIN_CONFIG_ELIGIBILITY_KEY`/`ADMIN_CONFIG_GEOFENCE_KEY`
  (`venue_eligibility.py:40,44`) use for exactly "admin-editable without a
  deploy").
- `tests/rds_fake.py::InMemoryRdsVenueStore.update_venue_address_components`
  mirrors the real store's SQL and must change in lockstep (its own docstring
  says so).

## Current Behavior
Every venue's `venues.address` row has `raw_text`/`lat`/`lng` from BestTime and
null `street`/`neighborhood`/`city`/`postal_code`. Google Places enrichment
requests and maps `addressComponents` and writes them with a blind, no-provenance
overwrite — but only for venues that get a full `enrich_venue` call, which today
means brand-new venues and admin force-refreshes, never the existing ~2,661
servable backlog. No text parser exists. The events serving projection already
reads `venues.address.neighborhood` into its `venue_neighborhood` payload field
(`plans/260905_events-serving-projection.md` Phase 4 §4); with the column null,
the field is null, and the events UI card shows no bairro.

## Desired Behavior
- Every venue's address row is filled from the best available source: Google's
  Geocoding answer for a venue with a stored `place_id` (subject to Phase 3's
  free-tier gate), the text parser for whatever Google didn't answer or wasn't
  attempted for, and never touched again by a lower-precedence source once a
  higher-precedence one has answered.
- The parser is pure, deterministic, and never invents a bairro it isn't
  reasonably sure of: no shape match leaves the field null; an ambiguous match
  is corroborated against the venue's own coordinates before being trusted.
- The city vocabulary the parser matches against is derived from the data, is
  inspectable and requires operator approval before it affects any write, and
  can grow without a deploy.
- A bounded, resumable, idempotent, observable job backfills the existing
  catalog. A newly added venue gets the same treatment automatically, with no
  manual trigger required.
- The Geocoding free-tier assumption is verified cheaply before the bulk run,
  and there is a concrete, cheap, immediate way to stop relying on it if it
  turns out to be wrong.

## Implementation Approach

### Phase 1 — the parser and the data-derived vocabulary (pure, no I/O)

**`app/services/venue_address_parser.py`** — new pure module, same style as its
sibling `venue_address_components.py`: given `raw_text` and a `vocabulary`
(a sequence of `{name, lat, lng, ambiguous}` entries — see below), returns
`{street, neighborhood, city, postal_code}`, each a non-empty string or `None`.
Never does I/O; never guesses when it isn't reasonably sure.

Algorithm, in the order it runs:

```
1. postal_code := last regex match of \b\d{5}-?\d{3}\b in raw_text,
   normalized to "NNNNN-NNN". Independent of every other step (~95% hit
   rate per the census) — the cheapest, safest win, extracted even when
   nothing else below resolves.

2. uf := the RIGHTMOST standalone token matching a real Brazilian UF code
   (word-boundaried, no dash required before it — "Recife PE 500..." and
   "Recife, PE," both match). None found -> stop, return all-None except
   postal_code (address unparseable as Brazilian).

3. tail := raw_text[:match_start(uf)]  (everything before the UF token)

4. Strip tail's OWN trailing UF-separator before splitting it, or the next
   step finds the wrong dash: rstrip whitespace; if tail now ends in a
   dash-like character, remove exactly that one dash and rstrip again; then
   also strip a trailing comma the same way (the comma-format's "... - PE,"
   leaves one in the identical position). Without this, in the DOMINANT
   space-joined format the LAST dash in tail is the one directly before the
   UF (e.g. "...Niterói - " immediately before "RJ"), not the street/bairro
   dash before it — every row of this shape (3,311 of 3,575) would split on
   the wrong dash and yield an empty blob. Traced:
   "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil" ->
   tail (raw, step 3) = "R. Dr. Paulo César 225 - Santa Rosa Niterói - " ->
   rstrip -> ends in "-" -> remove it, rstrip again -> "R. Dr. Paulo César
   225 - Santa Rosa Niterói" (no trailing comma either) — THIS fixed-up
   tail is what step 5 below splits, not the raw one.

5. Find the LAST dash-like character in the fixed-up tail.
   - found: street := tail[:last_dash], blob := tail[last_dash+1:]
   - not found: street := None, blob := tail (some real rows have no
     street-bairro separator at all, e.g. "COHAB Recife - PE ..." — step 8
     below exists exactly because this branch cannot trust blob outright)
   Trim whitespace/commas from blob.

6. FORMAT B (comma-delimited): if blob contains a comma, split on the LAST
   comma -> bairro_candidate, city_candidate. Guard against a street-address
   comma masquerading as the bairro/city comma (e.g. a store-number or
   floor/kiosk fragment before the real trailing city): reject the split
   and fall through to step 7 if city_candidate contains a digit or exceeds
   4 words. A guarded split needs no vocabulary lookup — the comma makes it
   unambiguous by construction; city_candidate is still checked in step 9.

7. FORMAT A (space-joined), or Format B rejected: tokenize blob into words
   (a hyphenated compound like "Ceará-Mirim" is one token). For n = 3, 2, 1
   (longest first — vocabulary entries are already-resolved names; longest
   match wins so a 2-word approved city is preferred whole over accidentally
   matching just its own last word), take the trailing n words, fold
   accents/case, check membership in vocabulary. First (longest) match wins
   -> city_candidate = original-case trailing n words, bairro_candidate =
   the remaining leading words (empty is valid: bairro unknown, not an
   error). No match at any n -> city_candidate = None, bairro_candidate =
   None (this row is left for Google or a future vocabulary update — never
   guessed).

8. PLAUSIBILITY GUARD — only when step 5 found no dash (street is None):
   the leading remainder that steps 6 (Format B) or 7 (Format A) resolve as
   bairro_candidate carries no confirmation that it is a real bairro at
   all, since there was no recognizable street/number before it to anchor
   that reading. Reject it — keeping the already-matched city_candidate
   untouched — when bairro_candidate contains a digit, has more than 3
   words, or has any word (accent/case-folded) in the stoplist {LOJA,
   QUIOSQUE, PISO, SALA, SHOPPING, COHAB, CONJUNTO} (mall/housing-complex
   fragments this shape commonly captures, not real bairros). Rejected ->
   bairro_candidate := None. ONE rule covers both known examples: "COHAB
   Recife" (bairro_candidate "COHAB", 1 word, stoplist hit -> rejected,
   city "Recife" kept) and "Jardim Santa Maria São Paulo" (bairro_candidate
   "Jardim Santa Maria", 3 words, no stoplist hit -> accepted, city "São
   Paulo") are the SAME check applied to two different remainders, not two
   different rules. When step 5 DID find a dash (street is not None), this
   guard does not run — a real street/number before the split is itself
   the corroborating signal Format A's/B's match already relies on.

9. AMBIGUITY GUARD (both formats): if the matched/split city_candidate's
   vocabulary entry (or, for an unvetted Format-B exact split, a
   vocabulary lookup that comes back "ambiguous") is flagged `ambiguous`,
   accept it only if the venue's own (lat, lng) is within
   `address_backfill_ambiguous_radius_km` (default 50km) of the entry's
   (lat, lng); otherwise treat the steps 6/7 match as a non-match (city,
   neighborhood := None) rather than trust the text alone. Non-ambiguous
   entries skip this check (fast path — most matches never need it).

10. neighborhood := bairro_candidate or None. city := city_candidate.
```

Three worked edge cases from real production `raw_text` values, because this
is where the design is either right or wrong:
- `"R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil"`:
  blob = "Santa Rosa Niterói" (per steps 4-5 above: the dash directly before
  "RJ" is stripped first, so the LAST dash found is the one after "225", not
  the one after "Niterói" — see the traced example in step 4). Vocabulary
  suffix check tries n=1 first: "Niterói" ∈ vocabulary (once approved) →
  city="Niterói", neighborhood="Santa Rosa". The over-merge discovery's own
  census script produced (`19 Icarai Niteroi` as if it were one city name)
  cannot happen here: the LIVE parser only ever matches against the frozen,
  already-approved vocabulary — it never invents an n-gram at request time.
  Over-merge risk is confined entirely to the offline mining step below,
  which is exactly why that step is gated on operator approval.
- `"...Jardim São Paulo, Recife - PE ..."` (a bairro that itself contains a
  city name) vs. `"...Boa Vista Recife - PE ..."` (a bairro name that is also
  a state capital): longest-suffix-first, anchored strictly at blob's own
  end, resolves both correctly. The first: Format B comma split gives
  bairro="Jardim São Paulo", city="Recife" directly — the comma marks the
  boundary, no suffix matching needed for the split itself, only to validate
  "Recife". The second (space-joined, no comma): n=2 suffix "Vista Recife"
  is not in vocabulary; n=1 "Recife" is → city="Recife",
  neighborhood="Boa Vista" — correct, because matching always starts from
  the true end of the string, never a "contains" search that could seize
  "Boa Vista" out of the middle.
- `"COHAB Recife - PE 51340-670 Brazil"` vs. `"Jardim Santa Maria São Paulo -
  SP ..."`: both hit step 5's "not found" branch — no street/number at all
  before the bairro+city run, so street=None and blob is the whole fixed-up
  tail. FORMAT A resolves the city the same way either time (n=1 "Recife";
  n=2 "São Paulo"), leaving bairro_candidate "COHAB" (1 word) vs. "Jardim
  Santa Maria" (3 words). Step 8's plausibility guard is the ONE rule that
  reconciles both: "COHAB" matches the stoplist exactly → rejected, city
  "Recife" kept, neighborhood unset; "Jardim Santa Maria" is 3 words with no
  digit and no stoplist hit → accepted, neighborhood="Jardim Santa Maria",
  city="São Paulo". Neither is a special case carved out for its own sake —
  the same check simply resolves differently on two different remainders.

**City vocabulary** — `app/services/venue_city_vocabulary.py`, also pure.
`STATE_CAPITALS` (`venue_eligibility.py:72-100`, 27 entries, already carrying
`lat`/`lng`) seeds it directly. Deriving the REST from data, with the
correctness guard the task requires, is two related, mechanical passes over
`venues.address` rows (raw_text + lat/lng), never a single frequency count:

- **Candidate generation**, over rows where no vocabulary suffix currently
  matches: generate trailing 1/2/3-word n-grams. A candidate is promoted to
  the *proposed* list only if it clears BOTH an independent gates:
  (a) **frequency** — it recurs as the trailing token across
  `address_backfill_min_distinct_remainders` (default 3) or more DISTINCT
  leading (bairro) prefixes, or came from an unambiguous Format-B comma split
  (trustworthy even at count 1); and (b) **geo-tightness** — the venues that
  produced it cluster within `address_backfill_geo_tightness_km` (default
  30km) of their own centroid. All n-gram lengths that pass are surfaced
  side by side (not just the longest or shortest) — a real municipality name
  can legitimately be 1-3 words ("Niterói" vs. "Lauro de Freitas" vs.
  "Jaboatão dos Guararapes"), and a purely statistical test cannot always
  tell a true multi-word name from a shorter name's own fragment sharing the
  identical points (a 1-word suffix of a genuine 3-word city passes the exact
  same frequency+tightness gates the true name does, because it's the same
  rows). **This is a known, structural limit of statistical clustering, not
  a bug to hide** — it is exactly why candidates are proposed, never applied,
  and an operator (who knows or can look up Brazilian municipality names)
  picks the correct one before anything is approved. A small mechanical
  assist orders the default suggestion: Portuguese connector words
  (de/da/do/das/dos) and prefixes (São/Santo/Santa/Nossa/Nova/Novo)
  immediately to the left of a candidate's boundary extend it leftward by
  one word before the candidate is finalized, correctly drafting "Lauro de
  Freitas", "Jaboatão dos Guararapes", "São Gonçalo", "Nova Iguaçu" — but
  this is a draft suggestion, not a guarantee (a chain like "São José de
  Ribamar" can still come out wrong from this mechanical pass alone); the
  operator can edit the proposed name before approving.
- **Ambiguity flag**, over ALL blobs (not just residual failures), for every
  vocabulary name whether seeded (a capital) or newly proposed: count how
  often the name appears inside a blob as a substring **that is not** the
  actual longest-suffix match for that row (i.e., some other city won).
  Above `address_backfill_ambiguous_ngram_min_occurrences` (default 2), flag
  `ambiguous=True`. This is exactly how "Boa Vista" (Roraima capital, also a
  common bairro name including in Recife) gets flagged automatically from the
  data, without a hand-maintained collision list, and is what step 9 (the
  ambiguity guard) of the parser algorithm above checks against.

Candidates and flags are **never applied automatically**. They are written to
a new admin-config key, `address_city_vocabulary_candidates` (read-only
output, no validator — see Phase 2), for an operator to inspect via the
existing generic `GET /admin/config/address_city_vocabulary_candidates` — no
new endpoint.

**The vocabulary the LIVE parser actually reads is `STATE_CAPITALS` UNIONED
WITH a separate key, `address_city_vocabulary` (by name/slug, capitals
winning any collision) — never the config value alone.** This is a
deliberate departure from this repo's own established precedent:
`AdminConfigService.get()`/`.set()` store and return the given JSON
verbatim, with no merge semantics at all
(`app/services/admin_config_service.py:41-69`), and the one existing config
consumer built the same way, `venue_eligibility.load_geo_fence`
(`venue_eligibility.py:622-649`), reads its admin-config Redis mirror
directly and uses a present value AS-IS, fully replacing the default.
Following that same precedent here would be wrong: `address_city_vocabulary`
is designed, above, to hold only NEW cities beyond the 27 capitals — the
candidates key by construction proposes only new cities — so an operator
approving the first batch through a config value built the usual way would
silently replace the vocabulary with just those, regressing the
capitals-derived 86% baseline the very first time anyone approves anything.
`venue_city_vocabulary.py`'s own `load_city_vocabulary(admin_config_service)`
— analogous in shape to `venue_eligibility.load_eligibility_config`/
`load_geo_fence`, but deliberately NOT in merge behavior — is the one
function responsible for this union; every call site (the live parser and
Phase 4's backfill service) goes through it rather than reading the
admin-config key directly, so the union can never be bypassed by a new
caller forgetting it. `address_city_vocabulary` (the config value) is edited
by the operator through the existing generic `PUT /admin/config/{key}` (a
new validator, described in Phase 2, checks shape only — not correctness,
which stays a human judgment call) and need only ever carry additions, never
a copy of the capitals. Re-running the mining job any time (it makes no
external calls, is cheap and read-only against RDS) re-surfaces fresh
candidates as new venues arrive, satisfying "pick up new cities without a
deploy."

### Phase 2 — provenance (migration + write-path change)

**Migration `0045_venue_address_provenance` (additive, nullable, chains from
`0044_event_flyer_media`).** `venues.address` gains four nullable text
columns: `street_source`, `neighborhood_source`, `city_source`,
`postal_code_source`, each `CHECK (col IS NULL OR col IN ('operator',
'google', 'parsed'))` — matching this repo's established
raw-SQL-text-plus-CHECK-constraint style (`0042_venue_add_job_run.py`'s
`job_type`/`status`) rather than a Postgres enum type. Every existing row
gets NULL sources (correct: nothing has been written under this regime yet,
and every existing data column is null anyway per the census). Downgrade
drops exactly the four new columns; nothing else in the table is touched.
Justified because the *existing* write path (`update_venue_address_components`)
is a **blind** COALESCE with no way to know whether a stored value came from
an operator, Google, or a parser — adding a second, lower-trust writer (the
parser) onto that same blind method would let a parsed guess permanently
block Google's later, correct answer. That is a real, present gap, not a
hypothetical one (confirmed by this method's own existing test suite, which
asserts any fresh non-null value always wins).

**`RdsVenueStore.update_venue_address_components` gains a required
`source` keyword** (raises `ValueError` on anything outside
`{"operator","google","parsed"}`, so a future caller cannot forget it
silently). Per field, a non-empty incoming value is written only when the
column's stored `_source` is NULL or the incoming source's precedence is
`>=` the stored source's precedence (`operator` > `google` > `parsed`) —
`>=`, not `>`, so a source can still refresh its own prior answer (preserves
today's tested "Google's second answer overwrites Google's first" behavior).
A `None`/empty incoming value never writes, exactly as today. This changes
one existing internal method (added yesterday, not a stable public
contract) — the plan intentionally breaks its current blind-overwrite
signature rather than adding a parallel method, because the whole point of
this phase is that the blind version is unsafe once a second writer exists.

```sql
-- one field's CASE, repeated per column; :rank(x) = operator:3, google:2, parsed:1, else 0
neighborhood = CASE WHEN NULLIF(:neighborhood,'') IS NOT NULL
                     AND (neighborhood_source IS NULL OR RANK(:source) >= RANK(neighborhood_source))
               THEN :neighborhood ELSE neighborhood END,
neighborhood_source = CASE WHEN NULLIF(:neighborhood,'') IS NOT NULL
                            AND (neighborhood_source IS NULL OR RANK(:source) >= RANK(neighborhood_source))
                       THEN :source ELSE neighborhood_source END
```
(`RANK(x)` inlined as `CASE x WHEN 'operator' THEN 3 WHEN 'google' THEN 2 WHEN 'parsed' THEN 1 ELSE 0 END`
in the actual query — Postgres has no bare enum-ordering function for a plain
text column here, matching this repo's no-custom-function style.)

- `app/dao/venue_repository.py::update_venue_address_components` needs **no
  change** — it already forwards `**components` unchanged, so a `source=`
  kwarg passes through automatically.
- `_apply_address_components` (`google_places_enrichment_service.py:213-235`)
  gets a one-line change: `update(venue_id, source="google", **mapped)`.
- `tests/rds_fake.py::InMemoryRdsVenueStore.update_venue_address_components`
  gets the identical precedence logic (its docstring already promises to
  mirror the real SQL).
- Existing tests in `tests/test_venue_address_components.py` are updated to
  pass `source="google"` explicitly (preserving every existing assertion's
  intent unchanged); new tests cover cross-source precedence (see Test Plan).

### Phase 3 — the authoritative Google path (Geocoding by stored `place_id`)

**New client method**, `GooglePlacesAPIClient.geocode_by_place_id(place_id)`.
This is genuinely new integration surface, not a reuse of `get_place_details`:
the Geocoding API is a different, legacy REST family
(`https://maps.googleapis.com/maps/api/geocode/json?place_id=...&key=...`,
query-string auth) from Places API (New)
(`GOOGLE_PLACES_API_BASE = "https://places.googleapis.com/v1"`,
`X-Goog-Api-Key` header) that every existing method in this client uses.
Its response's `address_components` use `long_name`/`short_name` fields, not
Places (New)'s `longText`/`shortText` — the method normalizes each component
to `{"longText": long_name, "types": types}` internally and returns that
list, so `map_address_components` (Phase 1's sibling, already merged) is
**reused unchanged** rather than duplicating its fallback-rung logic for a
second response shape.

**Free-tier verification, before any bulk run — required, not assumed.**
The 10,000-free-requests/month figure is from Google's public docs, not from
anything in this repo, and this repo has no independent pricing/quota data
(confirmed by discovery). Verification is necessarily partly out-of-band
(this codebase has no GCP Billing API integration and this plan does not add
one):
1. A new admin-config boolean, `address_backfill_geocoding_enabled`
   (default **false**), gates every Geocoding call site — the backfill
   (Phase 4) and the new-venue fallback (Phase 4) both check it before
   calling `geocode_by_place_id`; when false, both paths run the parser only
   and make zero Geocoding calls.
2. A new counter, `VENUE_GEOCODING_REQUESTS_TOTAL{outcome}`
   (`success`|`no_place_id`|`api_error`|`disabled`), zero-filled at startup —
   this is both an operational metric and the in-repo corroborating signal
   for the free-tier claim (an operator watches the `success` count in
   Grafana stay comfortably under 10,000/month).
3. The operator flips `address_backfill_geocoding_enabled` on and triggers
   the backfill job with a small explicit `{"limit": 10}` (same override
   mechanism `google_places_backfill`'s `cfg.get("limit")` already
   establishes) — a tiny, cheap, reversible sample.
4. The operator checks the Google Cloud Console Billing page for the
   Geocoding API SKU for that project and confirms $0 charged. This step
   cannot be performed or confirmed by this codebase — it needs a human with
   Console access; see Open Questions.
5. **If verification fails** (a charge appears, or the call errors as
   API-not-enabled-for-this-key): flip `address_backfill_geocoding_enabled`
   back to false immediately (one admin-config write, no deploy, no restart)
   and cancel any in-flight run via the existing
   `POST /admin/trigger/address_components_backfill/stop`. The backfill then
   continues on the parser alone — silently degrading Google coverage to
   zero rather than accumulating unapproved spend, exactly this repo's
   posture elsewhere ("design to the free tier, probe free before paying").
   Buying the minimal-mask route afterward stays a separate, explicit
   spend-approval decision (Non-goals).

### Phase 4 — the backfill job, and going forward

**`app/services/venue_address_backfill_service.py`** — new service,
constructed with the venue repository, `GooglePlacesAPIClient`, and
`AdminConfigService`.

`backfill_batch(limit)`: reads the persisted cursor (see below), selects up
to `limit` `venues.address` rows with `venue_id > cursor` where at least one
of the four structured columns is still null, ordered by `venue_id` alone.
**Not** servable-first: an earlier draft of this plan ordered `(servable
venues first, then venue_id)` via a `LEFT JOIN serving.eligible_venue`,
still resumed with the same single `venue_id > cursor` cursor — but
`venue_id`s carry no correlation to servability, so once the cursor
advanced past a servable row's id, every non-servable row whose id sorts
below that cursor value would be permanently skipped without ever being
processed, silently orphaning a slice of the non-servable Brazilian backlog
and violating this plan's own acceptance criterion that every venue
eventually gets a value or a logged reason. A correct two-tier resume needs
a compound cursor `(servable, venue_id)` persisting both parts, not a
single column — real, but not worth the extra persisted state and query
complexity here: the whole 3,600-row catalog completes in 18 batches at the
default batch size, so servable-first priority buys an operator nothing
noticeable, and no acceptance criterion or other part of this plan depends
on it. Plain `venue_id` ordering is simpler and cannot have this bug. For
each row:
1. If `address_backfill_geocoding_enabled` and the venue has a stored
   `google_place_id`: call `geocode_by_place_id`, map, write with
   `source="google"`. A transport/API error logs a warning and moves on —
   the row's nulls are untouched, so it is naturally retried on the next
   run, never poisoned with a false answer (mirrors
   `_search_and_enrich_servable`'s own existing "transport failure must
   never look like a real no-match" posture).
2. For any of the four fields still null after step 1 (no Google attempt,
   no place_id, or Google's response didn't answer that particular field):
   run the Phase 1 parser against `raw_text` with the current approved
   vocabulary, write with `source="parsed"`.
3. Increment `VENUE_ADDRESS_PARSED_TOTAL{outcome}` (`written`|`unchanged`,
   mirroring `VENUE_ADDRESS_COMPONENTS_TOTAL`'s existing vocabulary) and
   `VENUE_GEOCODING_REQUESTS_TOTAL{outcome}` per venue.

**Bounded**: `limit` (config default `address_backfill_batch_size = 200`,
overridable per trigger). **Resumable**: the cursor (last processed
`venue_id` + running totals) is persisted to a new admin-config key,
`address_backfill_cursor`, **once per batch** (not per venue — every write
this service makes is idempotent under Phase 2's precedence rule, so a crash
mid-batch simply re-processes up to `limit` already-safe rows on the next
run; no per-venue write is needed to stay correct). **Idempotent**: re-running
over already-filled rows changes nothing (the query's own `WHERE` excludes
fully-filled rows once every field is non-null; any field already answered
by an equal-or-higher-precedence source is a COALESCE no-op regardless).
**Observable**: `VENUE_ADDRESS_BACKFILL_REMAINING{field}` gauge (count of
still-null rows per column), refreshed at the end of each batch, plus the
two counters above and the existing `PIPELINE_RUN_INFO`/log-correlation
machinery every `JOB_REGISTRY` entry already gets for free. **Cannot run
away**: a new `JOB_REGISTRY["address_components_backfill"]` entry (runner ->
`backfill_batch(limit=cfg.get("limit", settings.address_backfill_batch_size))`)
is added in `app/routers/admin_trigger_router.py` — **and, a separate edit,
easy to miss**: a new `ADDRESS_COMPONENTS_BACKFILL` constant is added to
`app/services/job_lock.py`'s `LOCKED_JOB_NAMES` frozenset literal
(`job_lock.py:26`, currently four named constants built by hand — NOT
derived from `JOB_REGISTRY`'s keys, so adding the registry entry alone does
not enroll the job in the shared lock). Both edits are required so an admin
trigger cannot race a scheduled run of the same job, and
`POST /admin/trigger/address_components_backfill/stop` (generic, already
exists) cancels an in-flight run between venues — no new stop mechanism
needed.

**Going forward — no manual step.** Brand-new venues already get a full
`enrich_venue(force_refresh=True)` call at creation
(`add_venue_handler.py:1361`), which already calls `_apply_address_components`
(now `source="google"`). The gap is what happens when Google has no match or
no `place_id` for a new venue: `enrich_venue`, immediately after its existing
`_apply_address_components` call, invokes a small shared helper —
`VenueAddressBackfillService.apply_parser_fallback(venue_id, raw_text)` —
which runs only if the address row still has a null field after the Google
attempt. This is the SAME code path Phase 4's bulk job uses per-venue (no
duplicated logic), so a newly added venue gets a bairro from one of the two
sources within the same request that created it, with no separate cron or
manual trigger.

## Data, Config, And API Impact
- **Migration** `migrations/versions/0045_venue_address_provenance.py`:
  additive, nullable, `CHECK`-constrained columns on `venues.address`; working
  `downgrade()` drops exactly those four columns. No other table changes.
- **Persistence contract change**: `RdsVenueStore.update_venue_address_components`
  gains a required `source` kwarg and precedence-aware semantics (was: blind
  overwrite). One caller (`google_places_enrichment_service.py`) updated.
  `venue_repository.py`'s wrapper is unaffected (kwargs passthrough).
- **New settings** (`app/config.py`, deploy-time, not spend-sensitive):
  `address_backfill_batch_size: int = 200`,
  `address_backfill_ambiguous_radius_km: float = 50.0`,
  `address_backfill_geo_tightness_km: float = 30.0`,
  `address_backfill_min_distinct_remainders: int = 3`,
  `address_backfill_ambiguous_ngram_min_occurrences: int = 2`.
- **New admin-config keys** (no deploy needed to change, via the existing
  generic `GET/PUT /admin/config/{key}`):
  `address_backfill_geocoding_enabled` (bool, default false — the free-tier
  kill switch, Phase 3); `address_city_vocabulary` (list of
  `{name, lat, lng, ambiguous}`, operator-approved, new
  `validate_address_city_vocabulary_config` registered in
  `app/container.py`'s `AdminConfigService(validators={...})` — shape only:
  non-empty list, each entry a dict with a non-empty `name` and numeric
  `lat`/`lng` in plausible Brazil ranges; correctness of the name is a human
  call, not validated); `address_city_vocabulary_candidates` (list, no
  validator — read-only mining output); `address_backfill_cursor` (small
  JSON progress state, system-written, no validator).
- **New API surface**: none. Reuses the existing generic
  `GET/PUT /admin/config/{key}` and `POST /admin/trigger/{job}[/stop]`
  routes with new registry/key entries — no new endpoints.
- **New Prometheus metrics**: `VENUE_ADDRESS_PARSED_TOTAL{outcome}` (zero-filled
  `written`/`unchanged`), `VENUE_GEOCODING_REQUESTS_TOTAL{outcome}`
  (zero-filled `success`/`no_place_id`/`api_error`/`disabled`),
  `VENUE_ADDRESS_BACKFILL_REMAINING{field}` gauge
  (`street`/`neighborhood`/`city`/`postal_code`).
- **New job registry entries**: `address_components_backfill` (Phase 4 — a
  `JOB_REGISTRY` entry in `admin_trigger_router.py` AND a new constant in
  `job_lock.py`'s `LOCKED_JOB_NAMES` frozenset, two separate edits, neither
  optional) and `address_vocabulary_mining` (Phase 1's candidate-generation
  job — read-only against RDS, no external calls, not added to
  `LOCKED_JOB_NAMES`).
- No change to the events serving projection payload, to `raw_text`/`lat`/`lng`,
  or to any cross-repo contract.

## Error Handling And Observability
- A Geocoding transport/API error for one venue logs a warning with the
  venue id and is counted `VENUE_GEOCODING_REQUESTS_TOTAL{outcome="api_error"}`;
  the row's nulls are left untouched (not poisoned with a false marker), so
  it is retried on the batch's next run — mirrors this repo's existing
  "a transport failure must never look like a real answer" posture for
  Google Places search.
- A venue with no stored `place_id` counts `outcome="no_place_id"` and goes
  straight to the parser — expected and common, not an error.
- While `address_backfill_geocoding_enabled` is false, every attempted
  Geocoding call short-circuits to `outcome="disabled"` and the parser runs
  alone — the explicit, cheap, reversible degraded mode Phase 3 designs for
  a failed free-tier verification.
- The parser never raises on malformed `raw_text` — worst case, every field
  comes back `None` (no shape matched), which is a normal, expected, logged
  outcome (`VENUE_ADDRESS_PARSED_TOTAL{outcome="unchanged"}`), not a failure.
- An operator watching this feature roll out sees: `VENUE_ADDRESS_BACKFILL_REMAINING`
  trending down per field across batches; `VENUE_GEOCODING_REQUESTS_TOTAL{success}`
  staying well under the free-tier ceiling (Grafana); `PIPELINE_RUN_INFO`
  showing each `address_components_backfill` run's id, status and duration,
  with its logs correlated by `job=<id>` exactly like every other job;
  `address_backfill_cursor` (via `GET /admin/config/address_backfill_cursor`)
  showing exact resume position and running totals; `address_city_vocabulary_candidates`
  (via the same generic config GET) showing what the miner proposed, for
  review before it can affect a single write.
- A crash or container restart mid-batch loses at most the in-flight batch's
  progress (cursor persisted once per batch, not per venue) and resumes
  cleanly on the next trigger — no partial/duplicate writes, because every
  write is idempotent under the Phase 2 precedence rule.

## Test Plan
Feature file: `tests/bdd/enrichment/address-components-backfill.feature`

Scenarios:
- Google's authoritative answer fills the bairro (`source=google`) for a
  venue with no stored value yet.
- The parser fills the bairro from a space-joined address when the venue has
  no Google place id.
- The parser splits a comma-delimited address into bairro and city even for
  a city not yet in the approved vocabulary.
- Google's answer overwrites a previously parsed bairro (precedence: google
  beats parsed).
- A parsed answer is refused over an already Google-sourced bairro (order-
  independence: parsed can never undo google).
- An operator-entered bairro is never overwritten by Google or the parser
  (highest precedence — exercised directly at the DAO/precedence level,
  since no endpoint sets `source=operator` yet).
- The parser leaves the bairro unset rather than guess when the address has
  no recognizable city (no shape match).
- The parser accepts a plausible multi-word bairro found the same way (no
  street/number before it) when it does not hit the digit/word-count/
  stoplist guard — the same rule that rejects the no-recognizable-shape
  case above, applied to a remainder it should not reject.
- The parser picks the true trailing city over a look-alike city name
  written inside the bairro itself.
- An ambiguous city match is accepted only when the venue's own coordinates
  corroborate it, and left unset otherwise.
- A newly added venue with no Google match still gets a bairro from the
  parser automatically, with no manual trigger.
- The backfill processes a bounded batch and resumes from exactly where it
  left off on the next run, reaching every venue — servable and
  non-servable alike — with none silently skipped.
- Re-running the backfill over already-filled venues writes nothing new.
- The backfill makes no Geocoding calls while the free-tier kill switch is
  off, and still fills what the parser can.

Pytest unit tests:
- `tests/test_venue_address_parser.py` (new): every step of the Phase 1
  algorithm against real verbatim production `raw_text` samples from the
  census (`census_result_v3.txt` §6 and the municipality table), including
  both formats, the no-shape-at-all cases, the space-instead-of-dash and
  comma-instead-of-dash UF variants, the Format-B street-comma guard, and
  both worked ambiguity edge cases (Niterói/Icaraí, Boa Vista/Recife,
  Jardim São Paulo/Recife). Explicit cases for step 4's UF-separator strip
  (the empty-blob defect the plan review found in the dominant format):
  "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil",
  "R. Abdon Batista, 300 - Santo Amaro Recife - PE 50100-460 Brazil",
  "Av. Herculano Bandeira, 21 - Pina Recife - PE ...", and the comma-format
  "R. Carmelita Muniz de Araújo, 225 - Casa Caiada, Olinda - PE, 53130-645,
  Brazil" — each must produce a non-empty blob and the correct bairro/city
  split, not the empty blob the unfixed steps 3-4 produced. Explicit cases
  for step 8's plausibility guard, side by side so the single rule is
  proven to resolve both directions rather than one regressing the other:
  "Jardim São Paulo, Recife" (accepted, Format B), "Boa Vista Recife"
  (accepted, Format A), "R. da Guia, 207 - Recife PE" (accepted with an
  empty bairro — no leading words is not a guard rejection), "COHAB Recife"
  (rejected — stoplist hit, city kept), and "Jardim Santa Maria São Paulo"
  (accepted — 3 words, no stoplist hit, city kept).
- `tests/test_venue_city_vocabulary.py` (new): candidate generation's
  frequency + geo-tightness gates (including a synthetic reproduction of the
  Icaraí over-merge, asserting the miner does NOT auto-promote it), the
  particle/prefix extension heuristic on the real multi-word municipality
  names from the census table, and the ambiguity flag against a synthetic
  Boa-Vista-style collision. A read-path test for `load_city_vocabulary`
  proving an approval payload for `address_city_vocabulary` that omits the
  27 capitals does not drop them from what the parser actually consumes —
  the union happens at read time regardless of what the config value holds.
- `tests/test_venue_address_components.py` (updated): existing tests pass
  `source="google"` explicitly; new tests cover every precedence pairing
  (parsed→google upgrades; google→parsed refused; operator→anything
  refused; same-source always refreshes, both for `google` and for
  `parsed`).
- `tests/test_venue_address_backfill_service.py` (new): batch bounding,
  cursor persistence and resume, idempotent re-run, the geocoding-disabled
  degraded path, and the new-venue `apply_parser_fallback` hook.
- `app/dao/rds_venue_store.py`'s new precedence SQL verified against a real
  Postgres integration test alongside the existing COALESCE integration
  coverage (per `tests/README.md`'s `make test-integration`), not only the
  fake.

Manual or integration checks:
- The Phase 3 free-tier verification sequence itself (tiny sample run +
  Google Cloud Console Billing check) — cannot be automated from this repo;
  an explicit operator action before the bulk backfill is enabled.
- After Phase 4 ships, a spot check of the events UI card against a handful
  of real venues whose bairro the backfill filled.

## Acceptance Criteria
- `venues.address.{street,neighborhood,city,postal_code}` are no longer
  0/3,600 — every venue has either a non-null value with a recorded source,
  or an observable, logged reason it stayed null (no place_id and no
  parseable shape) — never silence.
- A parsed value is never able to block a subsequent Google answer for the
  same field on the same venue; an operator-entered value is never
  overwritten by either.
- The derived city vocabulary that affects any write is exactly the
  operator-approved list at `address_city_vocabulary`; the mining job's
  candidates never take effect on their own.
- The Geocoding free-tier verification steps in Phase 3 have been run and
  their outcome recorded (either confirmed $0 and the bulk backfill enabled,
  or a nonzero charge and `address_backfill_geocoding_enabled` left/returned
  to false with the parser as the sole route) before any full-catalog run.
- The backfill job can be stopped mid-run via the existing generic stop
  endpoint and resumed later with no lost or duplicated work.
- A brand-new venue added after this ships gets a filled bairro (from Google
  or the parser) with no manual step.
- No change to `raw_text`, `lat`, `lng`, the events projection payload
  shape, or `city_slug`'s derivation.

## Open Questions
- The Geocoding API's 10,000-free-requests/month figure is from Google's
  public docs, not verifiable from this repo. Phase 3 defines a cheap
  in-band verification (tiny sample + a Prometheus counter as a
  corroborating signal), but the actual confirmation — checking the Google
  Cloud Console Billing page for the project — is a human action outside
  this codebase and `/execute-feature` cannot complete Phase 3's rollout
  gate on its own; it must stop at "verification pending" until an operator
  performs and confirms it.
- Whether the project's existing `google_places_api_key` is already
  authorized for the Geocoding API SKU in Google Cloud Console (a separate
  enablement from the Places API (New) SKU it already uses) is unverified.
  If not enabled, the first verification call fails with a clear
  permission/not-enabled error; enabling it is a Console action, not a code
  change, and should be checked before or as part of the Phase 3 sample run.
- Whether any other consumer in the same Google Cloud project already draws
  against the same Geocoding API free quota this month is unverified from
  this repo (only confirmed: cs-server itself calls no Geocoding endpoint
  today). Worth an explicit operator sanity check before assuming the full
  10,000/month headroom is available.
- The default geo-tightness/ambiguity-radius/min-distinct-remainder constants
  in Phase 1 are reasoned defaults, not values tuned against the full
  production corpus. `/execute-feature` should confirm them against the real
  3,474-row Brazilian dataset (not just the worked examples in this plan)
  during the pytest phase, and adjust if the miner's proposed candidates
  look wrong on the actual data.
- No writer for `source="operator"` exists yet (Non-goals) — the precedence
  tier is designed for and protected, but nothing sets it. If an operator
  hand-edit path is wanted, it is a separate follow-up plan.
- No remediation path exists for a value already written under a
  since-corrected vocabulary entry. The backfill's own idempotency (Phase 4)
  excludes any row where every structured column is already non-null, so a
  bairro written from a mistaken (human-approved) vocabulary entry is
  skipped by every future run; fixing the vocabulary and re-running does not
  correct rows the backfill already touched. With no `source="operator"`
  writer yet (above) and Geocoding default-off (Phase 3), a wrong value can
  persist indefinitely once written. Two remediation shapes for a follow-up
  plan, not designed or built here: (a) an operator tool that resets
  `value`+`_source` to NULL for every row whose given field is
  `source="parsed"` and whose stored value matches a specific
  (now-corrected) vocabulary entry's name, so the next backfill run
  re-evaluates it; or (b) a `re-parse` run mode that re-evaluates every row
  with `source="parsed"` against the CURRENT vocabulary regardless of
  null-ness, overwriting only when the new parse disagrees with the stored
  value (same precedence rule — `parsed` can overwrite `parsed`). This plan
  ships without a correction path and accepts that risk for the initial
  rollout.
