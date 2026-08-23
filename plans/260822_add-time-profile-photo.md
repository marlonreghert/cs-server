# Capture the Instagram profile photo at add time

**Status:** implemented, NOT deployed (held while the 517-venue backfill runs)
**Branch:** `feature/add-time-profile-photo`
**Reported:** "is this flow in add_venues? it should be, when adding a venue,
if it has handler, we should get the photo and process it (remove grey borders)"

## The gap

`AddVenueHandler.add()` already runs `_discover_instagram_handle(venue)` inline,
so an added venue gets its handle immediately. It never captured the photo. The
photo only arrived when the scheduled job next swept, and that job is
`IntervalTrigger(hours=24)` behind a 200-venue cap — so a venue added today
waited up to a day, and longer whenever the due queue exceeded the cap (517 at
the time of writing). In the list it showed an emoji placeholder the whole time.

## What "process it (remove grey borders)" already does

Nothing to add: `sample_edge_color(data)` runs inline in the capture path
(`venue_profile_photo_service.py`, in `_process_venue`'s store branch), on the
bytes it just downloaded. Every fresh capture records the colour the card paints
behind a fitted avatar. Confirmed live during the backfill —
`projected_venues` and `edge_color_venues` moved in lockstep, 745 -> 888 -> …

The separate `mode: edge_color` exists only to retrofit rows captured *before*
that feature shipped. It is not part of the normal capture path.

## The change

`VenueProfilePhotoService.capture_for_venue(venue_id, handle)` — one venue, now.

- **Every spend gate is the job's own**, not a second copy: the enable flag, the
  media-store/CDN config check, `_has_current_photo` (age-blind, handle-aware)
  and `_attempt_suppresses` (the 7-day negative cache). A re-add of a venue that
  already has a photo buys nothing.
- **The per-run cap deliberately does NOT apply.** It bounds a catalog sweep;
  this is one venue the operator just paid to add, and capping it could only
  ever mean "silently skip" — worst exactly when the backfill queue is longest.
- **A missing handle is resolved from the store**, not treated as absent. That
  is the normal shape of a recovered / geo-linked add, where discovery reports
  `skipped` *because* the venue already carries a handle. `Venue` has no handle
  field (it is on `MinifiedVenue`), which is why the lookup lives in the service.
- `AddVenueHandler._capture_profile_photo` mirrors `_discover_instagram_handle`'s
  contract exactly: deadline-bounded, never raises, never fails the add, every
  outcome counted on its own `add_venue_profile_photo_total`.

Settings: `add_venue_profile_photo_enabled` (default **on**) and
`add_venue_profile_photo_deadline_seconds` (60). Off restores the previous
behaviour exactly — the backfill still picks the venue up.

## Cost

$0.003 per added venue that has a handle and no photo, on top of the discovery
spend the add already makes. It is not additional spend in aggregate: it is the
backfill's spend, moved earlier. A venue is bought once either way.

## A deliberate honesty note

The deadline bounds the ADD, not the spend. On timeout the Apify call may still
be running and may still be billed; the log says so rather than letting a
timeout read as free.

## Verification

- 9 unit tests on `capture_for_venue`, including the two that matter: a venue
  that already has a photo, and one inside the negative-cache window, each
  asserting `apify.calls == []` rather than merely checking the outcome string.
- **Sabotage-verified**: removing both spend gates turns exactly those two red.
- Full cs-server suite green: 3,690 passed, 15 skipped.
