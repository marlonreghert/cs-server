# Event Source Media — every archived image behind an event, not just one cover

## Branch
feature/event-source-media

## Goal
An operator opening an event in the admin console can see **every image the
pipeline archived for that event**, grouped by the post it came from, and can
tell which image the extractor actually read. That is what makes it possible to
judge whether merging several posts into one item produced complementary
information or lost it.

## Non-goals
- **Any admin console work.** This plan ships the API. The gallery is
  `vibes_bot/plans/260813_event-detail-media-gallery.md`, which consumes it.
- **Feeding more images to the extractor.** §Evidence shows the extractor reads
  one image per post and that a second classified flyer can go unread. Changing
  what the model sees changes extraction output, cost, and `source_event_key`
  stability — a separate decision, deliberately not taken here. This plan makes
  the gap *visible*, which is the prerequisite for deciding it.
- **Re-archiving or back-filling images.** Read-only over what S3 already holds.
- **Videos.** Only archived still images are surfaced. A `Video` post's archived
  frame is an image and is included; the video itself was never archived.
- **Changing the existing `/events/{id}/cover` endpoint.** The console is a
  released client and keeps working unchanged.

## Evidence

### The console shows one image; the archive holds five
`NOITE DA PATROA` (Club Metrópole) is one item merged from three announcing
posts. What exists in `s3://vibesense-datalake-839287955684` under the
2026-08-07 run for that venue:

| post | archived keys | read by the extractor |
|---|---|---|
| `Dbs1FdsEWr7` | `media/flyer/Dbs1FdsEWr7_1.jpg`, `media/Dbs1FdsEWr7_2.jpg` | `_1` only |
| `DbvhZJqkUyf` | `media/flyer/DbvhZJqkUyf_1.jpg`, `media/flyer/DbvhZJqkUyf_2.jpg` | `_1` only |
| `DbtSQngKcPm` | `media/flyer/DbtSQngKcPm.jpg` | yes |

Five archived images. **`DbvhZJqkUyf_2.jpg` is classified `flyer` and was never
read by anything.** The console today shows exactly one image: `EventCover`
fetches a single signed url from `GET /events/{event_id}/cover`, which signs the
item-level `cover_photo_key` (`admin_events_router.py:576`).

### The per-source key is already on the wire and already unusable
`EventSourceOut` has carried `cover_photo_key` since
`260811_event-views-and-post-type-surfaces.md`
(`admin_events_router.py:222-229`), and the console's `EventSourceOut` type
declares it (`admin-ui/src/views/Enrichment/types.ts:45`). Nothing can be done
with it: there is no endpoint that signs a *source's* key, only the item's. The
console renders each source as shortcode + permalink + timestamp
(`components/EventSources.tsx`) with no image.

So the smallest useful version of this feature — three images instead of one for
`NOITE DA PATROA` — is blocked only by a missing presign path.

### One image per post is chosen, by confidence, and the rest are dropped
`event_extraction_service.py:809`:
`image_key = post.flyer_photo_key or post.any_photo_key`, where
`flyer_photo_key` is the highest-`classification_confidence` entry with
`category == flyer` (lines 342-350). Every other archived image for that post is
invisible from that moment on.

This also settles a hypothesis worth recording so it is not re-investigated:
`flyer_names_time` is read off the **same** manifest entry that becomes
`flyer_photo_key`, so the `unread_time` disagreement on `NOITE DA PATROA` is two
model calls disagreeing about one image — **not** the classifier reading a
different carousel slide than the extractor.

### The manifest already holds everything the gallery needs
`EventPostSource` groups `instagram_posts` manifest entries by shortcode, and
each entry carries `key`, `category`, `classification_confidence`, `attributes`
(including `names_time`), and `post_type`
(`event_extraction_service.py:249-362`). The gallery needs no new archive data —
only a read path scoped to one event.

## Current Behavior
`GET /events/{event_id}/cover` returns a single short-lived signed url for the
item's own `cover_photo_key`, or 404 when there is none. Nothing exposes the
other archived images, and nothing exposes any per-source image.

## Desired Behavior
1. One request returns every archived image for an event, grouped by source
   post, each with a short-lived signed url.
2. Each image says what it is: its classified category, its confidence, and
   whether it is the one the extractor read.
3. Sources are ordered oldest post first, so a merged item reads as a timeline.
4. An event with no archived media returns an empty result, not an error.
5. No client can cause an arbitrary S3 key to be signed.

## Implementation Approach

### A. One batch endpoint, not one presign per image
`GET /events/{event_id}/media` returns the sources with their images and signed
urls in a single response. Per-image round trips would make a three-post event
cost six requests and would spread the presign policy across call sites.

Reuse `settings.event_cover_presign_expires_seconds` — one expiry policy for
event imagery, not two.

### B. Resolve the images from the item's own sources, server-side
Never accept a key from the client. For each of the event's
`post_item_source` rows:

1. Take that source's stored `cover_photo_key`. It encodes the run partition
   (`…/run_id=<R>/venue_id=<V>/media/…` or `…/promoter=<H>/media/…`).
2. Derive that run's manifest path from the key's own prefix and read it — one
   S3 GET per distinct run, and a merged item's sources usually share one run.
3. Keep the entries whose shortcode matches the source's `source_shortcode`.
4. Sign each resulting key.

**Do not walk `list_run_prefixes` from a date.** That is the crawl-time scan
(`_manifests_since`), sized for a batch job; driving it from an interactive
endpoint would read every run since the lookback to serve one event.

Fallback when the manifest is missing or unreadable: sign the source's stored
`cover_photo_key` alone. The operator then sees one image per post — today's
behaviour extended to every source — rather than an error. Count this fallback
(§Error Handling); a manifest that cannot be read is a real archive problem.

### C. Say which image the extractor read
Mark the image whose key equals the source's `cover_photo_key`. That is exactly
`flyer_photo_key or any_photo_key` as stored at extraction time, so the flag is
a recorded fact rather than a re-derivation that could drift from what the model
actually saw.

Carry each image's `category` and `classification_confidence` through
unchanged — an operator judging "is complementary information working" needs to
see that the unread image was classified `flyer` at high confidence, which is
precisely what makes it interesting.

### D. Keep the same admin gate
The route sits behind `_require_admin`, exactly like `/cover`. A signed url is a
bearer credential for that object until it expires and must never be logged —
the existing cover path already establishes both rules.

## Data, Config, And API Impact
- **Migration** — none. Read-only over RDS and S3.
- **Config** — none new; reuses `event_cover_presign_expires_seconds`.
- **Admin API** — one additive endpoint, `GET /events/{event_id}/media`.
  Response: an ordered list of sources, each with its shortcode, permalink,
  post timestamp and media type, and its images (signed url, category,
  confidence, and whether it was the one read). `/events/{id}/cover` is
  untouched.
- **Serving projection** — untouched.
- **Rollback** — revert; the endpoint disappears and the console falls back to
  the single cover (the consuming plan must degrade, not break — pinned as a
  cross-repo contract).

## Error Handling And Observability
- 404 only when the event does not exist. An event with no archived media
  returns an empty source list with 200 — "nothing archived" is an answer, and
  this repo has already shipped one bug from treating absence as an error.
- A per-image signing failure omits that image and is counted; it must not fail
  the whole response and lose the images that did sign.
- Count responses by outcome (`manifest_read`, `manifest_fallback`,
  `no_media`), and count images returned per response. A rising
  `manifest_fallback` means archived manifests are going unreadable, which is
  invisible everywhere else.
- Never log a signed url.

## Test Plan
Feature file: `tests/bdd/api/event-source-media.feature`

Scenarios:
- Return every archived image for an event announced by three posts.
- Group the images under the post each came from.
- Order the sources oldest post first.
- Mark the one image the extractor read, per source.
- Report each image's classified category and confidence.
- Return an empty list, with 200, for an event with no archived media.
- Fall back to the stored cover key when a run manifest cannot be read.
- Omit an image whose url could not be signed and still return the others.
- Refuse the request without the admin credential.
- Never sign a key supplied by the caller.
- Leave `GET /events/{id}/cover` behaving exactly as before.

Pytest unit tests:
- Deriving a run's manifest path from a `venue_id=`-partitioned key and from a
  `promoter=`-partitioned key.
- Matching manifest entries to a source by shortcode, including the `_1`/`_2`
  suffix forms and the unsuffixed form.
- The read-by-the-extractor flag: exactly one image per source when the stored
  `cover_photo_key` is present, none when it is null.
- One S3 manifest read per distinct run when several sources share a run.

Manual or integration checks:
- Against production, request the media for `NOITE DA PATROA` and confirm five
  images across three sources, with `DbvhZJqkUyf_2.jpg` present and marked as
  not read.

## Acceptance Criteria
- `GET /events/{event_id}/media` returns every archived image for the event,
  grouped by source, oldest first, each with a working signed url.
- The image the extractor read is identifiable, per source.
- No caller-supplied key is ever signed.
- An event with no media returns 200 and an empty list.
- `/events/{id}/cover` is unchanged.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
