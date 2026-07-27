# Photo archive: pluggable sources (Apify Google Maps extractor)

## Branch
feature/archive-source-registry

## Goal

Make the photo archive's **source** a first-class, extensible choice, and add
`apify_gmaps_extractor` as the second one.

Two things follow from "extensible" and both are the point of this plan:

1. Each source brings **its own config fields and its own cost model**. Google
   bills per photo request; Apify bills per place scraped. A single cost formula
   cannot describe both.
2. cs-server **publishes** the source catalog — id, label, availability, and the
   extra config each source needs — so adding `instagram_posts_apify` later is a
   backend change plus a registry entry, with no admin-UI work.

## Non-goals

- **Serving archived images.** `media/` stays internal-use only.
- **Implementing `instagram_posts_apify`.** The registry must make it cheap to
  add; this plan does not add it.
- **Changing the archive layout, skip semantics, or eligibility.** Those are
  source-independent and stay exactly as they are.
- **Deciding the legal posture of scraping.** Recorded in Open Questions; the
  operator chooses the source per run.

## Evidence

- `app/services/venue_photo_archive_service.py` — `SUPPORTED_SOURCES` is a
  one-element tuple and `_fetch_photos` calls
  `google_places_client.get_place_photos` directly. The source is validated but
  never dispatched on.
- `app/api/google_places_client.py:509` `get_place_photos` is **1 Place Details
  + N photo-media calls per venue** (Details at `:59-63`, then the per-photo
  loop at `:78-85`). So a venue costs **N+1** billed calls, not N.
- `estimate()` computes `calls = after_skip * photos_max`, which **omits the
  Place Details call** — a 33% undercount at 3 photos/venue, in a control whose
  whole job is to keep spend under the documented $10/month gate.
- `app/api/apify_gmaps_extractor_client.py` — already wired, already used for
  menu photos. `fetch_venue_menu_photos(search_query, menu_keywords, max_photos)`
  (`:78`) starts a `compass~google-maps-extractor` run with
  `{searchStringsArray, maxImages, language, includeImages, scrapeImageAuthors}`
  (`:104-110`), polls it, and reads the dataset. It filters to menu categories
  (`_extract_menu_photos`, `:171`) — the archive needs **all** photos, so it
  needs a sibling method, not a parameter.
- `_start_run` raises `ApifyCreditExhaustedError` on HTTP 402 (`:293-297`) —
  the budget-exhausted signal the archive must surface rather than swallow.
- `settings.apify_api_token` exists (`app/config.py:257`) and
  `docker-compose.yml:72` already passes `APIFY_API_TOKEN` to cs-server — but
  **the deploy workflow never sets it and no such GitHub secret exists**, so it
  is empty in production today.
- Apify pay-per-event pricing (fetched 2026-07-27): **$0.004 per place scraped**,
  plus **$0.002 per place** for the additional-details add-on that carries
  images; free plan credit is **$5/month**. Per-image charges exist but are not
  published, so any estimate is a floor.

## Current vs desired behavior

| Situation | Current | Desired |
|---|---|---|
| Operator picks a source | only `google_photos` exists | picks from a published catalog |
| A source needs its own settings | impossible | source declares its own config fields |
| Cost estimate | one formula, and it undercounts Google by 33% | per-source model, Google counts the Details call |
| A source's dependency is missing | n/a | source is advertised as unavailable, with the reason |
| Adding a future source | edit the service and the admin UI | add a registry entry |
| Apify credits exhausted mid-run | n/a | run stops cleanly and says so |

## Implementation approach

### 1. Source registry

A module-level `ARCHIVE_SOURCES` mapping id → descriptor:

- `label`, `description`
- `config_schema`: the **extra** fields this source adds, each with name, type,
  default, and help. Shared fields (caps, eligibility, path, skip) stay on the
  common config and are not repeated per source.
- `requires`: the container attribute that must be wired for the source to be
  usable, plus the reason to show when it is not.
- `fetch`: `async (client, venue, cfg) -> list[photo dict]` returning the
  existing shape (`url`, `author_name`, `photo_name`) so `_store_photo` is
  untouched.
- `estimate_calls(venues, cfg) -> (units, unit_label)` and
  `unit_cost_usd(cfg)`: the per-source cost model.

`SUPPORTED_SOURCES` derives from the registry so validation stays honest.

### 2. `google_photos` descriptor

Behavior unchanged. Its cost model becomes `venues * (1 + max_photos)` at
`google_photo_cost_per_1k_usd` — **fixing the undercount**. Extra config: none.

### 3. `apify_gmaps_extractor` descriptor

New `fetch_venue_photos(search_query, max_photos)` on the Apify client: the same
run shape as the menu method but **without category filtering**, returning every
image with its author attribution, normalised to the common photo dict.

Venues are matched by `search_query` built from venue name + address (the
extractor is a search API, not a place-id API). A venue whose search returns
nothing is counted `no_match` — the source-neutral analogue of `no_place_id`.

Extra config: `language` (default `pt-BR`), and `photo_pool` — how many images
to ask the actor for, since the actor charges per place, not per image, so a
larger pool is nearly free and improves selection.

Cost model: `venues * (place_scraped + place_details)` — two settings,
defaulting to `0.004` and `0.002`, with the estimate's caveat stating the
per-image charge is unpublished and the figure is a floor.

`ApifyCreditExhaustedError` ends the run cleanly with `credit_exhausted` in the
summary, exactly as a Google ledger exhaustion would.

### 4. Publishing the catalog

`GET /admin/jobs` gains `sources` for the archive job: each id, label,
description, `available`, `unavailable_reason`, and `config_schema`. The admin UI
renders from this, so a new source needs no UI change.

### 5. Secret plumbing

The deploy workflow writes `APIFY_API_TOKEN=${{ secrets.APIFY_API_TOKEN }}` into
the EC2 `.env`, alongside the existing keys. The **value is never in the repo,
in a plan, or in a log** — only the secret reference. The operator adds the
secret; until then the source advertises itself unavailable rather than failing
a run.

## Data, config, and API impact

- **API (admin only):** `GET /admin/jobs` gains `sources` on the archive job.
  Run config gains an optional `source_config` object for per-source fields.
  No public API change.
- **Persistence:** none. No migration. Same S3 layout — `source=` already
  partitions the archive, so a second source lands beside the first.
- **New settings:** `apify_place_scraped_cost_usd` (0.004),
  `apify_place_details_cost_usd` (0.002), `apify_gmaps_language` (`pt-BR`),
  `apify_gmaps_photo_pool` (20).
- **Cost:** the estimate becomes *more* conservative for Google (+1 call/venue)
  and gains a model for Apify. No scheduled execution is added.

## Error handling and observability

| Failure | Behavior |
|---|---|
| Source unknown | 400 before any spend |
| Source's dependency unwired | 400 naming what is missing; advertised unavailable in the catalog |
| Apify 402 credits exhausted | run stops, `credit_exhausted` in the summary, counted |
| Apify run times out / actor fails | venue counted failed, run continues |
| Search returns no match | counted `no_match`, no retry, not an error |
| Per-image charge unknown | stated in the estimate's caveat |

Existing `MEDIA_ARCHIVE_*` metrics already carry a `source` label, so both
sources report side by side with no metric changes. Add
`MEDIA_ARCHIVE_VENUES_TOTAL{result="no_match"}` and a `credit_exhausted` outcome.

## Test plan

Feature file: `tests/bdd/enrichment/archive-source-registry.feature`

Scenarios:
- The archive job advertises both sources with their labels and config schemas.
- A source whose dependency is unwired is advertised unavailable with a reason.
- Choosing an unknown source is rejected before any spend.
- Choosing a source whose dependency is missing is rejected before any spend.
- A run with `apify_gmaps_extractor` fetches photos through the Apify client and stores them under `source=apify_gmaps_extractor`.
- The Apify source makes no Google call, and the Google source makes no Apify call.
- Exhausted Apify credits stop the run cleanly and are reported.
- A venue the extractor cannot match is counted and does not fail the run.
- The Google estimate counts one Place Details call per venue in addition to the photo calls.
- The Apify estimate is priced per place, not per photo.
- Both estimates state that they are upper bounds.
- Per-source config is passed to its own source and validated.

Pytest unit tests:
- Registry integrity: every source declares label, schema, fetcher and cost model.
- Google cost arithmetic including the +1 Details call, at the boundaries.
- Apify cost arithmetic (place_scraped + details), and that photo count does not change it.
- Apify photo normalisation to the common dict, including a missing author.
- Search-query construction from name + address.
- `SUPPORTED_SOURCES` stays derived from the registry (a new entry needs no second edit).

## Acceptance criteria

- Both sources are selectable, and each is used for its own run with no
  cross-calling.
- The Google estimate no longer undercounts; the Apify estimate is per place.
- The catalog is published by cs-server, so adding a source needs no UI edit.
- An unavailable source is visible as unavailable, and unusable, before spending.
- Apify credit exhaustion is a clean stop, not a stack trace.
- The Apify token reaches production only via a GitHub secret; no value in the
  repo, the plans, or the logs.
- The archive layout, skip semantics, and eligibility are unchanged.

## Open questions

- **Legal posture (operator's call, not a code question).** The Places API is a
  licence with terms; scraping the same content is not. This repo already
  accepted Apify scraping for menu photos, so the source choice is a consistency
  decision the operator makes per run. Recorded, not resolved here.
- The Apify **per-image** charge is unpublished. Estimates are floors; a small
  real run is the only way to measure it.
