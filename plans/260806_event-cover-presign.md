# Event Cover Presign — a viewable image for the review queue

## Branch
feature/event-cover-presign

## Goal
Publish an admin endpoint that turns an event's archived cover photo into a
short-lived, viewable image URL, so an operator reviewing an extracted event can
actually see the flyer it was read from.

## Non-goals
- **Making the bucket public, or widening any IAM grant.** The grant needed
  already exists.
- **Serving images to the app.** `retrieved/` stays internal-use only. This is
  an admin path behind admin auth.
- **A general "presign any key" endpoint.** See the security note — the key is
  never client-supplied.
- **Rendering.** vibes_bot owns the viewer UI.

## Evidence

**The review queue is not reviewable without this.** vibes_bot's
`app/services/event_console_presentation.py:resolve_flyer_source` currently
returns the Instagram permalink, and falls back to emitting the raw
`cover_photo_key` string with a comment recording the gap: cs-server "does not
yet publish an admin endpoint that turns `cover_photo_key` into a presigned
URL (its only `presign()` caller today is the internal vision-model pipeline,
not an HTTP route)".

**A permalink cannot be an image.** Instagram post URLs are pages, not image
resources; they cannot be used as an `<img src>` and Instagram blocks framing.
So the permalink is the right "open the original post" link and can never be the
inline viewer. The archived copy is the only image we can actually display.

**The capability is already there.** `MediaArchiveStore.presign(key, expires_in)`
(`app/dao/media_archive_store.py:284`) returns a time-limited GET url and returns
`None` rather than raising. `infra/datalake/iam.tf` already grants
`s3:GetObject` on `retrieved/*`, and its comment says that grant exists
precisely because "a presigned URL is only valid if the SIGNER holds GetObject".
**No terraform change.**

**The key is already stored.** `EventExtractionService._extract_one` writes
`cover_photo_key` on every event it creates.

## Current Behavior
`presign()` is only ever called by the vision pipeline. No HTTP route exposes it,
so the console can state that an archived copy exists but cannot show it.

## Desired Behavior
1. Serve a time-limited image URL for a given event's archived cover photo.
2. Take an **event id** and resolve the key server-side from that event's row.
3. Return 404 when the event does not exist or has no `cover_photo_key`, and a
   clear error when signing fails — never a broken or empty URL.
4. Keep the URL short-lived, and say when it expires so a client can refresh it
   rather than caching a dead link.
5. Require admin auth, like every other admin route.

## Implementation Approach

`GET /admin/events/{event_id}/cover` → `{"url": ..., "expires_at": ...,
"expires_in": ...}`.

**The endpoint accepts an event id, never a key.** A route that presigns a
client-supplied S3 key is an arbitrary-object-read primitive against the whole
lake — every `raw/` and `retrieved/` object, from an authenticated admin session
or anything that borrows one. Resolving the key from the event row means the
reachable set is exactly "cover photos of events that exist", which is the set
the feature needs and nothing more. This is the single security decision in the
plan and it is not negotiable for convenience.

Returning JSON rather than a 302 redirect keeps the expiry visible to the
client, so the console can refresh an expired link instead of rendering a broken
image. A redirect would hide the expiry inside a URL nobody inspects.

Expiry is short (the existing 900s default) and configurable. It is handed to a
browser rather than a model, but the reasoning is the same as the vision path's:
the URL *is* the grant.

Register the route beside the other specific `/admin/events/...` paths and
**before** `/{event_id}`, matching the ordering fix already made in this router —
a catch-all registered first swallows it into a 404.

## Data, Config, And API Impact
- **Migration / persistence:** none.
- **API:** one new admin route.
- **Settings:** `event_cover_presign_expires_seconds` (default 900).
- **IAM / terraform:** none — `GetObject` on `retrieved/*` already granted.
- **Serving:** none. No app-facing response changes.

## Error Handling And Observability
Unknown event, or an event with no cover key → 404 with a reason distinguishing
the two, because "this event has no archived image" and "no such event" send an
operator to different places. A `presign()` failure (it returns `None`) → 502,
never a 200 carrying a null url.

Metric: `event_cover_presign_total{result}` — `signed`, `no_key`, `not_found`,
`failed`.

Do not log the signed URL. It is a bearer credential for the object for its
lifetime; logging it puts a readable grant into Loki.

## Test Plan
Feature file: `tests/bdd/api/event-cover-presign.feature`

Scenarios:
- Return a signed url and its expiry for an event with an archived cover.
- Return 404 for an event with no cover photo key, distinguishable from a
  missing event.
- Return 404 for an unknown event id.
- Return 502 when signing fails, and never a 200 with a null url.
- Require admin authentication.
- Resolve the key from the event row and never from the request — a request
  carrying a key-like parameter must not influence which object is signed.
- Keep the signed url out of the logs.

Pytest unit tests:
- Route ordering: `/admin/events/{id}/cover` resolves rather than being consumed
  by `/{event_id}`.
- Expiry arithmetic: `expires_at` matches `expires_in` against a frozen clock.
- `presign()` returning `None` maps to 502.
- The metric's four result values.

Manual or integration checks:
- Against a real bucket: fetch a cover url for a live event and confirm it loads
  in a browser and stops working after expiry.

## Acceptance Criteria
- An event with an archived cover yields a working, time-limited image url.
- No request input can change which object is signed.
- Missing event and missing cover are distinguishable.
- A signing failure never returns 200.
- No terraform change is needed.
- No signed url appears in logs.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None.
