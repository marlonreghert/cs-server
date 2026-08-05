# Event Venue Targeting — which venues are worth crawling for events

## Branch
feature/event-venue-targeting

## Goal
Produce a durable, admin-tunable answer to "which venues should we crawl for
events", computed in two stages: a **free category gate** over the whole
servable catalog, then a **bounded evidence gate** over the top N of what
survives. Expose that answer as a reusable run-targeting mode so every event job
that follows aims at the same set.

## Non-goals
- **Extracting events.** This plan decides *where to look*, not what is found.
  (`260804_instagram-event-extraction.md`.)
- **Promoter accounts.** Those belong to no venue and are targeted differently.
  (`260804_instagram-promoter-events.md`.)
- **Changing venue eligibility or the serving view.** `venue_eligibility.py`
  decides whether a venue is shown to users at all; this decides whether we
  crawl it for events. A venue can be perfectly servable and a hopeless event
  candidate, and confusing the two would let a targeting decision hide a real
  bar. Nothing here writes `deleted_at` or touches the serving projection.
- **Any Instagram scrape.** The evidence gate reads data already paid for.

## Evidence

**Categories are already resolved and already admin-tunable.**
`app/models/venue_category.py` maps Google/BestTime types onto display
categories and merges an admin override from `admin_config:venue_category_map`
at serve time. The category vocabulary already distinguishes the venues the
operator described: `NIGHTCLUB`, `LIVE_MUSIC`, `EVENT_VENUE`, `KARAOKE`,
`CASINO`, `ENTERTAINMENT` on one side, `RESTAURANT`, `BAKERY`, `COFFEE_SHOP`,
`BUFFET` on the other. A category allow-list is therefore a config key, not a
classifier.

**The admin-config pattern is established.** `admin.admin_config` in RDS with a
Redis mirror, read through `RdsVenueStore.get_admin_config` /
`upsert_admin_config`, with hardcoded defaults that survive a bad write — used
by `venue_category_map`, `venue_eligibility` and `venue_geofence`. A new key
follows it exactly.

**A priority ordering exists, but the obvious function is the wrong one.**
`RdsVenueStore.list_servable_venue_ids_by_priority(limit)` orders servable
venues by `priority`, then reviews, rating and id — the ranking this plan wants.
But since `0021_venue_source` it also filters `venue_source <> 'google_only'`,
and its docstring says why: it "backs the bounded live/weekly refresh" and a
google-only venue "carries no BestTime id to query". That exclusion is right for
BestTime and **wrong for event targeting** (see §B below). The unbounded
`list_servable_venue_ids()` reads `serving.eligible_venue` and does **not**
exclude them, so using the two together would gate one way and evaluate another.

**The vibe profile already knows who plays live.** `app/models/taxonomy.py`
holds `music_format` (`DJ`, `Banda ao vivo`, `Roda de samba`, `Karaokê`,
`Open mic`) and `estilo_do_lugar` (`Balada`, `Club`, `Cultural / alternativo`).
Those labels are already derived and stored — reading them costs nothing.

**Run targeting is a closed set of three modes.**
`venue_photo_archive_service.py:89-92` defines `ELIGIBILITY_MODES` as `all`,
`venue_ids`, `point_radius`, validated in `parse_config`. A fourth mode is the
natural seam for pointing any run at the event candidates.

**Bounded, operator-triggered runs are the house cost posture.** §3 of
`docs/venue-retrieval-storage.md`: caps always apply, defaults are small, there
is no cron, and a config error must cost nothing because validation precedes
every fetch.

## Current Behavior
Nothing in the repo has an opinion about which venues host events. A crawl can
target all venues, an explicit id list, or a point and radius. Running the
Instagram jobs against the whole servable catalog is the only way to cover the
event venues, which is also the most expensive possible way to do it.

## Desired Behavior
1. Evaluate every servable venue against an admin-tunable category allow-list,
   free of charge, and record whether it passed.
2. Promote a venue that fails the category gate but whose vibe profile shows a
   live-music or club format — the restaurant that runs a weekly samba night —
   also free of charge.
3. Take the **top N** survivors by existing venue priority and run the evidence
   gate over them; N is a run config field with a small default.
4. Decide evidence from data already paid for: the count of `flyer`-classified
   photos and of captions carrying event markers within a lookback window.
5. Persist a tier per venue with the evidence behind it and when it was
   computed.
6. Distinguish "evaluated and rejected" from "never evaluated". A venue the
   bounded run never reached must not read as a venue with no events.
7. Offer an `event_candidates` targeting mode that resolves to the confirmed
   set, usable by any run that accepts an eligibility block.
8. Re-running must be idempotent and must be able to re-evaluate a venue whose
   evidence has gone stale.

## Implementation Approach

### A. Stage 1 — the category gate (free, whole catalog)
New admin config key `admin_config:event_candidate_categories`, holding an
allow-list of display categories plus the vibe-signal labels that override a
category rejection. Defaults ship in code so a missing or malformed config can
never empty the candidate set — the same fallback discipline
`venue_eligibility.py` uses.

Default allow-list: `NIGHTCLUB`, `LIVE_MUSIC`, `EVENT_VENUE`, `KARAOKE`,
`CASINO`, `ENTERTAINMENT`, `PUB`, `BAR`, `COCKTAIL_BAR`, `BREWERY`.
Default excluded: `RESTAURANT`, `BUFFET`, `BAKERY`, `COFFEE_SHOP`, `FOOD_DRINK`,
`WINERY`, `PARK`, `OTHER`.

Category is resolved with the existing `resolve_category`, so an operator who
retunes `venue_category_map` retunes this at the same time and in one place.

**The vibe override is why a category-only gate is not enough.** The obvious
objection to an allow-list is the restaurant with a live band on Thursdays. That
venue is already labelled `Banda ao vivo` or `Roda de samba` in its vibe
profile, and reading that label costs nothing, so the exception is caught for
free rather than argued about.

An `OTHER` venue is **not** a candidate by default, which is the opposite of the
`venue_eligibility` block-list posture — and deliberately so. There, an unknown
venue must stay visible because hiding a real bar is irreversible; here, an
unknown venue merely goes uncrawled, which costs nothing and reverses on the
next run. The two modules answer different questions and must not share a
default.

### B. Stage 2 — the evidence gate (bounded to top N)
Order the category survivors by priority and take `max_evidence_venues` (default
small). For each, score from data the repo already holds:

- how many of its archived photos classified as `flyer`
  (`260804_instagram-media-archive.md`) within `lookback_days`;
- how many of its cached `instagram.posts` captions match event markers — an
  explicit date pattern, a weekday-plus-time pattern, or a ticketing/lineup term
  (`ingressos`, `line-up`, `open bar`, `sympla`, `shotgun`, `pré-venda`).

A venue clearing `min_evidence_posts` is `evidence_confirmed`; one evaluated and
falling short is `evidence_rejected`; one the bound never reached stays
`category_candidate`.

**The ordering needs its own selection, and google-only venues must be in it.**
Add `RdsVenueStore.list_event_candidate_ids_by_priority(limit)` — the same
`priority, reviews, rating, venue_id` ordering over `serving.eligible_venue`,
**without** the `venue_source <> 'google_only'` filter. A new method rather than
a flag on the existing one: that filter protects BestTime refresh from being fed
ids that do not exist there, and a boolean someone can pass wrong is a worse
guard than two functions that cannot be confused.

A `google_only` venue is one BestTime could not forecast. In this catalog that
skews hard toward the small independent club and event space — precisely the
venue most likely to run events, and precisely the one the BestTime-shaped
filter would drop. The failure would also be silent: the function returns a
shorter list, not an error, so the evidence gate would look like it ran
correctly while never seeing the best candidates. Their `priority` may be unset,
so they tie-break on reviews and rating, which Google supplies.

**The evidence gate spends nothing on a model by default.** Both inputs were
already paid for — the flyer label came free with a classification call the
archive run already makes, and the captions are already in RDS. A deterministic
scorer is also auditable, which an LLM verdict is not: an operator can be shown
the three captions that promoted a venue. An optional LLM adjudication for the
ambiguous middle is left as a setting, defaulting off, so the cheap path is the
one that runs unless someone chooses otherwise.

**A venue with no Instagram handle is `unevaluated`, not rejected.** There is no
evidence either way, and recording "no events here" from an absence of data is
how a coverage gap becomes a permanent, invisible fact.

### C. Persistence
Migration `0022_events_schema` (revising `0021_venue_source`): create schema
`events` and table
`events.venue_event_profile`:

| column | meaning |
|---|---|
| `venue_id` | PK, FK `venues.venue` |
| `tier` | `unevaluated` \| `category_candidate` \| `evidence_confirmed` \| `evidence_rejected` \| `excluded_category` |
| `category_pass` | bool — did stage 1 pass |
| `category_reason` | which rule decided it (category name, or the vibe label that overrode) |
| `evidence_score` | integer |
| `evidence_sample` | jsonb — the counts and a few matched excerpts, so a verdict is explainable |
| `evaluated_at` | nullable timestamptz; NULL means stage 2 never ran |
| `updated_at` | |

`evaluated_at` being nullable is the mechanism behind requirement 6: "we never
looked" is a NULL, not a tier value that can be misread as a judgement.

`tier` is indexed — the targeting mode filters on it on every event run.

### D. The `event_candidates` targeting mode
Add `ELIGIBILITY_EVENT_CANDIDATES` to `ELIGIBILITY_MODES` and to `parse_config`.
It resolves to venues whose tier is `evidence_confirmed`, with an optional
`include_category_candidates` flag for a first run made before any evidence
exists. Every existing cap continues to apply on top — the mode narrows the
selection, it never widens past `max_venues`.

Placing this in the shared config parser rather than in an event-specific
service is the point: the archive job, the extraction job and anything later all
inherit it without each re-deriving the candidate set, and there is exactly one
definition of "an event venue" to change.

### E. The job
`event_targeting` in `JOB_REGISTRY`, config
`{max_evidence_venues, min_evidence_posts, lookback_days, recompute, dry_run}`.
`recompute` re-evaluates venues that already have an `evaluated_at` instead of
skipping them; without it a run only fills gaps, which is the cheap default.
`dry_run` reports the selection and the tier changes it would make and writes
nothing, matching the archive pipeline's dry-run contract.

No cron. Operator-triggered, like every other job in the registry.

## Data, Config, And API Impact
- **Migration:** `0022_events_schema` — new `events` schema, new
  `events.venue_event_profile`, index on `tier`.
- **DAO:** new `list_event_candidate_ids_by_priority(limit)`. Read-only,
  additive; the existing BestTime-scoped selections are untouched.
- **Admin config:** new key `admin_config:event_candidate_categories` (RDS row +
  Redis mirror), with in-code defaults.
- **Run config:** new eligibility mode `event_candidates` accepted by
  `parse_config`, valid for every job that takes an eligibility block.
- **API:** `GET /admin/events/targeting` (tier counts and a paged listing) and
  `POST /admin/trigger/event_targeting` via the existing trigger route. The
  admin-config CRUD route already handles arbitrary keys, so the allow-list is
  editable with no new endpoint.
- **Serving:** none. No Redis projection change, no venue response change. The
  app cannot observe this.

## Error Handling And Observability
A venue whose category cannot be resolved is `unevaluated` and logged, never
silently dropped. A malformed admin config falls back to the in-code defaults
and increments a counter — a bad write must not empty the candidate set and make
the next event run look like it found nothing.

Metrics:
- `event_targeting_venues_total{stage,verdict}` — `stage` is `category` or
  `evidence`.
- `event_targeting_config_fallback_total{reason}`.
- `event_candidate_venues` gauge by `tier` and `venue_source`. The second label
  is two values, so it costs nothing, and it answers the question this plan
  would otherwise leave unanswerable: whether the venues BestTime could not
  forecast are actually reaching the evidence gate, or are being dropped by a
  filter nobody remembers is there.

## Test Plan
Feature file: `tests/bdd/enrichment/event-venue-targeting.feature`

Scenarios:
- Pass a nightclub through the category gate and exclude a restaurant.
- Promote a restaurant whose vibe profile carries `Banda ao vivo`, proving the
  free exception path works.
- Apply an admin override that adds `RESTAURANT` to the allow-list and assert
  the next run includes it, with no code change.
- Fall back to the in-code defaults when the admin config is malformed, and
  assert the candidate set is non-empty.
- Evaluate only the top N by priority, and assert the venues past the bound stay
  `category_candidate` with a NULL `evaluated_at` — "never looked" is not
  "rejected".
- Evidence-evaluate a `venue_source='google_only'` venue that passed the
  category gate, proving the BestTime-scoped exclusion does not reach the event
  pipeline.
- Confirm a venue whose flyer count clears the threshold.
- Reject a venue evaluated with evidence below the threshold, and record the
  sample that justified it.
- Leave a venue with no Instagram handle `unevaluated`.
- Resolve `eligibility.mode = event_candidates` to exactly the confirmed set.
- Apply `max_venues` on top of `event_candidates`, proving the mode narrows and
  never widens.
- Skip already-evaluated venues without `recompute`, and re-evaluate them with
  it.
- Report the selection and write nothing under `dry_run`.

Pytest unit tests:
- The category gate across every category in `CATEGORIES`, including `OTHER`.
- `list_event_candidate_ids_by_priority` returns `google_only` venues while
  `list_servable_venue_ids_by_priority` still excludes them — asserted in one
  test over one fixture, so the two selections can never silently converge.
- A `google_only` venue with a NULL `priority` still orders deterministically.
- The vibe-signal override, including a venue with no vibe profile.
- The caption event-marker matcher: pt-BR date forms, weekday+time, ticketing
  terms, and a caption that must NOT match (a menu announcement, a holiday
  greeting).
- Evidence scoring at, just below, and just above the threshold.
- Tier transitions, including confirmed → rejected on recompute.
- `parse_config` accepts the new mode and still rejects unknown modes.
- Admin-config parsing: valid, empty, wrong type, unknown category name.

Manual or integration checks:
- Run against the prod-shaped catalog with `dry_run` and read the tier
  distribution; confirm the confirmed set is small enough that the runs which
  follow are affordable.

## Acceptance Criteria
- Every servable venue has a `venue_event_profile` row after a full run.
- A restaurant is excluded by category unless its vibe profile or the admin
  allow-list says otherwise.
- Exactly `max_evidence_venues` venues are evidence-evaluated, chosen by
  priority, and `google_only` venues are eligible to be among them.
- A venue never reached by the bound has `evaluated_at` NULL and is
  distinguishable from a rejected one in both the API and the metrics.
- `eligibility.mode = event_candidates` resolves to the confirmed set and honours
  `max_venues`.
- The evidence gate makes zero external API calls and zero model calls with the
  default settings.
- `make test-feature` and `make test-unit` pass.

## Open Questions
None blocking. `min_evidence_posts` and `lookback_days` ship as small defaults
and are run config; the first real run over the prod catalog is what calibrates
them, and the `evidence_sample` column exists so that calibration can be done by
reading verdicts rather than guessing.
