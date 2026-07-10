# Dead-Code Sweep (Refactor Assessment S2-S4 + Twin Helper)

## Branch
chore/dead-code-sweep

## Goal
Delete the provably unreferenced / unreachable code identified by the
2026-07-09 cross-repo refactor assessment (cs-server items S2, S3, S4, and the
`_field`/`_get` twin helper from S5) — roughly 300-400 production LOC plus the
unit tests that exist only to pin the deleted code — with zero observable
behavior change.

## Non-goals
- The two behavior bug fixes from the same assessment (S1 admin breakdown,
  S5's `_geo_lookup` accent folding) — planned separately in
  `plans/260710_admin-breakdown-and-addvenue-fold.md`.
- Performance work (P1-P5), scaffolding abstractions (A1-A5, S6-S8), and
  renames (N1-N4) — planned separately.
- The BDD step-harness consolidation the assessment flags as opportunistic.
- Deleting `PhotoEnrichmentService.refresh_photos_for_venues` — the dormant
  service method stays (see Open Questions for its main.py wrapper).
- Any Redis key, DTO field, or config-contract change beyond removing settings
  fields that are documented as intentionally dead.

## Evidence
Verified against the code on 2026-07-10:

- **S2 — duplicate `__init__`:** `app/db/geo_redis_client.py:14-37` defines an
  `__init__(host, port, password, db)` that is silently shadowed by the second
  `__init__(client)` at `39-54`. Every construction site passes a client
  (`app/container.py:88`, all test fixtures), so the first definition is
  unreachable by Python semantics.
- **S3 — unreachable `verbose` branches in `VenueHandler._transform`:**
  `app/handlers/venue_handler.py:289-291` returns `merged` early when
  `verbose` is true; yet inside the minified-only loop, `verbose` is consulted
  at 353 (`all_photos if verbose else all_photos[:2]`), 393-399 (reviews
  load "only for verbose"), and 453-476 (menu load "only for verbose").
  These branches can never execute; `venue_reviews` (392) and `venue_menu`
  (452) are always `None` in the minified DTO, while the comments at 391 and
  451 claim verbose-mode loading that cannot occur.
- **S4 — confirmed dead code (no production callers):**
  - `app/services/google_places_enrichment_service.py:319-367`
    `enrich_venues()` — zero references anywhere.
  - `app/api/besttime_client.py:428-461` `get_venues_nearby()` and `641-663`
    `get_venue_search_progress()` — the "Legacy methods" block (comment at
    426); zero callers, zero tests. (The assessment cites this file as
    `besttime_client.py` — it lives under `app/api/`, not `app/clients/`.)
  - `app/services/venue_service.py` (whole 36-line file) — `VenueService` is
    constructed at `app/container.py:314` and exported by
    `app/services/__init__.py:2,6`, but `container.venue_service` is read
    nowhere; the only other references are its own tests
    (`tests/test_services.py:52-66`).
  - `app/dao/redis_venue_dao.py:316-328` `list_cached_live_forecast_venue_ids`
    (referenced only by its own unit tests, `tests/test_redis_dao.py:176-188`
    and `tests/test_redis_dao_unit.py:170-177`), `471-485`
    `list_cached_vibe_attributes_venue_ids` (zero references), and `673-688`
    `get_venue_photos_fresh` (no production caller; used as a read-back
    helper by `tests/test_photo_resolve.py` — see Open Questions).
  - `main.py:147-172` `run_photo_enrichment_job` — never scheduled, never
    imported, no admin trigger; the Job 5 comment at `main.py:446-455`
    documents the pre-bake as retired and this wrapper as "intact but dormant".
  - `app/config.py` eight `*_on_startup` fields —
    `google_places_enrichment_on_startup` (178), `photo_enrichment_on_startup`
    (206), `instagram_enrichment_on_startup` (228),
    `ig_posts_enrichment_on_startup` (238), `menu_enrichment_on_startup`
    (246), `menu_extraction_on_startup` (280), `vibe_classifier_on_startup`
    (286), `refresh_on_startup` (311) — read by no code path;
    `main.py:616-630` documents them as intentionally dead
    (`startup_background_pipelines` is a no-op by design after the 2026-07-01
    incident). Mirror entries exist in `config.example.json`
    (34, 42, 52, 64, 74, 87, 102, 115). The BDD step that proves the
    "startup runs nothing" guarantee builds a `SimpleNamespace`
    (`tests/bdd/steps/discovery_hardening_geofence_steps.py:91-98`), so it
    does not depend on the Settings fields existing.
- **S5 twin helper:** `app/handlers/add_venue_handler.py:802-810` `_field`
  and `840-847` `_get` are byte-for-byte identical in behavior (attr-or-dict
  read). `_get` has 9 call sites (690-705); `_field` has the docstring.

Line-number drift vs the assessment: `get_venues_nearby` is 428-461 (not
-463), `get_venue_search_progress` is 641-663 (not -662),
`list_cached_vibe_attributes_venue_ids` ends at 485 (not 483) — all trivial;
the BestTime client path is `app/api/besttime_client.py`.

## Current Behavior
The listed code exists but is unreachable or unreferenced. It compiles, is
partly pinned by unit tests that test only the dead members, and misleads
readers: the `_transform` comments promise verbose-mode reviews/menu that
never load; the first `GeoRedisClient.__init__` suggests host/port
construction that can never happen; the `*_on_startup` settings suggest
startup pipelines that are deliberately disabled.

## Desired Behavior
Identical runtime behavior with the dead code gone:

- `GeoRedisClient` has one `__init__(client)`.
- `_transform` contains no `verbose` branches after the early return; the
  minified DTO continues to serialize `venue_reviews`/`venue_menu` as `None`
  (model defaults — the response body must stay byte-identical).
- `enrich_venues`, the two legacy BestTime methods, `VenueService` (file,
  container wiring at `container.py:314`, `app/services/__init__.py` export,
  import at `container.py:13`), the three dead DAO methods,
  `run_photo_enrichment_job`, and the eight `*_on_startup` Settings fields
  (plus their `config.example.json` entries) no longer exist.
- The `main.py:616-630` no-startup-pipelines doc comment stays (reworded to
  past tense: the settings were removed, and a stray `*_ON_STARTUP` env var
  is ignored by Settings as before — pydantic reads only declared fields).
- `add_venue_handler` keeps one attr-or-dict helper: keep `_field` (it has
  the explanatory docstring), delete `_get`, and repoint the 9 `_get` call
  sites.
- Unit tests that exist solely to pin deleted members are removed with them
  (`tests/test_services.py` `TestVenueService` + its fixture,
  `test_redis_dao.py`/`test_redis_dao_unit.py` cases for
  `list_cached_live_forecast_venue_ids`).

## Implementation Approach
Pure deletion, one commit per concern is not required — a single reviewed PR:

1. `app/db/geo_redis_client.py` — delete the first `__init__` (14-37).
2. `app/handlers/venue_handler.py` — drop the `verbose` references inside the
   minified loop: photos truncation becomes unconditional `all_photos[:2]`,
   the reviews/menu loading blocks are removed, and
   `venue_reviews=None`/`venue_menu=None` keep flowing into `MinifiedVenue`
   exactly as today (explicitly or via model defaults — response JSON
   unchanged). Update the stale comments.
3. Delete dead members listed in Evidence: `enrich_venues`; the two legacy
   BestTime methods and their "Legacy methods" banner; `venue_service.py` +
   its container/`__init__` wiring; the three DAO methods; the
   `run_photo_enrichment_job` wrapper (updating the Job 5 comment to stop
   naming it); the eight Settings fields + `config.example.json` entries
   (keeping both explanatory comments).
4. `app/handlers/add_venue_handler.py` — delete `_get`, repoint its call
   sites to `_field`.
5. Delete/trim the test cases that only pin deleted members; adjust
   `tests/test_photo_resolve.py` per the Open Question resolution.

## Data, Config, And API Impact
None — behavior-preserving. No API response change (minified DTO fields
`venue_reviews`/`venue_menu` remain in the schema, still always `None`; the
verbose path is untouched). No Redis key change. Config impact is limited to
removing eight settings fields that no code reads; existing `.env`/JSON
values for them become ignored input (they already are functionally ignored).
No migration.

## Error Handling And Observability
No new runtime path. No metric, log, or error-handling change. The
`PhotoEnrichmentService` metrics and the on-demand photo-resolve path
(`app/routers/internal_router.py`) are untouched.

## Test Plan
# bdd-exempt: pure deletion of unreachable/unreferenced code, no observable behavior change

Pytest unit tests:
- Full suite green (`make test-unit`) after removing only the tests that
  exclusively pin deleted members.
- Import/wiring checks still covered by existing suites: container
  construction (fixtures across `tests/test_handlers.py`,
  `tests/test_rds_repository.py`) and `app.services` imports exercise the
  trimmed `__init__.py`.
- `tests/test_handlers.py` minified-response assertions must pass unchanged —
  they are the equivalence guard for the `_transform` edit (venue photos
  truncated to 2, `venue_reviews`/`venue_menu` absent/None as today).
- Add one focused regression only if a gap appears while deleting: a
  `_transform` minified-shape test asserting `venue_reviews is None`,
  `venue_menu is None`, and 2-photo truncation, if no existing test already
  asserts it.

Manual or integration checks:
- `python -c "import main"` (or the equivalent app-startup smoke used in CI)
  to prove no lingering import of deleted names.
- Full BDD suite green (`make test-bdd`) — the discovery-hardening geofence
  feature must still pass, since its steps build a `SimpleNamespace` rather
  than `Settings`.

## Acceptance Criteria
- All members listed in Evidence are gone; `grep -rn` for each deleted name
  over `app/`, `main.py`, and `tests/` returns no hits (except the two kept
  doc comments, reworded).
- `/v1/venues/nearby` minified and verbose response bodies are byte-identical
  before/after on the same seeded data (existing handler tests + BDD serve as
  the equivalence check).
- Full pytest and BDD suites green with no test skipped or weakened other
  than deletions of tests whose only subject was deleted code.
- No config key other than the eight `*_on_startup` fields is touched;
  `config.example.json` parses and matches `Settings`.

## Rollback
Pure-deletion PR: `git revert` restores everything; no data, schema, or
Redis-state cleanup exists to undo. No deploy-order constraint.

## Open Questions
- **Were reviews/menu meant to appear in verbose responses?** The comments at
  `venue_handler.py:391,451` ("only load for verbose/detail mode") describe
  behavior the early return at 289-291 makes impossible — verbose responses
  return raw merged venues without reviews/menu enrichment. If the original
  intention was that verbose/detail mode serves `venue_reviews`/`venue_menu`,
  this is a latent unmet feature, and the branches should be *fixed* (moved
  before the early return) rather than deleted. Deleting is only correct if
  minified-only serving is the accepted contract (vibes_bot reads Redis
  directly for detail data today). Needs a product/owner call.
- **`get_venue_photos_fresh` (redis_venue_dao.py:673-688):** production-dead
  in cs-server, but it is the read-back contract for a *live* write path
  (`set_venue_photos_fresh`, whose consumer is vibes_bot via raw Redis) and
  `tests/test_photo_resolve.py` uses it in ~9 assertions. Delete it and have
  those tests read the raw `venue_photos_fresh_v1:{id}` key, or keep it as
  the documented key-format read-back helper? (Plan default if unanswered:
  keep it, deviating from the assessment on this one method.)
- **`run_photo_enrichment_job` (main.py:147-172):** the Job 5 comment
  (446-455) says the wrapper "remain[s] intact but dormant" — an explicit,
  documented decision. Confirm deleting the wrapper (service method stays,
  comment updated) does not conflict with an intent to re-schedule it later;
  re-adding the ~26-line wrapper is trivial if so.
