# Fix Admin Venue-Type Breakdown 500 And Accent-Folded Add-Venue Geo Short-Circuit

## Branch
fix/admin-breakdown-and-addvenue-fold

## Goal
Two operator-visible bug fixes from the 2026-07-09 cross-repo refactor
assessment (cs-server S1 and the behavior half of S5):

1. `GET /admin/venue-type-breakdown` must return HTTP 200 with per-type venue
   counts instead of the permanent 500 it returns today.
2. Re-adding an accented venue (e.g. "LAÇA, Pina" already cataloged as
   "Laca Pina") must short-circuit on the free local geo lookup instead of
   falling through and spending a paid BestTime `add_venue_to_account` call.

## Non-goals
- The wider dead-code sweep (S2-S4), performance batch (P1-P5), scaffolding
  abstractions (A1-A5), and renames (N1-N4) from the same assessment — each is
  planned separately.
- Deleting the breakdown endpoint (the assessment offers "1-line fix or
  −47 LOC"; operators still want the breakdown, so we fix it).
- Converting the breakdown handler from `async def` to `def` (threadpool) —
  that belongs to the performance batch (P4) which covers all blocking admin
  handlers together.
- Any change to `_find_name_match` ranking, containment guards, or the
  geo-fallback linking flow — only `_geo_lookup`'s comparison normalization
  changes.
- De-duplicating the `_field`/`_get` twin helpers in the same file (dead-code
  batch).

## Evidence
Verified against the code on 2026-07-10:

- `app/routers/admin_trigger_router.py:796` — `venue_dao = _container.venue_dao`.
  The container defines no `venue_dao` attribute: `app/container.py:92` names
  the serving Redis DAO `redis_only_dao` and `app/container.py:108` names the
  RDS-backed repository `redis_venue_dao`. The attribute access raises
  `AttributeError` on every request, swallowed by the blanket handler at
  `admin_trigger_router.py:826-828` into `HTTPException(500)` — the endpoint
  has never returned data.
- `app/routers/admin_trigger_router.py:368-376` —
  `_get_venue_dao_from_container()` exists to work around exactly this naming
  inversion (getattr fallback `venue_dao` → `redis_venue_dao`) and is already
  used by the sibling endpoints (e.g. lines 394, 442, 690).
- `app/routers/admin_trigger_router.py:804-818` — the breakdown loop issues
  two reads per venue (`get_venue(vid)` at 805, `get_vibe_attributes(vid)` at
  814) over every id from `list_all_venue_ids()` (797).
- `app/dao/venue_repository.py:41,119-126` — `VenueRepository` subclasses
  `RedisVenueDAO` and offers `list_all_venues()` backed by one bulk RDS row
  read (`rds_store.list_all_venue_rows()`), which the working inventory
  endpoint (`admin_trigger_router.py:690-738`) already uses.
- `app/handlers/add_venue_handler.py:447-468` — `_geo_lookup` normalizes with
  bare `venue_name.strip().lower()` (456) and `(venue.venue_name or "").strip().lower()`
  (463) before the equality/containment check (466). No accent folding.
- `app/handlers/add_venue_handler.py:790-799` — `_fold_text` accent-folds,
  casefolds, strips punctuation, and collapses whitespace; the geo-fallback
  matcher `_find_name_match` (850-894) uses it consistently (862, 870, 885).
- `app/handlers/add_venue_handler.py:147-159` — the geo-cache short circuit
  (step 2 of `add()`) returns 200 `already_exists` **before** the budget
  reservation (162) and the paid BestTime create (178). A missed geo hit for
  an accented re-add therefore reserves a monthly slot and spends a BestTime
  `POST /forecasts` call on a venue we already have.
- `app/metrics.py:648-659` — `ADD_VENUE_BY_ADDRESS_TOTAL{result=...}` labels
  include `already_exists` (the geo short-circuit outcome) and `created`.

Line-number drift vs the assessment: none material. The assessment cites
`_find_name_match` at "862+"; the def is at 850 with folding at 862.

## Current Behavior
- `GET /admin/venue-type-breakdown` always returns
  `500 {"detail": "'Container' object has no attribute 'venue_dao'"}` — the
  AttributeError is logged and converted at 826-828. Operators cannot see the
  BestTime-type / Google-type distribution at all.
- `POST /admin/venues/by-address` for a venue whose cataloged name differs
  from the submitted name only by accents/punctuation misses the `_geo_lookup`
  short circuit (bare lowercase comparison), reserves a manual budget slot,
  and issues a paid BestTime create for a venue already in the catalog. The
  same submission matches fine in the geo-fallback path because
  `_find_name_match` folds.

## Desired Behavior
- `GET /admin/venue-type-breakdown` must return 200 with `total_venues`,
  `with_google_type`, `besttime_types`, and `google_places_types` counts,
  resolving its DAO through `_get_venue_dao_from_container()` like its
  siblings.
- `_geo_lookup` must compare names with the same `_fold_text` normalization
  as `_find_name_match`, so an accented re-add of an existing nearby venue
  returns 200 `already_exists` from the local geo index, increments
  `add_venue_by_address_total{result="already_exists"}`, consumes no monthly
  budget slot, and makes no BestTime call.

## Implementation Approach
- `app/routers/admin_trigger_router.py` — replace the direct
  `_container.venue_dao` access (796) with `_get_venue_dao_from_container()`
  (503-guard behavior included). While there, drop the id-then-`get_venue`
  N+1 half of the loop by iterating `venue_dao.list_all_venues()` (one bulk
  RDS row read, the pattern the inventory endpoint uses) instead of
  `list_all_venue_ids()` + per-id `get_venue`. The per-venue
  `get_vibe_attributes` read stays for now — it is an admin-only, low-traffic
  endpoint; the performance batch's bulk per-table readers (P1) can later
  serve it in one query (noted there).
- `app/handlers/add_venue_handler.py` — in `_geo_lookup`, replace both
  `.strip().lower()` normalizations (456, 463) with `_fold_text(...)`, keeping
  the existing exact/containment check and the deprecated-venue skip
  unchanged. `_fold_text` is defined at module scope in the same file; no
  import changes.
- No signature, route, DTO, or Redis-key change anywhere.

## Data, Config, And API Impact
None. No request/response schema change (`/admin/venue-type-breakdown` starts
returning the body it always declared), no persistence, config, feature-flag,
or migration impact. Folding in `_geo_lookup` strictly widens the set of
requests that short-circuit locally; every previously matching pair still
matches (`_fold_text` is a superset normalization of `.strip().lower()` for
the equality/containment checks used here).

## Error Handling And Observability
- No new runtime path. The breakdown endpoint keeps its existing
  log-and-500 blanket handler (826-828) for genuine failures and gains the
  standard 503 "service unavailable" from `_get_venue_dao_from_container()`
  when the container is not initialized.
- The geo short-circuit already increments
  `add_venue_by_address_total{result="already_exists"}` (155); accented
  re-adds now land in that label instead of `created`. No metric or log
  additions required.

## Test Plan
Feature file: `tests/bdd/api/admin-breakdown-and-addvenue-fold.feature`

Scenarios:
- Admin venue-type breakdown returns 200 with per-type counts — seed venues
  with known BestTime types and Google primary types, assert `total_venues`,
  `with_google_type`, and both count maps.
- Admin venue-type breakdown counts venues without Google enrichment — a venue
  lacking vibe attributes appears in `besttime_types` but not
  `google_places_types`.
- Accented re-add short-circuits on the local geo index — a venue cataloged
  with a folded name exists nearby; the operator re-adds it with the accented
  name; response is 200 already-exists, no BestTime create call is made, the
  monthly counter does not move, and
  `add_venue_by_address_total{result="already_exists"}` increments.

Pytest unit tests:
- `_geo_lookup` matches accented vs folded names both directions
  (submitted-accented/stored-folded and the reverse) and still skips
  deprecated venues; a genuinely different name still misses.
- Breakdown handler counts with the DAO fake: correct maps, sorted by
  descending count, unknown `venue_type` bucketed as "unknown".

Manual or integration checks:
- None (BDD covers the HTTP contracts with deterministic fakes; no live
  BestTime calls, per repo policy).

## Acceptance Criteria
- `GET /admin/venue-type-breakdown` returns 200 with correct
  `total_venues`/`with_google_type`/`besttime_types`/`google_places_types` on
  a seeded catalog, and no longer references `_container.venue_dao`.
- An add-venue request whose name differs from an active nearby cataloged
  venue only by accents/punctuation returns 200 already-exists **without**
  any BestTime API call and **without** consuming a budget slot.
- Non-accented behavior is preserved: exact and containment geo-lookup
  matches that succeeded before still succeed; deprecated venues are still
  skipped (re-add after geo-link undo still falls through to BestTime).
- Full pytest + BDD suites green.

## Rollback
Both fixes are single-file, behavior-local edits with no persisted-state or
schema impact: reverting the PR restores the previous behavior immediately.
No data cleanup needed (the endpoint writes nothing; the fold only changes
which venues short-circuit at request time).

## Open Questions
- None.
