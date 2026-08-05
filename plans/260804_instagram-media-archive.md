# Instagram Media Archive — post images and captions as an archive source

## Branch
feature/instagram-media-archive

## Goal
Archive Instagram post images and their captions into the S3 data lake under
their own source folder, through the existing `venue_photo_archive` pipeline, so
the lake holds Instagram media beside the Google-derived media it already holds
— and so the event work that follows has a durable corpus of flyers to read.

## Non-goals
- **Events.** No event extraction, no event schema, no promoter accounts. This
  plan only puts Instagram pixels and captions in the lake.
  (`260804_event-venue-targeting.md` and `260804_instagram-event-extraction.md`.)
- **Stories, highlights, reels.** Feed posts only. Stories expire in 24h and
  need a frequent cron rather than an operator-triggered run — a different cost
  and scheduling posture, decided separately.
- **Serving Instagram media to the app.** `retrieved/` is internal-use only
  (`docs/venue-retrieval-storage.md` §8). Nothing here widens that.
- **Backfilling the existing `instagram.posts` rows** with image URLs. Those
  rows were scraped caption-only and the URLs are gone; re-scraping is a paid
  run the operator triggers, not a migration.
- **Re-categorising already-archived photos.** Adding a photo category affects
  new writes only; moving objects is a different job
  (`docs/venue-retrieval-storage.md` §4).

## Evidence

**The layout already separates by source, so "a separate folder" is free.**
`app/dao/media_archive_store.py` writes
`retrieved/source=<s>/year=/month=/day=/run_id=<ulid>/venue_id=<v>/media/<category>/<photo_id>.<ext>`.
A new source is a new top-level folder by construction. **No terraform change is
needed** — `infra/datalake/iam.tf` already grants `PutObject` and prefix-scoped
`ListBucket` on `retrieved/*`, and adding a source does not add a prefix. This
matters because `docs/venue-retrieval-storage.md` §5 warns that writing outside
the policy fails *after* the fetch has been paid for.

**Sources are a registry, not an if-branch.** `app/services/archive_sources.py`
holds `ARCHIVE_SOURCES` with three entries (`google_photos`,
`apify_gmaps_extractor`, `searchapi_gmaps_photos`). Each owns its config schema,
its `fetch`, its own cost model, and the client attribute it needs. `GET
/admin/jobs` publishes the catalog and the admin panel renders it, so **a new
source needs no admin-UI change** (§2 of the storage doc). Instagram is a
registry entry.

**The Instagram client deliberately throws the images away.**
`app/api/apify_instagram_client.py:fetch_recent_posts` returns
`caption / likes_count / comments_count / timestamp / post_type` and its
docstring says "no image URLs — they expire". `app/models/instagram.py:
InstagramPost` carries the same five fields. The expiry is real and is precisely
the argument for archiving the bytes: an Instagram CDN URL is signed and
short-lived, so it is worth nothing stored and everything downloaded.

**Handles already exist.** The cascade (`app/services/instagram_cascade_service.py`,
`instagram_handle_sources.py`, `instagram_judge.py`) resolves handles into
`instagram.handle`, which since `0020_instagram_handle_source` records the tier
that produced each one. `RdsVenueStore.list_instagram_sources()` and
`list_fresh_instagram_venue_ids()` already read that table.

**The classifier will label these photos.** `ArchiveSource.provides_categories`
is False for any source that cannot name Google's own tab, and
`app/services/photo_classification_service.py` runs between fetch and store so a
photo lands in the right folder first time. Instagram returns no category, so
Instagram photos are classified — and `app/models/photo_taxonomy.py` has no
category for a poster, so every event flyer currently lands in `other`.

**The photo id is derived from a Google CDN URL.**
`venue_photo_archive_service.photo_token()` parses the
`lh3.googleusercontent.com/...<token>=<size>` shape. Instagram URLs do not have
that shape and their query string is a rotating signature, so reusing this
function would produce a different id for the same image on every run.

## Current Behavior
The archive pipeline can only fetch Google-derived photos. The `instagram_posts`
job scrapes captions into `instagram.posts` (a single jsonb row per venue,
`venue_id` primary key, FK to `venues.venue`) and Redis, and discards every
image URL at the client boundary. No Instagram pixel has ever reached S3.

## Desired Behavior
1. Offer an `instagram_posts` source in the archive-source catalog, available
   only when the Apify token is configured.
2. For each selected venue with a confirmed Instagram handle, scrape its recent
   feed posts, download each post image, and store the bytes under
   `retrieved/source=instagram_posts/…/venue_id=<v>/media/<category>/`.
3. Expand every carousel: each child image is archived separately.
4. Write one manifest entry per image carrying its caption, permalink,
   shortcode, post timestamp, carousel index, like and comment counts, and post
   type — so a consumer can join an image back to the post that carried it
   without a second scrape.
5. Derive a photo id that is stable across runs and independent of the signed
   URL.
6. Record `no_handle` as its own outcome, distinct from `no_match` and `failed`.
7. Classify Instagram photos with the existing vision pass, and give a poster or
   flyer its own category so it is not filed as `other`.
8. Respect every existing spend control — `max_venues`, `max_photos_per_venue`,
   `skip_scope`, `dry_run` — unchanged, and price a run before it spends.

## Implementation Approach

### A. Teach the Instagram client to return media
Extend `fetch_recent_posts` to keep what it currently drops: `displayUrl`, the
carousel children (`childPosts[].displayUrl`), `shortCode`, `url`, and the
post's `type`. The caption-only shape stays valid — the extra keys are additive,
so `InstagramPostsEnrichmentService` keeps working untouched. `InstagramPost`
gains optional `shortcode`, `permalink`, and `image_urls`, which ride in the
existing `instagram.posts.payload` jsonb: **no migration.**

The scrape and the download must happen in the same run. An Instagram CDN URL
carries a signature with a short expiry, so a design that scrapes now and
downloads later returns 403s for content already paid for. The source's `fetch`
therefore returns URLs the pipeline downloads immediately, which is already how
the shared downloader behaves — this is a constraint to preserve, not a
mechanism to build.

### B. The `instagram_posts` archive source
One entry in `ARCHIVE_SOURCES`:

- `requires_attr="apify_instagram_client"`, unavailable reason naming
  `APIFY_API_TOKEN`.
- `fetch` reads the venue's handle from `instagram.handle`; a venue without one
  returns the new `no_handle` outcome and **costs nothing**, because the lookup
  is a database read that happens before the actor runs. This ordering is the
  same cost guarantee as the existing skip check (§3 of the storage doc) and
  must not be rearranged.
- `estimate_units` = `venues × posts_per_venue`, unit label `posts scraped` —
  the Apify Instagram actor bills per result item, unlike the Maps extractor
  which bills per place. A new setting `apify_instagram_post_cost_usd` carries
  the unit price and, like the Google price, is an unverified setting surfaced
  in the estimate's caveat.
- `provides_categories=False`, so the classifier runs.
- `config_schema`: `posts_per_venue`, and `include_carousel_children`
  (default yes).

**Carousels are the cap trap.** `max_photos_per_venue` is documented as the
total across every category (§3). A ten-post scrape where each post is a
ten-image carousel is a hundred images from one venue. The per-venue cap must
bound the images, not the posts, and the fetch must stop requesting children
once the cap is reached rather than downloading past it and trimming.

### C. A stable Instagram photo id
`photo_id_for` dispatches per source. For Instagram the id is
`<shortcode>` for a single image and `<shortcode>_<n>` for carousel child *n*.
This is derived from the post identity rather than the URL, so it is stable
across runs, survives the signature rotation that would defeat a URL hash, and
is human-traceable straight back to `instagram.com/p/<shortcode>`. Google's
token-based derivation stays exactly as it is for the Google sources.

### D. A category for posters
Add `flyer` to `app/models/photo_taxonomy.py`: "the subject is a promotional
poster, flyer or event announcement — the image is mostly graphic design and
text rather than a photograph". Additive to `PHOTO_CATEGORIES`, `CATEGORY_RULES`
and the per-category attribute map, so it costs no extra model call — the single
classification call already returns a category and gains one option.

Its attributes are the ones a poster can actually answer, and no more: whether
it announces a dated event, and whether it names a time. Deliberately shallow —
parsing the date is `260804_instagram-event-extraction.md`'s job with a purpose-
built prompt, and asking a photo classifier to do it would repeat the
`time_of_day` mistake recorded in §4 of the storage doc, where a question was
asked of photos that could not answer it and dragged the coverage figure down.

This category is what makes the archive worth building twice over: it is the
free pre-filter that decides which images the event extractor is allowed to
spend a vision call on.

### E. Outcomes
Add `no_handle` to the outcome counters. The repo already learned this lesson —
`no_match` was separated from `failed` because conflating them made an unmatched
venue look like an outage (§6), and `ArchiveFetchTimeout` was separated from
both for the same reason. A venue with no Instagram handle is a **targeting**
result, not a scrape result, and lumping it into `no_match` would make a catalog
with thin handle coverage look like a broken scraper.

## Data, Config, And API Impact
- **Migration:** none. New post fields ride in the existing
  `instagram.posts.payload` jsonb.
- **Persistence:** a new S3 source folder, `retrieved/source=instagram_posts/`.
  Layout, manifest shape, `_latest.json` and `skip_scope` semantics are
  unchanged.
- **IAM / terraform:** none. `retrieved/*` is already granted. Recorded
  explicitly because the storage doc flags a new prefix as a deploy-order trap,
  and this is deliberately not one.
- **Settings:** `apify_instagram_post_cost_usd` (estimate only),
  `instagram_archive_posts_per_venue` (default for the source's config field).
- **API:** none added. `GET /admin/jobs` gains the new source in its existing
  catalog response, which the admin panel already renders.
- **Behavior change worth flagging:** `instagram.posts` payloads written after
  this change carry more keys. Every reader must tolerate both shapes; nothing
  reads by strict schema today, and `InstagramPost`'s new fields are optional.

## Error Handling And Observability
A post whose image fails to download is counted and skipped; the other images of
the same venue still archive, matching the existing per-photo failure isolation.
`ApifyCreditExhaustedError` must be translated to `ArchiveCreditExhausted` at
the source boundary so the run stops — the Maps source needed exactly this
translation and, without it, kept calling into an exhausted balance for every
remaining venue.

Metrics: `media_archive_*` gains `source="instagram_posts"` with no new label
(`job_id` is never a Prometheus label — §7), the new `result="no_handle"`, and
`instagram_archive_images_total{result}` split by
`downloaded | expired | failed | skipped_cap` so an expiry wave is visible as
itself rather than as a generic download failure.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-media-archive.feature`

Scenarios:
- Archive a venue's Instagram posts into `source=instagram_posts` and assert the
  object keys, so folder separation is proven rather than assumed.
- Expand a carousel post into one archived object per child image.
- Stop expanding a carousel once `max_photos_per_venue` is reached, and assert
  the images past the cap were never downloaded.
- Skip a venue with no Instagram handle, count it `no_handle`, and assert **zero**
  Apify calls were made.
- Write a manifest entry per image carrying caption, permalink, shortcode and
  post timestamp.
- Produce the same photo id for the same post across two runs whose signed URLs
  differ — the id-stability guarantee.
- Classify a poster image into the `flyer` category and store it under that
  folder.
- Keep archiving the remaining images when one image download fails.
- Stop the run when Apify reports credit exhaustion, and save the run record.
- Estimate a run and assert zero Apify calls and zero objects written.
- Skip venues already archived under the previous run when `skip_scope` is
  `latest_run`, spending nothing on them.

Pytest unit tests:
- `photo_id_for` on Instagram single, carousel child, and a URL whose signature
  differs between two observations.
- `fetch_recent_posts` parsing: single image, carousel, video post, and an item
  missing `displayUrl`.
- Cost model: `estimate_units` for the Instagram source across post counts and
  carousel settings.
- The cap applies to images, not posts.
- `no_handle` is returned before any client call — assert on call count, not
  just the outcome, because the ordering is the cost guarantee.
- `photo_taxonomy` still validates every existing category after `flyer` is
  added.

Manual or integration checks:
- One bounded live run (2–3 venues, 3 posts each): objects under
  `retrieved/source=instagram_posts/`, manifest correct, `_latest.json` written,
  run record retrievable by `job_id`.
- Re-run with `skip_scope=latest_run` and confirm zero Apify calls.

## Acceptance Criteria
- Instagram post images are stored under
  `retrieved/source=instagram_posts/…/venue_id=…/media/<category>/`, in a folder
  no Google source writes to.
- Every archived image has a manifest entry naming its post, caption, permalink
  and timestamp.
- A photo id is identical across two runs of the same post.
- `max_photos_per_venue` bounds images including carousel children, and images
  beyond it are never downloaded.
- A venue without a handle costs nothing and is reported `no_handle`.
- Posters classify as `flyer`.
- No terraform apply is required for the run to write.
- `make test-feature` and `make test-unit` pass.

## Open Questions
None blocking. Two settings are unverified and surfaced as such: the Apify
Instagram per-post price (an estimate input, like the Google unit price), and
the exact key Apify uses for carousel children — confirmed at execution against
one live response before the parser is finalised, the same way the
`externalUrls` shape drift was handled in `260729_instagram-candidate-loss.md`.
