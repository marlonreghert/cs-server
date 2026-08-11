# Add-Venue Instagram Discovery

## Branch
feature/add-venue-instagram-discovery

## Goal
Attach a venue's Instagram handle at the moment it is added, instead of leaving
it to a cascade run that nothing schedules. Every add that creates or newly
links a venue must attempt discovery inline, must still succeed when no handle
is found, and must tell the operator which of those happened in the add
response itself.

## Non-goals
- Changing the cascade's scoring weights, thresholds, or source order. The
  fitted `PROVENANCE_WEIGHT` values are load-bearing and stay exactly as they
  are.
- Adding a scheduled cascade job. This plan closes the add-time gap only; the
  whole-catalogue run stays admin-triggered.
- Re-running discovery for an `already_exists` short-circuit. An add that
  resolves to a venue we already hold performs no discovery.
- Any mobile change. `instagram.handle` is already in the projection map, so a
  handle attached here reaches serving and the app with no downstream work.
- Backfilling handles for venues added before this ships.

## Evidence
- `app/handlers/add_venue_handler.py:326` — `_finalize_created_venue` is the
  shared success tail for all three create paths (inline BestTime create,
  timeout recovery, Google-only mint). It calls `_enrich_from_google`, caches
  week_raw, saves the address cache, and returns the body. It never touches
  Instagram.
- `app/services/instagram_cascade_service.py:338` — `discover(venue_id, config)`
  is the per-venue entry point, already config-driven and already degrade-safe
  ("Nothing here fails a venue").
- `main.py` — there is **no scheduled cascade job**. `run_instagram_enrichment_job`
  wraps the older `instagram_enrichment_service`, and
  `instagram_enrichment_enabled` defaults to `False`. The cascade is reachable
  only through `app/routers/admin_trigger_router.py:164`. A venue added today
  therefore gets a handle only if a human remembers to fire the trigger.
- `app/services/redis_projection_service.py:67` — `"instagram.handle"` is in the
  projection map, so persistence via `set_venue_instagram` is all that is needed
  for the handle to reach serving.
- `app/services/instagram_handle_sources.py:42` — `PAID_SOURCES = {apify_search,
  google_search}`. The free sources are `google_website`, `archived_gmaps_website`,
  and `venue_website`.
- `app/services/instagram_cascade_adapters.py:275` — `GoogleSearchInstagramSource._query`
  already builds `[venue_name, neighborhood or "Recife", "instagram"]` and takes
  the first Instagram profile link, which is the discovery path the developer
  measured as strongest. It is "paid" only because it reaches Google through the
  `apify~google-search-scraper` actor; we hold no Google-search API key.
- `app/services/instagram_cascade_service.py:60-69` — the comment on
  `PROVENANCE_WEIGHT[SOURCE_GOOGLE_SEARCH] = 0.20` records that
  `0.20 + NAME_WEIGHT(0.40) = 0.60` sits below the production bar of 0.65, so a
  `google_search` candidate **cannot be accepted without the judge at any name
  similarity**. Two of the first five real results were plausible but wrong (a
  public square, a monastery in another city).
- `app/config.py:296,308,316` — `instagram_cascade_enabled=True`, but
  `instagram_judge_enabled=False` and `instagram_google_search_enabled=False`.
  Both are off in the current default configuration.
- `app/container.py:50` — `_build_google_search_source()` returns `None` unless
  `instagram_google_search_enabled` **and** an Apify token are present, so the
  tier is not wired today.
- `app/services/batch_add_service.py:52` — `_classify` maps an `AddVenueOutcome`
  to a batch row dict; `BatchAddService` calls `handler.add()`, so the batch path
  inherits any hook placed in the handler.
- vibes_bot `app/admin/routes.py:864` — `ADD_VENUE_PROXY_TIMEOUT_SECONDS = 90`
  bounds how long the admin panel will wait for this endpoint.

## Current Behavior
`POST /admin/venues/by-address` reserves a monthly slot, creates the venue on
BestTime (or recovers a timeout, or mints a Google-only row), Google-enriches it
inline, and returns `{status, venue_id, venue_name, venue_address, venue_lat,
venue_lng, source}`. Instagram is never consulted. The venue is persisted with no
`VenueInstagram` record at all, and because nothing schedules the cascade, that
record is typically still absent days later. The operator has no signal either
way — the response is identical whether the venue has an Instagram presence or
not.

## Desired Behavior
After a venue is created or newly geo-linked and Google enrichment has run, the
handler must run the Instagram cascade for that venue with an add-time
configuration, and must fold the outcome into the response.

The add-time configuration must:

- enable the three free sources (`google_website`, `archived_gmaps_website`,
  `venue_website`);
- enable `google_search` (Apify-backed) — the strongest discovery path;
- keep `apify_search` (the paid Instagram user search) **disabled**;
- enable the judge, because `google_search` cannot clear the accept bar without
  it.

The add must never fail because discovery failed. A source that raises, a
deadline that expires, an unconfigured cascade, an unconfigured judge — each
degrades to "no handle" and the venue is still created and returned `201`.

The response must carry an `instagram` object on every created/newly-linked
outcome so the operator learns what happened without opening another tool.

## Implementation Approach

### 1. Per-source enablement in the cascade
`_source_enabled` currently reads:

```python
if source in PAID_SOURCES and config.get("tier_apify_search_enabled") is False:
    return False
return config.get(f"tier_{source}_enabled", True) is not False
```

`tier_apify_search_enabled` doubles as the master paid switch *and* as
`apify_search`'s own switch, so there is no way to run `google_search` while
`apify_search` is off — which is exactly the add-time shape. Change it so an
explicit per-source key wins, falling back to today's master behavior:

```python
explicit = config.get(f"tier_{source}_enabled")
if explicit is not None:
    return explicit is not False
if source in PAID_SOURCES and config.get("tier_apify_search_enabled") is False:
    return False
return True
```

This is backward compatible in the way that matters: an operator run passing
only `tier_apify_search_enabled: false` still disables **both** paid sources and
stays the zero-cost run that `app/config.py:294` promises. The add-time config
opts `google_search` back in explicitly.

### 2. Do not poison the not-found cache from a partial run
`_finalize` persists `status="not_found"` when nothing is found, and `discover`'s
freshness gate then skips that venue for `instagram_not_found_cache_ttl_days`
(7). Because the add-time run deliberately skips `apify_search`, a negative
result written here would block the only tier that was never tried from ever
running for that venue in the next full cascade.

Add a `suppress_not_found_cache` config flag, honored in `_finalize`/`_persist`:
when set, a `not_found` outcome is reported to the caller but **not** persisted.
The add-time config sets it. A found or low-confidence result persists normally.

### 3. Hook the handler
Add an optional `instagram_cascade_service` dependency to `AddVenueHandler`
(absent → discovery is skipped and reported as `skipped`, per the repo's
dependency-aware guardrail). Call it from `_finalize_created_venue` **after**
`_enrich_from_google`, because that call is what populates the vibe row's
`website_uri` that the free `google_website` tier reads.

Also call it from the `_geo_fallback` `was_new` branch, which persists a venue
without passing through `_finalize_created_venue`.

Wrap the call in `asyncio.wait_for` with a configurable deadline. The vibes_bot
proxy gives the whole add 90s and the BestTime create can already consume most
of it; the profile probe is blocked in production and burns its full 10s per
candidate, so an unbounded cascade would turn a successful add into a 504 for
the operator. On deadline the venue is already persisted — only the handle is
lost, and the response says so.

The whole call is wrapped so that no exception can escape into the add path.

### 4. Response contract
Append an `instagram` object to the `201` created bodies and to the
`matched_via_geo_fallback` body when `newly_linked` is true:

```
"instagram": {
  "status": "found" | "low_confidence" | "not_found" | "timeout" | "skipped" | "error",
  "handle": "<handle>" | null,
  "url": "https://instagram.com/<handle>" | null,
  "source": "google_website" | "venue_website" | "google_search" | ... | null,
  "confidence": 0.0-1.0
}
```

Additive only — no existing field changes name, type, or meaning, so a caller
that ignores it behaves exactly as today. `already_exists` bodies are unchanged.

### 5. Batch rows
Extend `_classify` to copy the `instagram` object onto the batch row result for
every created/geo-linked outcome, so a bulk run's triage shows which rows landed
without a handle. `BatchAddService` needs no other change — it already calls
`handler.add()`.

## Data, Config, And API Impact

**API (additive):** the `instagram` object described above on
`POST /admin/venues/by-address` and on batch job rows from
`GET /admin/venues/batch/{job_id}`.

**Persistence:** none new. Discovery writes through the existing
`set_venue_instagram`, the same call the cascade already makes, so Redis key
shapes and the `instagram.handle` projection entry are untouched.

**Config — new settings:**
- `add_venue_instagram_enabled: bool = True` — kill switch for the whole hook.
- `add_venue_instagram_deadline_seconds: float = 25.0` — the inline budget.

**Config — settings that must be turned on for the feature to do anything:**
- `instagram_google_search_enabled` must be `true` **and** an Apify token must be
  present, or `_build_google_search_source()` returns `None` and the strongest
  tier is silently absent.
- `instagram_judge_enabled` must be `true`, or `_build_instagram_judge()` returns
  `None` and every `google_search` candidate is capped below the accept bar by
  construction.

**Side effect to accept deliberately:** `instagram_judge_enabled` is global, so
turning it on also lets the admin-triggered whole-catalogue run adjudicate. Keep
that run's behavior unchanged by having the trigger pass `judge_enabled: false`
unless the operator opts in, so LLM spend stays confined to add-time until
someone asks for otherwise.

**Cost:** one Apify Google-search actor call plus at most a few judge
adjudications per newly added venue — a fraction of a cent each, roughly $1 per
300-row batch. No BestTime credit and no additional Google Places call.

## Error Handling And Observability
Every failure mode degrades to a created venue with no handle, never to a failed
add: cascade unconfigured → `skipped`; deadline expired → `timeout`; any
exception → `error`, logged with venue id and exception type. `discover` is
already internally degrade-safe per source, so a single dead source does not end
the attempt.

New metric `ADD_VENUE_INSTAGRAM_TOTAL` labelled `result` over
`found|low_confidence|not_found|timeout|skipped|error`, incremented once per add
that attempts discovery. This is the series that answers "are new venues
actually getting handles" without reading logs, and the `timeout` label is the
one that tells us the deadline is set wrong.

Existing cascade metrics (`INSTAGRAM_CASCADE_RESULTS_TOTAL`,
`INSTAGRAM_CASCADE_TIER_ATTEMPTS_TOTAL`, `INSTAGRAM_CASCADE_PAID_CALLS_TOTAL`,
`INSTAGRAM_JUDGE_TOTAL`) continue to fire from inside the cascade, so add-time
paid calls and judge verdicts are already attributable.

Log one line per add at INFO with the venue id, resolved status, source, and
confidence. Never log the raw search payload.

## Test Plan
Feature file: `tests/bdd/api/add-venue-instagram-discovery.feature`

Scenarios:
- A successful add whose venue has an Instagram link on its Google listing
  returns `201` with `instagram.status = "found"`, the handle, and
  `source = "google_website"`, and persists the handle.
- A successful add whose venue has no discoverable Instagram anywhere returns
  `201`, creates the venue, and reports `instagram.status = "not_found"` with a
  null handle — the add is not failed.
- An add whose handle is found only by the Google-search tier and confirmed by
  the judge returns `instagram.source = "google_search"` and `status = "found"`.
- An add whose Google-search candidate is rejected by the judge returns the
  venue created with `instagram.status` not equal to `"found"` and no handle
  attached.
- Discovery that exceeds the deadline still returns `201` with the venue created
  and `instagram.status = "timeout"`.
- Discovery that raises returns `201` with `instagram.status = "error"` and the
  venue created.
- An add that short-circuits to `already_exists` performs no discovery and its
  body carries no `instagram` object.
- A newly-linked geo-fallback outcome carries the `instagram` object; a
  geo-fallback outcome that linked an existing venue does not.
- The add-time run never attempts the `apify_search` tier.
- A not-found add-time run does not write a negative cache entry, so a
  subsequent full cascade run for the same venue still consults its sources.
- A batch-add job row carries the `instagram` object for each created row.

Pytest unit tests:
- `tests/test_instagram_cascade.py` — `_source_enabled` matrix: explicit
  per-source key wins; `tier_apify_search_enabled: false` alone still disables
  both paid sources (the zero-cost-run guarantee).
- `tests/test_instagram_cascade.py` — `suppress_not_found_cache` prevents the
  `not_found` persist while leaving `found`/`low_confidence` persists intact.
- `tests/test_add_venue_handler.py` — the hook runs after `_enrich_from_google`;
  is skipped when the service is absent; is skipped on `already_exists`; and
  never propagates an exception or a deadline into the add outcome.
- `tests/test_add_venue_handler.py` — the response body's `instagram` object is
  shaped correctly for each status, and existing fields are untouched.
- `tests/test_batch_add_service.py` — `_classify` carries `instagram` onto
  created and geo-linked rows.

Manual or integration checks:
- Confirm in a deployed environment that `INSTAGRAM_GOOGLE_SEARCH_ENABLED` and
  `INSTAGRAM_JUDGE_ENABLED` are set, and that `_build_google_search_source()` and
  `_build_instagram_judge()` both return a service at startup — the log lines
  `[Container] Instagram Google-search tier initialized` and the judge's
  equivalent are the proof. Without them the feature ships inert.
- Add one real venue through the admin panel and confirm the response carries
  `instagram`, the handle appears under the venue's Redis key, and the add
  completes well inside the 90s proxy timeout.

## Acceptance Criteria
- An add that creates or newly links a venue attempts Instagram discovery
  exactly once and returns an `instagram` object describing the outcome.
- A venue with no discoverable Instagram is still created and returned `201`.
- No discovery failure, exception, or deadline can turn a successful add into an
  error response.
- The add-time run never calls the `apify_search` tier.
- An add-time `not_found` does not write a negative cache entry.
- An operator run passing only `tier_apify_search_enabled: false` still performs
  zero paid calls.
- `already_exists` responses are byte-for-byte unchanged.
- `ADD_VENUE_INSTAGRAM_TOTAL` reports every add-time outcome.
- Batch job rows carry the `instagram` object for created and geo-linked rows.

## Open Questions
None.
