# Classify From Bytes, And Give Every Event A Durable Cover

## Branch
fix/classify-bytes-and-durable-covers

## Goal
Make Instagram photo classification actually work, and make every event point at
the archived image rather than only at a link that will rot. Four defects found
by a production RCA on 2026-08-07, all in the same failure chain.

## Non-goals
- **Re-classifying photos already archived without a category.** Those objects
  exist under `media/<photo_id>` with no category segment; moving them is a
  different job (`docs/venue-retrieval-storage.md` §4 says recategorising means
  moving objects). A fresh run archives correctly; the back-fill is separate.
- **Changing the taxonomy, the extraction prompt, or the resolution ladder.**
- **The prod `config/cs-server.json` model pins** (it overrides
  `vibe_classifier_*` and `instagram_judge_model` back to gpt-5.4, so #138 is
  only half-live). Real, but a config decision for the operator, not code.
- **Any vibes_bot change.** Its console already derives `has_cover` from
  `cover_photo_key`; fixing defect 3 lights the viewer up with no UI work.

## Evidence

All four were confirmed against production, not inferred.

**Defect 1 — OpenAI cannot fetch Instagram CDN urls.** The classifier hands
OpenAI an image **url** to download server-side
(`app/api/openai_photo_classifier_client.py`, whose docstring says it "takes a
plain list of image urls"). That is fine for Google — `lh3.googleusercontent.com`
is openly fetchable — and fatal for Instagram. Reproduced live against a fresh
url:

```
400 invalid_image_url
Error while downloading https://instagram.fper12-1.fna.fbcdn.net/v/t51.82787-15/...
```

Production consequence, from Prometheus:

```
openai_api_calls_total{endpoint="photo_classify", status="error"} = 1
photo_classification_fallbacks_total{reason="classify_error"}     = 5
photo_classification_total                                        = no samples
```

Every archived Instagram photo therefore has **no category**: the S3 keys are
`media/<photo_id>.jpg` with no `<category>/` segment, and the manifest entries
carry `caption`, `permalink`, `shortcode`, `uploaded_at`, `carousel_index` —
everything except `category`.

**The bytes are already in hand.** Classification runs *between the fetch and the
store* (`docs/venue-retrieval-storage.md` §4: "so a photo lands in the right
folder the first time: no second write, no object copy"). The pipeline is
holding the downloaded image when it calls the classifier and passes a url
anyway.

**Defect 2 — the error message sends you to the wrong place.** The
`BadRequestError` handler logs `request rejected parameter '{e.param or '?'}'`
unconditionally. `invalid_image_url` carries `param: None`, so the live failure
read as:

```
[PhotoClassifier] request rejected parameter '?' for model gpt-5.6-luna
```

An operator would hunt a model-parameter bug. The actual cause is an image
download. A diagnostic that names the wrong subsystem is worse than none.

**Defect 3 — promoter events never reference their archived image.**
`cover_photo_key` is written in exactly one place,
`app/services/event_extraction_service.py:484` (the venue path).
`app/services/promoter_crawl_service.py` archives post images to S3
(`retrieved/source=instagram_posts/.../promoter=<handle>/media/<photo_id>.jpg` —
verified present in the bucket) and then never records where they went. So a
promoter event carries only `source_permalink`.

That is the exposure the operator raised: an Instagram permalink is not durable.
The post can be edited or deleted, and the image behind it is a signed CDN url
that expires within the hour. We pay to archive the bytes and then keep only the
perishable pointer.

**Defect 4 — the pipeline is inert at its default bound.** Live targeting:

```
category_pass = 771   excluded_category = 661
evidence_rejected = 23   unevaluated = 2   evidence_confirmed = 0
event_candidate_venues{tier="category_candidate"} = 746
```

`max_evidence_venues` defaults to **25** against 771 category survivors, so 746
venues were never evaluated and **zero** were confirmed. Extraction's eligibility
resolves to `evidence_confirmed`, so the venue path examined no posts at all
(`event_extraction_posts_total` has no samples).

The bound was sized as if the evidence gate were the expensive stage. It is not:
`260804_event-venue-targeting.md` §B states it makes zero model calls and zero
external API calls. A cost control on a free stage bought nothing and made the
happy path unreachable.

## Current Behavior
Instagram photos archive uncategorised because their classification call 400s;
the failure is logged as a parameter rejection; promoter events reference only a
perishable permalink; and the evidence gate reaches 25 of 771 candidates,
confirming none.

## Desired Behavior
1. Classify from the image **bytes** already downloaded, never by asking the
   provider's CDN to serve OpenAI.
2. Keep classifying by url where that is the only option (re-deriving attributes
   over an archived run reads presigned S3 urls), so both inputs are supported.
3. Report an image-fetch failure as an image-fetch failure, naming the host —
   distinct from a rejected parameter, in both the log and the metric.
4. Record `cover_photo_key` on every event a promoter crawl produces, pointing
   at the archived object.
5. Keep `source_permalink` too: the permalink is where an operator sees the post
   in context, the archived key is what survives the post being deleted.
6. Evaluate the whole category-gate output by default, and skip venues that
   cannot produce evidence before doing any S3 work.

## Implementation Approach

### A. Send bytes, not a url
The classifier accepts either an image url or a data URI today — the message it
builds is the same shape. So the change is in the **caller**: the archive
pipeline passes the bytes it just downloaded, encoded as a data URI, instead of
`photo["source_url"]`.

**Do not make this a new code path.** `rederive_attributes` re-classifies an
archived run and legitimately has no bytes in memory; it presigns S3 instead.
Both must keep working, so the classifier's input stays "a list of image
references, url or data URI" and only the live path changes what it supplies.

Worth stating because it is the trap: a data URI costs request *bytes* where a
url costs none. Image payloads inflate the request, and the batch is 10 photos.
That is a bandwidth cost, not a token cost — OpenAI bills the decoded image the
same either way — but a 10×300KB batch is a ~4MB request after base64, so the
batch size and any client timeout must be checked against real images rather
than assumed.

### B. Name the failure correctly
Branch on the error's `code`. `invalid_image_url` logs as an image-fetch failure
with the offending host (never the full signed url — it is a credential), and
increments a distinct metric label. A genuine parameter rejection keeps today's
message. Anything else keeps a generic branch.

The distinction is the point: after §A, an `invalid_image_url` should become
impossible on the live path, so if it reappears it means something re-introduced
a url — and the metric must say so plainly.

### C. Promoter events carry their cover
`_archive_post_images` already returns the stored objects; the reconciliation
that builds each event's fields must carry the first archived image's key onto
`cover_photo_key`, exactly as the venue path does.

**One post, several events, one image each.** Aligning a specific carousel slide
to a specific event is explicitly out of scope
(`260806_multi-event-posts.md`), so every event from a post gets that post's
cover. Better than null, and honest about what it is.

An event whose post yielded no storable image keeps `cover_photo_key` NULL and
the console's "no archived image" state — that is a fact, not a failure.

### D. Make the evidence gate reach the candidates
Two changes, both cheap:

- Raise the `max_evidence_venues` default so a default run covers the real
  category-gate output rather than 3% of it. The bound stays a control an
  operator can lower; it stops being a silent floor.
- **Skip a venue with no Instagram handle before any S3 listing.** It can
  produce no evidence by construction and is already recorded `unevaluated`;
  today that verdict is reached only after walking manifests. This is what makes
  a full-catalog pass affordable — the expensive part of the "free" gate is S3
  traffic, and almost every venue in the catalog has nothing archived.

## Data, Config, And API Impact
- **Migration:** none. `cover_photo_key` already exists on `events.event`.
- **Settings:** `max_evidence_venues` default changes (job registry
  `default_config`). No new setting.
- **API:** none. `GET /admin/events` already returns `cover_photo_key`, and
  vibes_bot already derives `has_cover` from it.
- **Behaviour:** newly archived Instagram photos land under a real category
  folder; previously archived ones are untouched.
- **Rollback:** revert. Nothing is destructive and no schema moves.

## Error Handling And Observability
Classification stays an enhancement, never a dependency: a failure still leaves
the photo archived under its source category, per §4 of the storage doc.

Metrics:
- `photo_classification_fallbacks_total` gains a reason distinguishing an image
  the model could not fetch or decode from a parameter rejection — the two need
  different fixes and must not share a label.
- Existing `openai_api_calls_total{endpoint="photo_classify"}` unchanged.

Never log a signed url — provider CDN links and presigned S3 urls are both
bearer credentials for that object. Log the host only.

## Test Plan
Feature file: `tests/bdd/enrichment/classify-bytes-and-durable-covers.feature`

Scenarios:
- Classify an archived Instagram photo from its bytes and file it under the
  classified category.
- Send no provider url to the model on the live archive path — asserted on what
  the client actually received, not on the outcome.
- Keep classifying by url when re-deriving attributes over an archived run.
- Report an image the model could not fetch as an image failure, not a
  parameter rejection, and count it under its own reason.
- Keep the failing photo archived under its source category.
- Record the archived cover key on an event produced by a promoter crawl.
- Record the same cover on every event from a multi-event promoter post.
- Leave `cover_photo_key` NULL when the post stored no image, without failing
  the event.
- Keep `source_permalink` alongside the cover key.
- Evidence-evaluate the whole category-gate output under the default bound.
- Skip a venue with no Instagram handle before any archive listing — asserted on
  the listing call count, since the saving is the point.

Pytest unit tests:
- The live path builds a data URI from the downloaded bytes, with the right
  content type, and no `http` reference reaches the client.
- The re-derive path still passes presigned urls.
- Error classification across `invalid_image_url`, a real parameter rejection,
  and an unknown 400.
- No signed url appears in any log record.
- Promoter cover assignment: single image, several events from one post, and no
  image at all.
- The handle-less short-circuit performs zero S3 calls.

Manual or integration checks:
- Re-run the Instagram archive over the same Métropole venue and confirm the new
  objects land under `media/<category>/` with `category` present in the
  manifest — the exact thing production got wrong.
- Then confirm the event review queue shows an inline flyer for a promoter
  event.

## Acceptance Criteria
- An Instagram archive run classifies its photos; keys carry a category segment
  and manifests carry a `category` field.
- No provider CDN url is sent to OpenAI on the live archive path.
- Re-deriving attributes over an archived run still works by url.
- An unfetchable image is reported as such, with its own metric reason, and the
  photo stays archived.
- Promoter events carry `cover_photo_key` alongside `source_permalink`.
- A default targeting run evaluates the full category-gate output and performs
  no S3 work for handle-less venues.
- No signed url is logged.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None. The batch size is left at 10 and re-checked against real image payloads
during the manual run rather than changed speculatively.
