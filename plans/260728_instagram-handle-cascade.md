# Instagram Handle Cascade With Evidence-Based Confidence

## Branch
feature/instagram-handle-cascade

## Goal
Find a venue's Instagram handle from the cheapest source that can supply it, and
attach evidence to the answer instead of a bare score.

Today discovery is two-phase: a free handle parsed from Google Places
`websiteUri`, then a paid Apify Instagram search for everything else. Two things
are missing. There is a **third free source already sitting in S3** — the Apify
Google Maps payload archived by the photo pipeline carries the venue's
`website`, already fetched and already paid for. And a handle, once found, is
never **verified**: a search result scoring 0.75 on name similarity is accepted
without anyone checking the profile exists.

This adds the free tier, stops at the first candidate that clears a high bar, and
verifies whichever tier produced it.

## Non-goals
- **Replacing `InstagramValidator`.** Its 7 weighted signals remain the scorer
  for the Apify tier; this wraps it, it does not rewrite it.
- **Instagram post scraping.** The operator asked about comparing post images to
  venue photos; posts require the Apify `instagram-scraper` actor and real spend
  on top of tier 3. The judge uses the free `og:image` and photos we already
  archived. Post comparison stays a later option.
- **Backfilling handles for venues with no archived payload.** Tier 2 can only
  help venues the photo-archive job has already run against.
- **The Instagram Posts job** (`instagram_posts`) — untouched.
- **Changing the serving projection.** Handles reach Redis through the existing
  projector, unchanged.

## Evidence

### Experiments run before planning (these invalidate the obvious design)
- **A plain GET cannot test existence.** `https://www.instagram.com/<handle>/`
  returns **HTTP 200 and ~602 KB for every handle**, including
  `zzzz_definitely_not_a_real_handle_9182`. All four probes landed within 30
  bytes of each other. Any "200 means it exists" check is worthless.
- **The anonymous page has no content to compare.** It is a JS shell:
  `<title>Instagram</title>`, 8 meta tags, **zero `og:` tags**, no bio, no
  address, no name. Page-content matching and screenshotting a profile are both
  impossible — a screenshot captures a login wall.
- **A crawler user-agent does work.** With
  `facebookexternalhit/1.1`, Instagram serves Open Graph:
  `og:title` = `"Tasquinha do Tio (@tasquinhadotio) • Instagram photos and videos"`,
  `og:description` = `"23K Followers, 8 Following, 218 Posts - …"`,
  `og:image` (profile picture URL), `og:type=profile`. **Absent** for a handle
  that does not exist — verified against three real handles and two fakes.
- **Name similarity separates true from false.** Measured on real venues:
  exact `1.00`; true-but-differently-named `0.76` (venue "Bercy Boa Viagem" vs
  profile "Bercy Village"); deliberate wrong pairing `0.26`. The 0.76 case is the
  one a fixed threshold gets wrong in both directions, and is where the judge
  earns its cost.
- **The link-shim trap is real, not theoretical.** Of 4 archived payloads, one
  venue's `website` is `https://l.instagram.com/?u=https%3A%2F%2Fwww.ifood.com.br…`
  — Instagram's outbound redirect wrapper pointing at iFood. Naive
  `contains("instagram.com")` extracts a garbage handle from it.
- **Yield, on the same 4 payloads:** 3 have a `website`, 1 of those is a direct
  handle, 1 is the shim, 1 is a normal domain. Directional only — the sample is
  tiny.

### Code
- Current discovery: `app/services/instagram_enrichment_service.py` — cache
  check, then `apify_client.search_users`, then `InstagramValidator.validate`
  per candidate with early exit at `auto_accept_threshold`.
- Scorer: `app/services/instagram_validator.py:104` — weights
  `name_similarity .30`, `bio_address_city .20`, `bio_venue_type .10`,
  `is_business_account .10`, `business_category .10`, `external_url .10`,
  `follower_sanity .10`; thresholds `0.75` accept / `0.50` low-confidence, both
  settings-driven (`app/container.py:257`).
- Free tier 1 already exists: the handle parser over Google `websiteUri` at
  `app/services/google_places_enrichment_service.py:827` handles several URL
  shapes; the cascade reuses it rather than writing a second parser.
- **Tier 2's data is unreadable today.** `MediaArchiveStore`
  (`app/dao/media_archive_store.py`) exposes `put_*`, `list_*`,
  `exists_for_venue` and **no object read**, because `infra/datalake/iam.tf`
  deliberately withholds `s3:GetObject` — "the pipeline may see that objects
  exist and add new ones, and can never read the archive back." Tier 2 requires
  changing that (see §G); the operator approved granting it, scoped to
  `retrieved/*` only.
- Storage: `instagram.handle` (`migrations/versions/0001_baseline_schemas.py:110`)
  — `venue_id` PK, promoted `instagram_handle`, `payload jsonb`, `deleted_at`,
  `updated_at`. RDS is the system of record; `set_venue_instagram`
  (`app/dao/venue_repository.py:185`) discards the TTL kwargs — nothing expires
  in Postgres.
- Freshness gate: `list_fresh_instagram_venue_ids`
  (`app/dao/rds_venue_store.py:420`) is status-aware — `found` ~30d,
  `not_found` ~7d — and is the existing skip-before-spend guard.
- LLM: `OpenAIVibeClient` (`app/api/openai_vibe_client.py:237`) is the in-repo
  precedent for a `gpt-4o-mini` call with a settings-driven key.

## Current Behavior
`enrich_all_venues` walks the serving view, skips venues still fresh, and for the
rest calls Apify search and scores candidates. A handle from Google Places is
cached with `confidence=1.0` and never re-checked. A handle from Apify is
accepted on `InstagramValidator` alone — **nobody ever asks Instagram whether the
profile exists.** The archived Google Maps `website` field is never consulted, so
venues whose handle is sitting in S3 still cost an Apify search.

## Desired Behavior
1. For a venue needing a handle, try sources cheapest-first and **stop at the
   first candidate that clears the high-confidence bar**:
   1. Google Places `websiteUri` (free, existing)
   2. Archived Apify Google Maps `place.json` → `website` (free, new)
   3. Apify Instagram search (paid, existing)
2. Extraction rejects link shims (`l.instagram.com`) and non-profile paths
   (`/p/`, `/reel/`, `/reels/`, `/explore/`, `/stories/`, `/tv/`).
3. Every candidate is **verified** by fetching its Open Graph metadata with a
   crawler user-agent: exists / display name / follower counts / profile image.
4. Confidence combines provenance, profile existence, display-name similarity,
   and — for tier 3 only — the existing 7-signal validator.
5. When the cheap signals land in the ambiguous band, an LLM judge decides,
   using the venue's name and address, the `og:image`, and archived venue photos.
   **The judge must still return a verdict when no images are available** (see
   §E) — missing photos degrade its confidence, never its ability to answer.
6. The chosen handle is persisted with its **provenance and evidence**, so
   per-tier yield and the free-vs-paid split are measurable afterwards.
7. A venue whose handle is still fresh is skipped before any source is consulted.

## Implementation Approach

### A. Cascade
A `HandleSource` descriptor per tier (id, label, cost class, resolver) and an
ordered tuple, mirroring `archive_sources.ARCHIVE_SOURCES` — the pattern already
used in this repo for exactly this shape. `discover_instagram_for_venue` walks
them in order, verifies each candidate, and returns as soon as one clears the
accept bar. Tier order and enablement are settings, so tier 3 can be turned off
entirely to run a zero-cost pass.

### B. Extraction
One parser, reused from the Google Places path, hardened with a reject list:

- host `l.instagram.com` → reject (outbound wrapper, seen in production data)
- first path segment in `{p, reel, reels, explore, stories, tv, s, accounts}` →
  reject (not a profile)
- strip query/fragment (`?igshid=…`), lowercase, strip a leading `@`

Rejections are **counted, not silent** — a shim that starts appearing under a new
host must show up in metrics rather than as a mysterious dip in yield.

### C. Verification (Open Graph)
`InstagramProfileProbe.fetch(handle)` issues one GET with the crawler
user-agent and parses `og:title`, `og:description`, `og:image`, `og:type`.
Returns `exists` plus display name, follower/following/post counts, image URL.

This is **undocumented behavior Instagram can withdraw**, so it degrades rather
than fails: a probe error, a timeout, or a body without `og:type=profile` yields
`unknown`, not `does not exist`. `unknown` suppresses the existence bonus and
forces the ambiguous path; it never turns a good handle into a rejection. Probes
are rate-limited and cached per handle for the run.

### D. Confidence
A small, explicit, testable model rather than a magic number:

- **Provenance**: tier 1 and tier 2 come from the venue's own listing — near
  certain. Tier 3 is a search guess and starts far lower.
- **Existence**: profile confirmed present via §C.
- **Name similarity**: venue name vs `og:title` display name.
- **Validator**: tier 3 only, the existing 7 signals.

Accept / ambiguous / reject bands are settings-driven and reuse the existing
`instagram_auto_accept_threshold` / `instagram_min_confidence` where they apply,
so tuning stays in one place.

### E. LLM judge — must work without images
Consulted **only** in the ambiguous band, and only when an OpenAI key is
configured. It receives whatever exists:

- always: venue name, address, city; profile display name, bio, follower/post
  counts
- when available: the `og:image` profile picture, and up to N venue photos read
  from the archive

The judge runs in one of three modes, chosen by what is actually available:

| Available | Mode | Effect |
|---|---|---|
| profile image + venue photos | `vision_both` | full comparison |
| only one side (no venue photos in S3, or no usable profile image) | `vision_partial` | judges on the image it has plus all text |
| neither | `text_only` | judges on names, address, bio, counts alone |

**Missing images must never block a verdict.** A venue with nothing archived, or
a profile using a default/blank avatar, still gets a decision — with the mode
recorded in the evidence and a lower confidence ceiling, because a text-only
judgement is genuinely weaker. If the LLM call itself fails or is unconfigured,
the cascade falls back to the cheap-signal score and records `judge=unavailable`;
it never fails the venue.

### F. Persistence and provenance
Writes stay on `set_venue_instagram` → `instagram.handle`. The `payload` jsonb
gains a `discovery` block: `source` (tier id), `confidence`, `signals`,
`profile_exists`, `display_name`, `name_similarity`, `judge` (mode + verdict when
consulted), `checked_at`.

`source` is **also promoted to a real column**, because the first question this
feature has to answer is "how many handles came free versus paid", and answering
it from jsonb extraction across the catalog is exactly the query that gets
written wrong. One additive Alembic migration; nullable, no backfill required.

### G. Terraform / IAM
`infra/datalake/iam.tf` grants `s3:GetObject` on **`retrieved/*` only**.
`raw/*` and `media/*` stay unreadable, so the raw BestTime lake and the archived
images keep their append-only property; only the place payloads this feature
needs become readable.

`MediaArchiveStore` gains a matching `get_info(...)` read confined to that
prefix.

**Do not edit `aws_iam_policy.description`** — it is immutable in AWS, and
changing it forces a destroy-and-recreate of the policy plus its role
attachment, opening a window with no `PutObject` during which a data lake flush
is dropped rather than retried. That is documented in the file; this change must
land as an in-place document update (`0 to add, 1 to change, 0 to destroy`).

### H. Metrics
Per-tier counters so the economics are visible without reading logs.

## Data, Config, And API Impact
- **API:** none.
- **Persistence:** one additive Alembic migration adding a nullable `source`
  column to `instagram.handle`. `payload` gains a `discovery` block — jsonb, no
  migration. No Redis key change.
- **New settings:** `instagram_cascade_enabled` (bool, true),
  `instagram_source_order` (list), `instagram_tier3_enabled` (bool, true — the
  zero-cost switch), `instagram_profile_probe_enabled` (bool, true),
  `instagram_profile_probe_timeout_seconds` (float, 10),
  `instagram_probe_user_agent` (str), `instagram_judge_enabled` (bool, false),
  `instagram_judge_model` (str, `gpt-4o-mini`),
  `instagram_judge_max_venue_photos` (int, 3),
  `instagram_judge_band_low` / `_high` (floats).
- **Infrastructure:** the IAM change in §G must be applied before tier 2 can read
  anything; until then tier 2 reports unavailable and the cascade skips it.

## Error Handling And Observability
Every tier and every check degrades rather than fails. A venue is only `error`
if something unexpected escapes.

| Failure | Behavior | Recorded as |
|---|---|---|
| Tier resolver raises | skip that tier, continue the cascade | `tier_error{source}` |
| No archived payload for the venue | tier 2 yields nothing, continue | `tier_miss{source}` |
| S3 read denied (IAM not applied) | tier 2 marked unavailable once, then skipped | `tier_unavailable` |
| Probe times out / non-profile body | `exists=unknown`, forces ambiguous path | `probe_unknown` |
| Extraction rejects a shim/non-profile path | candidate dropped | `handle_rejected{reason}` |
| No venue photos or no usable profile image | judge runs in a reduced mode | `judge_mode{mode}` |
| LLM unconfigured or failing | fall back to cheap-signal score | `judge=unavailable` |
| Apify credits exhausted | tier 3 disabled for the run; tiers 1–2 continue | existing behavior |

New metrics in `app/metrics.py`:

```
instagram_cascade_results_total{source,result}   result: accepted, rejected,
                                                 ambiguous, not_found, error
instagram_cascade_tier_attempts_total{source}
instagram_handle_rejected_total{reason}          reason: link_shim, non_profile_path,
                                                 empty, malformed
instagram_profile_probe_total{result}            result: exists, absent, unknown
instagram_judge_total{mode,verdict}              mode: vision_both, vision_partial,
                                                 text_only, unavailable
instagram_cascade_paid_calls_total
```

`instagram_cascade_tier_attempts_total` versus
`instagram_cascade_paid_calls_total` is the number this feature exists to move:
handles resolved without spending anything.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-handle-cascade.feature`

Scenarios:
- Accept a handle from the venue's own Google website without consulting any
  paid source.
- Take the handle from the archived Google Maps payload when Google Places has
  none, still without a paid call.
- Fall through to the paid Apify search only when both free tiers yield nothing.
- Stop the cascade at the first candidate that clears the high-confidence bar.
- Reject an `l.instagram.com` outbound wrapper instead of extracting a handle
  from it.
- Reject `/p/`, `/reel/`, and `/explore/` paths as non-profile URLs.
- Confirm a profile exists from its Open Graph metadata.
- Treat a handle whose profile has no Open Graph profile data as not found.
- Treat a probe failure as unknown rather than as proof of absence.
- Accept a strong name match without consulting the LLM judge.
- Consult the judge only in the ambiguous band.
- **Judge a candidate with no venue photos available.**
- **Judge a candidate whose profile picture is unusable.**
- **Judge on text alone when no images exist on either side.**
- Fall back to the cheap-signal score when the judge is unavailable.
- Record the source, confidence, and evidence for an accepted handle.
- Skip a venue whose handle is still fresh, consulting no source at all.
- Continue the run when one tier raises.
- Expose the per-tier and paid-call metrics.

Pytest unit tests:
- `tests/test_instagram_handle_extraction.py` — shim rejection (the real iFood
  wrapper), non-profile paths, `?igshid=` stripping, `@` prefix, casing, empty
  and malformed input.
- `tests/test_instagram_profile_probe.py` — og parsing from a real captured
  body; absent-og body → `absent`; timeout/5xx → `unknown` (never `absent`);
  crawler user-agent actually sent.
- `tests/test_instagram_confidence.py` — provenance weighting, the measured
  1.00 / 0.76 / 0.26 separation, band boundaries, tier-3 validator integration.
- `tests/test_instagram_cascade.py` — tier order; **stops before the paid tier
  when a free tier accepts (asserting the Apify client is never called)**;
  tier-2 unavailable is skipped cleanly; one tier raising does not end the
  cascade.
- `tests/test_instagram_judge.py` — the three modes selected by available inputs;
  a verdict is produced with zero images; unconfigured/failing LLM falls back
  without failing the venue.

Manual or integration checks:
- Apply the IAM change; confirm with the policy simulator that the role can
  `GetObject` under `retrieved/*` and still **cannot** under `raw/*` or `media/*`.
- Run the cascade over a handful of real venues with tier 3 disabled; confirm
  free-tier hits and that `instagram_cascade_paid_calls_total` stays at zero.

## Acceptance Criteria
- A venue whose Google or archived listing carries an Instagram URL resolves
  with **zero paid calls**, proven by an assertion that the Apify client is not
  called.
- `l.instagram.com/?u=…` never produces a handle.
- An accepted handle has been confirmed to exist via Open Graph, except when the
  probe returned `unknown`, which is recorded as such.
- The judge returns a verdict when there are no venue photos, when the profile
  picture is unusable, and when neither exists — with the mode recorded.
- A stored handle carries its `source`, confidence, and evidence, and `source` is
  queryable as a column.
- Tier 3 can be disabled to run a zero-cost pass over the catalog.
- The role can read `retrieved/*` and still cannot read `raw/*` or `media/*`.
- `make test-bdd` and `make test-unit` pass; the `@wip` tag is removed.

## Open Questions
- None. Source order, the S3-read decision (grant `GetObject` on `retrieved/*`),
  judge scope (og:image + archived venue photos, no post scraping), and the
  no-images requirement are all settled.

## Sequencing note
`chore/remove-dead-instagram-profile-scraper` (cs-server#107) also edits
`app/api/apify_instagram_client.py`. It is a pure deletion; merging it first
avoids a needless conflict.
