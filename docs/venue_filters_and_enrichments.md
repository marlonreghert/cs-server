# Venue Data — Filters & Enrichments (Current Flow)

**What this is:** an end-to-end map of every place venue data is *filtered* (rows
removed / hidden) or *enriched* (fields added) as it flows from external sources,
through **cs-server**, across the HTTP boundary, through the **vibes_bot**
pipeline, and out to the user.

**Scope notes**
- **DB cutover (RDS).** cs-server now has an **RDS Postgres system of record**;
  Redis becomes a **projection / read model**. This is **write-through** and
  gated by the `rds_enabled` flag (default `false` ⇒ Redis-only, today's
  behavior). The flag was not set in the env/compose I could see, so treat RDS as
  "implemented, cutover-gated," not necessarily live in prod. Crucially, this
  changes *where data is persisted*, **not** the filter/enrichment logic — and
  **serving is still Redis-only**. See [§1b](#1b-rds-system-of-record-the-db-cutover).
- **Co-location:** cs-server runs *only* on the vibes_bot EC2 host. vibes_bot
  reaches it over the internal Docker network at `http://cs-server:8080`
  (`Settings.CS_SERVER_URL`). They are never deployed apart.
- **Three datastores now.** cs-server **RDS** (truth) + cs-server **Redis #1**
  (venue projection + geo index); vibes_bot **Redis #2** (auth tokens, short-lived
  caches, and the favorites/hot-likes *read* projection). vibes_bot now **writes**
  favorites/hot-likes through cs-server's engagement API (no longer straight to
  Redis); it still **reads** them from Redis #2.

## Visual overview

Two diagrams. **Colors:** 🟥 red = filter (removes/hides venues), 🟩 green =
stored artifact/enrichment, cyan = API call / processing, 🟣 violet = **RDS
(system of record)**, 🟡 amber = **Redis (projection)**, indigo = vibes_bot.
Verbs and join keys are written **on the arrows**.

### A · Macro overview — the whole shape on one screen

![Macro overview](0_overview.png)

The new spine: external sources → cs-server **write-through** (RDS truth, then
project to Redis) → **serve from Redis only** → HTTP → vibes_bot pipeline →
user. Favorites/hot-likes and admin-config **write back** through cs-server APIs
(RDS truth → Redis mirror); vibes_bot keeps **reading** them from Redis. It also
surfaces, at a glance, the cs-server internals (ingest · enrichment family ·
eligibility gate · write-through · serve · engagement) and the **three places
venues get filtered** — the eligibility gate (ingest + serve) and the pipeline's
two major drops (no-busyness, open-now). Grouped stages only; the join keys and
individual artifacts live in the full-flow diagram below.

### B · The full flow — one big zoom-and-scroll diagram

![Full venue flow](1_full_flow.png)

Everything in one canvas (≈14000px wide — **zoom in / scroll** to read it, or
open the SVG). Ingestion, filtering and enrichment are intentionally **not split
apart** — they share inputs and all converge on the single write-through gateway.
What to trace:
- **BestTime → Google:** a venue's `name + address + coords` feed
  `places:searchText` (500 m bias) → `place_id` → `place details`.
- **Google → blocking a venue:** `google_primary_type` → `blocked_google_types`
  → eligibility soft-deletes (sweep) **and** hides at serve; the same type can
  **rescue** a name-keyword false positive when it resolves to a good category.
- **Write-through:** every writer (ingest, all enrichers, soft-deletes) goes
  through `VenueRepository` → **RDS first (raises on failure), then Redis**.
- **vibes_bot pipeline:** the two red hexagons (**no-busyness**, **open-now →
  unknowns dropped**) are where most venues disappear; pricing **overwrites
  `price_level` only when Google returns one**.

> Both PNGs are high-resolution (the full flow is ~14000px wide) — zoom to read
> the edge labels, or open the **SVG** for lossless zoom:
> [macro](0_overview.svg) · [full flow](1_full_flow.svg). Mermaid sources are in
> [diagrams/](diagrams/); regenerate with `docs/render_venue_flow.sh`
> (uses `npx mermaid-cli`; needs Node).

## 1b. RDS system of record (the DB cutover)

When `rds_enabled=true`, `VenueRepository` (a subclass of the Redis DAO) makes
every write **write-through**: persist to **RDS Postgres first** (the truth —
raises on failure), then **project to Redis** (the same DAO call that keeps the
`GEOADD` geo index). Reads/serving are **unchanged — Redis only**; RDS is never
on the read path.

- **RDS schemas:** `venues`, `besttime` (weekly + live), `google_places`
  (vibe_attributes with *promoted, indexed* `google_primary_type`/`google_place_id`,
  opening_hours, photos, reviews), `instagram`, `admin` (config + rejection_reason),
  `engagement` (favorites/hot-likes), `audit.enrichment_history`.
- **Never hard-deletes labels:** `delete_*`/soft-delete map to RDS
  `deleted_at`, and expensive derived labels get an **append-only**
  `audit.enrichment_history` row per write.
- **Projection service:** `rebuild_redis_from_rds()` reconstructs Redis (incl.
  geo index + live) for disaster recovery / warm-up; `backfill_rds_from_redis()`
  is the one-time import of the existing Redis dataset.
- **Engagement (carve-out):** vibes_bot writes favorites/hot-likes via
  `POST/DELETE /v1/favorites` and `/v1/hot-likes`; `EngagementService`
  **HMAC-pseudonymizes** the user id → RDS truth → Redis projection in the exact
  keys vibes_bot reads (`user_favorites:{id}`, `hot_likes:v1:{venue}`). Raw user
  ids never reach RDS.
- **Admin config (carve-out):** `AdminConfigService` validates → writes RDS
  (`admin.admin_config`) → **mirrors Redis** synchronously, so every runtime
  reader (eligibility/budget/etc.) keeps reading the Redis mirror unchanged.

> **"RDS is the truth" has three deliberate Redis-only edges** (a rebuild does
> **not** restore these):
> 1. **Monthly new-venue counter** (`venue_add_counter_v1:YYYY-MM`) lives in
>    Redis only — only the *limits* are admin-config (RDS+mirror).
> 2. **Live-forecast prune** — when the 5-min job drops a closed/unavailable
>    venue it deletes **Redis only**; the RDS `live_forecast` row lingers, so a
>    `rebuild_redis_from_rds()` can re-project a since-pruned snapshot.
> 3. **Photos** — current-value only (Google URLs expire): no history, no RDS
>    delete path; Redis keeps the refetch TTL.

**Legend** — used throughout:

| Tag | Meaning |
|-----|---------|
| 🟥 **F** | **Filter** — removes or hides venues (row count goes down) |
| 🟩 **E** | **Enrichment** — adds/derives fields (row count unchanged) |
| ♻️ | Reversible (serve-time hide; data not destroyed) |
| 🗑️ | Irreversible-ish soft-delete: `lifecycle_status=deprecated` (RDS `deleted_at` + Redis) |
| 🔑 | Requires an optional API key; **no-ops** (degrades gracefully) if missing |
| ⚙️ | Admin-tunable live (RDS `admin.admin_config` → Redis mirror; no redeploy) |

---

## 1. System topology

```mermaid
flowchart LR
    subgraph EXT["External data sources"]
        BT["BestTime API\n(discovery, inventory,\nlive + weekly forecast)"]
        GP["Google Places API\n(attributes, status, hours,\nreviews, photos)"]
        APIFY["Apify actors\n(IG handle search, IG posts,\nIG highlights, GMaps photos)"]
        OAI["OpenAI\n(GPT-4o / 4o-mini\nmenu + vibe vision)"]
        S3[("AWS S3\nmenu photos")]
    end

    subgraph EC2["vibes_bot EC2 host (single box, Docker Compose)"]
        direction LR
        subgraph CS["cs-server :8080  (RDS = system of record, Redis = projection)"]
            CSJOBS["APScheduler jobs\n(ingest + enrich + filters)"]
            CSWT["VenueRepository\n(write-through)"]
            CSDB[("RDS Postgres\nSYSTEM OF RECORD\nvenues/besttime/google/instagram\nadmin/engagement/audit")]
            CSRED[("Redis #1 (projection)\nserving keys + geo index")]
            CSSERVE["GET /v1/venues/nearby\n(serve from Redis only)"]
            CSENG["Engagement +\nadmin-config APIs"]
        end
        subgraph VB["vibes_bot :8000  (product API + LLM)"]
            VBPIPE["VenuePipeline.get_venues\n(20-step filter+enrich)"]
            VBAPI["POST /venues  (mobile)\nPOST /ask  (LLM bot)"]
            VBRED[("Redis #2\ntokens, favorites,\nhot likes, admin_config")]
        end
        CADDY["Caddy reverse proxy"]
    end

    USER["Mobile app / web client"]

    BT --> CSJOBS
    GP --> CSJOBS
    APIFY --> CSJOBS
    OAI --> CSJOBS
    S3 <--> CSJOBS
    CSJOBS --> CSWT
    CSWT -->|"1 · truth"| CSDB
    CSWT -->|"2 · project (geo)"| CSRED
    CSDB -. "rebuild" .-> CSRED
    CSRED --> CSSERVE
    CSSERVE -->|"HTTP verbose=false (minified)"| VBPIPE
    VBPIPE --> VBAPI
    VBRED <--> VBPIPE
    VBPIPE -. "fav / hot-like writes" .-> CSENG
    CSENG --> CSDB
    CSENG --> CSRED
    GP -. "live pricing" .-> VBPIPE
    VBAPI --> CADDY --> USER
```

**Data ownership at a glance** (with `rds_enabled=true`)

| Concern | Owner | Truth | Projection / read |
|---|---|---|---|
| Venue catalog, forecasts, attributes, photos, IG, menu, vibe profile | cs-server | **RDS** | Redis #1 (+ geo index) |
| Eligibility block-lists & other admin config | cs-server | **RDS** (`admin.admin_config`) | Redis #1 (`admin_config:*` mirror) |
| Discovery points; monthly **new-venue counter** | cs-server | **Redis #1 only** (counter not in RDS) | — |
| Favorites, hot-likes | cs-server (written via API) | **RDS** (`engagement.*`, pseudonymized) | Redis #2 (`user_favorites:*`, `hot_likes:*`) |
| Scoring weights, name blacklist, venue-type/vibe label maps, vibe modes | vibes_bot | Redis #2 (`admin_config`) | — |
| Auth tokens, short-lived caches (weather, pricing) | vibes_bot | Redis #2 | — |

---

## 2. cs-server — ingestion & write-time filters

How venues *enter* the catalog and which filters apply **at write time**. Source
of truth for discovery is BestTime; Google/Apify/OpenAI only enrich later.

```mermaid
flowchart TD
    START["Catalog refresh job\n(every 30 days)"] --> INV["Step 1: Inventory sync\nBestTime GET /api/v1/venues\n(no credit cost)"]
    INV --> BUDGET{"Monthly new-venue\nbudget remaining?\n(quota 500, reserve 10)"}
    BUDGET -- "no" --> STOP["Skip discovery\n(metric: skipped_due_to_monthly_cap)"]
    BUDGET -- "yes" --> DISC["Step 2: Discovery\nBestTime /venues/filter\nper discovery point ⚙️\n(VENUE_TYPES incl. OTHER)"]

    INV --> BORN1["🟥F born-deprecate\nhigh-confidence junk 🗑️\n(empty name / blocked type /\nhard keyword)"]
    DISC --> DEDUP["🟥F de-dupe\nby venue_id, then by name"]
    DEDUP --> BORN2["🟥F born-deprecate\nhigh-confidence junk 🗑️"]
    BORN2 --> UPSERT[("upsert_venue → Redis #1\n+ geo index")]
    BORN1 --> UPSERT

    UPSERT --> LIVE["Live forecast refresh\n(every 5 min)"]
    LIVE --> LIVEFILTER["🟥F drop/delete cache when\nstatus≠OK or busyness\nnot available (venue closed)"]
    UPSERT --> WEEK["Weekly forecast refresh\n(Sun 00:00 Recife)\ncache week_raw per day"]

    MANUAL["Admin POST /admin/venues/by-address"] --> MCACHE{"address-hash\nor geo-cache hit?"}
    MCACHE -- "yes" --> EXISTS["return already_exists"]
    MCACHE -- "no" --> RESERVE{"reserve monthly slot\n(uses the reserve=10)"}
    RESERVE -- "exhausted" --> Q429["429 quota exhausted"]
    RESERVE -- "granted" --> BTADD["BestTime POST /forecasts\n→ persist; geo-fallback\nif rejected"]
    BTADD --> UPSERT
```

| Stage | Cadence | Source call | Filter applied | Reversible? |
|---|---|---|---|---|
| Inventory sync | 30 d (step 1) | `GET /api/v1/venues` | born-deprecate high-confidence junk | 🗑️ soft-delete |
| Discovery | 30 d (step 2) | `/venues/filter` | de-dupe by id→name; monthly cap; born-deprecate | mixed |
| Live forecast | 5 min | `/forecasts/live` | delete stale cache if not OK / unavailable | ♻️ cache only |
| Weekly forecast | Sun 00:00 | `/forecasts/week/raw` | (none) cache week_raw | — |
| Manual add | on-demand (admin) | `POST /forecasts` | dedupe via address-hash + geo cache; budget reserve | — |
| Eligibility sweep | on-demand (admin) | none (cache-first) | soft-delete active venues now ineligible | 🗑️ soft-delete |

> **Budget:** `VenueBudgetService` enforces a monthly *new-venue* quota
> (`admin_config:venue_monthly_budget`, default quota 500 / manual reserve 10).
> Discovery stops short of the reserve so manual admin adds can always succeed.

---

## 3. cs-server — enrichment jobs (background)

Every enricher is **optional, key-gated 🔑, and idempotent**; a missing key or
disabled flag makes the job a no-op without breaking venue serving. Cadence and
flags come from `app/config.py` / `docs/pipelines.md`.

```mermaid
flowchart LR
    V[("Active venues\nin Redis #1")]

    V --> GPE["🟩E Google Places enrichment 🔑\nDaily 03:00"]
    GPE --> GPEOUT["vibe attributes (pet-friendly,\noutdoor, lgbtq…), google_primary_type,\nbusiness_status, opening_hours,\nreviews, IG handle from website (free)"]
    GPE --> GPECLOSE["🟥F business_status check:\npermanently_closed → soft-delete 🗑️\ntemporarily_closed → kept active"]

    V --> PHOTO["🟩E Photo enrichment 🔑\nDaily 03:00 → venue_photos"]
    V --> IG["🟩E Instagram handle 🔑\nMon 04:00\n(Google-website free → Apify search →\n7-signal validator)"]
    IG --> IGP["🟩E IG posts 🔑\nWed 04:00 → recent captions\n(text context for vibe AI)"]

    V --> MP["🟩E Menu photos 🔑\nMonthly 1st 05:00\nIG highlights → GMaps fallback → S3"]
    MP --> MX["🟩E Menu extraction 🔑\nMonthly 1st 06:00\nGPT-4o vision → structured menu"]

    PHOTO --> VC["🟩E Vibe classifier 🔑\nMonthly 1st 07:00\n2-stage GPT (4o-mini → 4o)\n→ vibe_profile, taxonomy, evidence photos"]
    IGP --> VC
```

| Job | Cadence | External source | Produces (Redis #1) | Doubles as filter? |
|---|---|---|---|---|
| `google_places_enrichment` | Daily 03:00 | Google Places | `VibeAttributes` (incl. `google_primary_type`), `OpeningHours`, `VenueReviews`, IG handle (from website, free) | **yes** — soft-deletes `permanently_closed` 🗑️ |
| `photo_enrichment` | Daily 03:00 | Google Places | `venue_photos` (list of `{url, author}`) | no |
| `instagram_enrichment` | Mon 04:00 | Google website (free) + Apify search | `VenueInstagram` (handle/url, confidence) | no |
| `ig_posts_enrichment` | Wed 04:00 | Apify IG scraper | `VenueInstagramPosts` (captions) → feeds vibe AI | no |
| `menu_photo_enrichment` | Monthly 1st 05:00 | Apify IG highlights → Apify GMaps (fallback) → S3 | `VenueMenuPhotos` (S3 keys) | no |
| `menu_extraction` | Monthly 1st 06:00 | OpenAI GPT-4o vision | `VenueMenuData` (sections/items/prices) | no |
| `vibe_classifier` | Monthly 1st 07:00 | OpenAI GPT-4o / 4o-mini | `VenueVibeProfile` (8-category taxonomy, evidence photos, top vibes) | no |

> The **Google Places enrichment is the only enricher that also filters**: it
> calls `search_for_lgbtq_indicators`, sets `business_status`, and
> soft-deprecates permanently-closed venues while *keeping* temporarily-closed
> ones active.

---

## 4. cs-server — the centralized eligibility filter

`app/services/venue_eligibility.py` owns **one** decision reused by discovery
write, inventory sync, the sweep, **and** serve time. It is a **block-list**:
a venue is eligible unless it positively matches a block rule — unknown/unlabeled
venues stay visible by design (never irreversibly hide a real bar).

```mermaid
flowchart TD
    IN["evaluate(name, besttime_type, google_type, config ⚙️)"] --> EMPTY{"empty name?"}
    EMPTY -- yes --> RH1["INELIGIBLE\nempty_name · HIGH"]
    EMPTY -- no --> GT{"google_type in\nblocked_google_types?"}
    GT -- yes --> RH2["INELIGIBLE\ngoogle_type · HIGH"]
    GT -- no --> BTT{"besttime_type in\nblocked_venue_types?"}
    BTT -- yes --> RH3["INELIGIBLE\nbesttime_type · HIGH"]
    BTT -- no --> GOOD{"resolves to a good\ncategory? (bar/food/nightlife)"}
    GOOD -- yes --> OKK["ELIGIBLE\n(positive category suppresses\nkeyword false-positives)"]
    GOOD -- no --> HARD{"hard name keyword?\n(farmácia, hospital, igreja…)"}
    HARD -- yes --> RH4["INELIGIBLE\nname_keyword · HIGH"]
    HARD -- no --> AMB{"ambiguous keyword?\n(mercado, parque, academia…)"}
    AMB -- "yes + google-labeled" --> RH5["INELIGIBLE\nname_keyword · HIGH"]
    AMB -- "yes + unlabeled" --> RL["INELIGIBLE\nname_keyword · LOW ♻️"]
    AMB -- no --> OK2["ELIGIBLE"]
```

**Confidence drives irreversibility:**

| Confidence | Where it acts | Effect |
|---|---|---|
| **HIGH** (`soft_deletable`) | write-time born-deprecate, sweep, **and** serve-time hide | safe to soft-delete 🗑️ even before Google labeling |
| **LOW** | serve-time hide only ♻️ | hidden from users, but **never** soft-deleted before a Google type confirms a non-good category |

Block-lists (`blocked_venue_types`, `blocked_google_types`,
`hard_blocked_name_keywords`, `ambiguous_name_keywords`) are **admin-tunable ⚙️**
via Redis key `admin_config:venue_eligibility`; a bad/invalid write silently
falls back to hardcoded defaults so filtering can never break.

---

## 5. cs-server — serve path `GET /v1/venues/nearby`

Synchronous, **Redis-only** (never calls upstream APIs). Default response is the
**minified** shape (`verbose=false`).

```mermaid
flowchart TD
    REQ["GET /v1/venues/nearby?lat&lon&radius&verbose"] --> GEO["geo radius load from Redis"]
    GEO --> DEP["🟥F drop deprecated\n(lifecycle_status != active)"]
    DEP --> ELIG["🟥F eligibility filter ♻️\n(HIGH-confidence only;\nuses cached google_primary_type)"]
    ELIG --> MERGE["🟩E merge live forecast +\nweekly forecast for current Recife day"]
    MERGE --> SORT["sort: live-data venues first,\ndesc by live busyness"]
    SORT --> XFORM["🟩E transform → MinifiedVenue"]
    XFORM --> OUT["JSON array → vibes_bot"]
```

**`MinifiedVenue` fields assembled in `_transform` (verbose=false):** core
(`venue_id/name/address/lat/lng/type`), `price_level`, `rating`, `reviews`
(count), `weekly_forecast` (today), `venue_live_busyness`, **display**
(`google_places_type` + `resolve_venue_display` → label/emoji/color + granular
subtitle), `vibe_labels`, `venue_summary`, `venue_photos` (**first 2 only**),
`opening_hours` (Google → **BestTime-derived fallback**, with `hours_source`),
`special_days`, `is_open_now`, `instagram_handle/url`, `vibe_profile`.

**Loaded only when `verbose=true`:** `venue_reviews` (full text), `venue_menu`,
full photo set + AI photo re-ordering by category/appeal.

---

## 6. The boundary — what actually crosses to vibes_bot

> **Critical for reading the diagrams correctly.** The pipeline's Step 1 calls
> `crowd_sense_client.get_venues_nearby(..., verbose=False)`. There is **no
> detail/verbose endpoint** in vibes_bot today (the only `verbose=True` calls are
> demo `__main__` blocks in the client files). Therefore:

| cs-server enrichment | Crosses on the served path? |
|---|---|
| Forecasts, vibe attributes, `google_primary_type`, IG handle, vibe labels, AI vibe profile, opening hours, display label | ✅ yes (in MinifiedVenue) |
| Venue **photos** | ⚠️ only the **first 2** |
| Google **reviews** (full text) | ❌ no — stored in cs-server, verbose-only |
| **Menu photos** (S3) + **menu extraction** (GPT-4o) | ❌ no — stored in cs-server, verbose-only |

So the monthly **menu photo + menu extraction** jobs and the Google **reviews**
enrichment are currently **stored but not served** to users through vibes_bot's
nearby flow. Worth flagging — a "comprehensive" view shouldn't imply they reach
the app today.

---

## 7. vibes_bot — the `get_venues` pipeline (19 steps)

This is where most user-facing filtering and enrichment happens. Every step is
traced (`PipelineTracer`) and most knobs are **admin-tunable ⚙️** (scoring
weights, blacklist, type/label maps, vibe modes) via vibes_bot's Redis.

```mermaid
flowchart TD
    S1["1 · fetch from cs-server (verbose=false)"] --> S2["2 · 🟩E normalize coords (lng/lon)"]
    S2 --> S3["3 · 🟩E forecast busyness for current/target day-hour (Recife TZ)"]
    S3 --> S4["4 · 🟩E backfill live busyness from forecast"]
    S4 --> S5{"5 · 🟥F drop venues with NO live busyness"}
    S5 -- "kept" --> S6["6 · 🟩E pricing 🔑 (Google Maps, prod)"]
    S5 -. "DROPPED (major)" .-> D1["✂"]
    S6 --> S7["7 · 🟩E annotate open status (hours / busyness)"]
    S7 --> S8{"8 · 🟥F open-now only — unknowns default to DROP"}
    S8 -- "open" --> S9["9 · 🟩E distance (km from origin)"]
    S8 -. "DROPPED (major)" .-> D2["✂"]
    S9 --> S10["10 · 🟩E live busyness label (vazio…cheio + color)"]
    S10 --> S11["11 · 🟩E busyness predictions (next_busy_level_X)"]
    S11 --> S12["12 · 🟩E hot likes count + user_has_liked"]
    S12 --> S13["13 · sort by combined score (master feed interleave)"]
    S13 --> S14{"14 · 🟥F name blacklist ⚙️"}
    S14 -- "kept" --> S15["15 · 🟩E venue type info (label/emoji/color)"]
    S14 -. "dropped" .-> D3["✂"]
    S15 --> S16["16 · 🟩E translate vibe labels → PT-BR + emoji"]
    S16 --> S17["17 · 🟩E AI vibe tags from vibe_profile"]
    S17 --> S18["18 · 🟩E similar venue IDs"]
    S18 --> S19["19 · 🟩E busyness insights 🔑flag (peak/arrival/momentum/timeline)"]
    S19 --> S20["20 · 🟩E vibe modes eligibility + per-mode scores"]
    S20 --> OUT["served list → POST /venues  /  LLM dataset"]
```

> *(Code labels two steps "Step 13"; renumbered sequentially here — 19 logical
> stages plus the final mode step.)*

| # | Step | Type | Notes |
|---|------|------|-------|
| 1 | fetch from cs-server | source | minified venues |
| 2 | normalize coords | 🟩E | unify `venue_lon`/`venue_lng`/`lng` |
| 3 | forecast busyness | 🟩E | current or **time-travel** target hour/day (Recife TZ) |
| 4 | live busyness fallback | 🟩E | fill missing live from forecast |
| 5 | **drop no-busyness** | 🟥F | **major drop** — no live & no estimate → removed |
| 6 | pricing | 🟩E 🔑 | Google Maps price (prod); mocked in dev |
| 7 | annotate open status | 🟩E | from opening hours / live busyness |
| 8 | **open-now only** | 🟥F | **major drop** — `DEFAULT_VALUE=False`: unknown ⇒ dropped |
| 9 | distance | 🟩E | km from search origin |
| 10 | live busyness label | 🟩E | `vazio`→`cheio` + brand color |
| 11 | busyness predictions | 🟩E | `next_busy_level_*`, relative to check_time |
| 12 | hot likes | 🟩E | count + `user_has_liked` (if authed) |
| 13 | score & sort | sort | quality `rating^3 × log(reviews)`, distance decay (half=8km), busyness, hot likes; interleaved "master feed" A/A/A/B/C/D ⚙️ |
| 14 | name blacklist | 🟥F ⚙️ | hardcoded list + Redis override (e.g. Subway, self-service) |
| 15 | venue type info | 🟩E ⚙️ | label/emoji/color |
| 16 | translate vibe labels | 🟩E ⚙️ | PT-BR + emoji |
| 17 | AI vibe tags | 🟩E | extracted from cs-server `vibe_profile` |
| 18 | similar venues | 🟩E 🔑flag | similar venue IDs |
| 19 | busyness insights | 🟩E 🔑flag | peak/arrival/momentum/timeline |
| 20 | vibe modes | 🟩E ⚙️ | `mode_eligibility` + per-mode `vibe_scores` for the frontend |

**The two hard drops (Steps 5 & 8) are where most venues disappear.** Step 5
removes anything cs-server couldn't attach live/forecast busyness to; Step 8
removes anything not provably open *right now* (or at the time-travel target),
**including venues whose status is unknown** (`DEFAULT_VALUE = False`).

---

## 8. Consolidated filter inventory

Every place a venue can be removed or hidden, in flow order:

| # | Filter | Repo / location | Mechanism | Reversible? | Tunable? |
|---|---|---|---|---|---|
| 1 | De-dupe (id, then name) | cs-server discovery | skip duplicates in `/venues/filter` batch | n/a | — |
| 2 | Monthly new-venue cap | cs-server discovery | budget service stops discovery | n/a | ⚙️ |
| 3 | Born-deprecate junk | cs-server write | eligibility HIGH → `deprecated` | 🗑️ | ⚙️ |
| 4 | Live-forecast cache prune | cs-server 5-min job | delete cache if closed/unavailable | ♻️ | — |
| 5 | Permanently-closed | cs-server Google enrich | `business_status` → soft-delete | 🗑️ | — |
| 6 | Eligibility sweep | cs-server admin | soft-delete now-ineligible actives | 🗑️ | ⚙️ |
| 7 | Drop deprecated | cs-server serve | `lifecycle_status != active` | ♻️ | — |
| 8 | Eligibility (serve) | cs-server serve | hide HIGH-confidence ineligible | ♻️ | ⚙️ |
| 9 | **No-busyness drop** | vibes_bot step 5 | `venue_live_busyness is None` | ♻️ | — |
| 10 | **Open-now drop** | vibes_bot step 8 | `is_open` false/unknown | ♻️ | — |
| 11 | Name blacklist | vibes_bot step 14 | name in blacklist | ♻️ | ⚙️ |

---

## 9. Consolidated enrichment inventory (field provenance)

"Where does each field on a served venue come from?"

| Field(s) on served venue | Produced by | Source | Crosses to vibes_bot? |
|---|---|---|---|
| `venue_id/name/address/lat/lng/type`, `rating`, `reviews`(count), `price_level` | cs-server discovery | BestTime | ✅ |
| `venue_live_busyness`, `weekly_forecast` | cs-server forecast jobs | BestTime | ✅ |
| `google_places_type`, vibe attributes, `business_status`, `opening_hours`, `special_days` | cs-server Google enrich | Google Places | ✅ (hours/type/`is_open_now`) |
| display `label`/`emoji`/`color`/granular subtitle | cs-server `resolve_venue_display` | derived | ✅ |
| `vibe_labels`, `venue_summary` | cs-server vibe attrs | Google/derived | ✅ |
| `vibe_profile` (taxonomy, top vibes) | cs-server vibe classifier | OpenAI + photos/IG | ✅ |
| `instagram_handle/url` | cs-server IG enrich | Google website + Apify | ✅ |
| `venue_photos` | cs-server photo enrich | Google Places | ⚠️ first 2 only |
| `venue_reviews` (full text) | cs-server Google enrich | Google Places | ❌ verbose-only |
| `venue_menu` | cs-server menu jobs | Apify + S3 + OpenAI | ❌ verbose-only |
| `price` (live) | vibes_bot step 6 | Google Maps | (added in vibes_bot) |
| `distance` | vibes_bot step 9 | computed | (added in vibes_bot) |
| `live_busyness_label` + color | vibes_bot step 10 | derived | (added in vibes_bot) |
| `next_busy_level_*` | vibes_bot step 11 | derived | (added in vibes_bot) |
| `hot_likes`, `user_has_liked` | vibes_bot step 12 | Redis #2 | (added in vibes_bot) |
| `combined_score` | vibes_bot step 13 | computed | (added in vibes_bot) |
| translated vibe labels / AI vibe tags | vibes_bot steps 16-17 | derived from cs-server data | (added in vibes_bot) |
| `similar_venue_ids` | vibes_bot step 18 | computed | (added in vibes_bot) |
| busyness insights | vibes_bot step 19 | derived | (added in vibes_bot) |
| `mode_eligibility`, `vibe_scores` | vibes_bot step 20 | derived | (added in vibes_bot) |

---

## 10. End-to-end: the life of one venue

1. **Discovered** by BestTime `/venues/filter` (or pulled in by inventory sync,
   or manually added). De-duped, checked against the monthly budget, and
   born-deprecated if it's high-confidence junk. → Redis #1.
2. **Forecasted**: live busyness every 5 min, weekly raw every Sunday.
3. **Enriched** over the following days: Google attributes + type + hours +
   reviews + photos (daily), Instagram handle (Mon) and posts (Wed), then monthly
   menu photos → menu extraction → AI vibe classification.
4. If Google says **permanently closed**, or it matches a block rule, it is
   **soft-deleted** and disappears from serving.
5. On a user request, **cs-server** serves it from Redis only — after dropping
   deprecated/ineligible venues and merging today's forecast — as a **minified**
   record (no reviews/menu; ≤2 photos).
6. **vibes_bot** runs it through the 20-step pipeline: it survives only if it has
   **busyness** (step 5) *and* is **open now** (step 8) *and* isn't
   **blacklisted** (step 14); then it's priced, scored, labeled, tagged, and
   given vibe-mode eligibility.
7. It's returned to the mobile app via `POST /venues`, or written into
   `places_dataset.json` and fed to the **LLM bot** for `POST /ask`.

---

## Appendix — key files

**cs-server**
- Orchestration / jobs: `main.py`, `app/services/venues_refresher_service.py`
- Eligibility filter: `app/services/venue_eligibility.py`
- Serve path: `app/handlers/venue_handler.py`, `app/routers/venue_router.py`
- Display mapping: `app/models/venue_category.py`
- Enrichers: `app/services/{google_places,photo,instagram,instagram_posts,menu_photo,menu_extraction,vibe_classifier}_*.py`
- Budget / manual add: `app/services/venue_budget_service.py`, `app/handlers/add_venue_handler.py`
- Pipeline doc: `docs/pipelines.md`

**vibes_bot**
- Pipeline: `app/services/venue_pipeline.py`
- HTTP boundary: `app/services/crowd_sense_client.py`
- Filters: `app/services/{open_now_filter,venue_blacklist_filter}.py`, open-status `app/services/open_status_service.py`
- Enrichers: `app/services/{distance,forecast_busyness,live_busyness,scoring,busyness_prediction,busyness_insights,hot_likes,similar_venues,venue_type,vibe_labels,vibe_modes_evaluator,pricing/*}.py`
- Entry points & flags: `app/main.py`, `app/config/settings.py`
