# Instagram Handle In The Venue Inventory Payload

## Branch
feature/instagram-handle-in-venue-inventory

## Goal
Return each venue's discovered Instagram handle from
`GET /admin/venues/inventory`, so the admin panel's venue table can show the
handle itself rather than only a yes/no coverage dot.

## Non-goals
- Any change to discovery, scoring, or persistence. This surfaces a value that
  is already stored and already fetched.
- Changing the `cache_flags` shape. The boolean stays exactly as it is; panels
  and tests that read it must keep working.
- The venue-detail endpoint. `/api/venues/{id}` is vibes_bot's own Redis read and
  already returns the full Instagram section.
- Backfilling or re-running discovery for venues without a handle.

## Evidence
- `app/routers/admin_trigger_router.py:1072` — `list_venue_inventory` builds each
  item from a fixed dict: `venue_id`, `venue_name`, `venue_address`, lat/lng,
  lifecycle/deprecation fields, `google_business_status`, and `cache_flags`. The
  Instagram handle is not among them, so the panel cannot display it.
- `app/routers/admin_trigger_router.py:1049` — `_venue_cache_flags_bulk` **already
  fetches the full record**: `ig_map = venue_dao.get_venue_instagram_bulk(venue_ids)`.
  It then reduces it to `"instagram": vid in ig_map` and throws the value away.
  The data is in hand; nothing new needs reading.
- `app/models/instagram.py:37` — `VenueInstagram` carries `instagram_handle`,
  `instagram_url`, `confidence_score`, `status`, and `source`. `bio` and
  `followers_count` exist but the cascade does not populate them, so they are
  not worth surfacing.
- Production values confirm what is worth showing: a handle is only meaningful
  alongside its confidence and tier — `venue_website` at `0.78` and
  `google_search` at `0.80` are very different kinds of evidence, and the
  operator judging a handle needs to see which one produced it.

## Current Behavior
Every inventory row carries `cache_flags.instagram`, a boolean. The panel renders
it as a coverage dot. An operator who wants to know *which* handle a venue has
must open the venue detail view or query Redis directly.

## Desired Behavior
Each inventory item must additionally carry an `instagram` object for venues that
have a record, and `null` for those that do not:

```
"instagram": {
  "handle": "champagne_recifee",
  "url": "https://instagram.com/champagne_recifee",
  "status": "found" | "low_confidence" | "not_found",
  "confidence": 0.78,
  "source": "venue_website" | "google_search" | ... | null
}
```

A `not_found` record must return the object with a null handle rather than
`null` — "we looked and found nothing" and "nobody has looked" are different
states, and the panel should be able to tell them apart.

`cache_flags.instagram` must keep its exact current meaning and value.

## Implementation Approach
Read the handle out of the `ig_map` that `_venue_cache_flags_bulk` already
builds, instead of discarding it.

The cleanest shape is to have that helper return the records alongside the flags
(or return the flags plus a parallel handle map) and have `list_venue_inventory`
project the fields above onto each item. Keep the bulk read as one call per page
— the reason the helper exists is that per-venue reads cost ~16 round trips per
page, and this must not reintroduce that.

Serialize defensively: a stored record predating the `source` field, or one with
a missing `confidence_score`, must produce nulls rather than raising and taking
the whole inventory listing down with it. The listing is a troubleshooting
surface; it has to survive imperfect rows.

## Data, Config, And API Impact
**API (additive):** one new `instagram` key per item on
`GET /admin/venues/inventory`. No existing field changes name, type, or meaning,
so a caller that ignores it behaves exactly as today.

**Persistence:** none. No new Redis reads, no new keys, no migration.

**Config:** none.

## Error Handling And Observability
No new external calls and no new failure mode: the bulk read already happens and
is already inside the endpoint's try/except. The added work is pure projection
over data in memory.

A malformed individual record must degrade to a null-ish `instagram` object for
that venue only — never a 500 for the whole page. No new metrics: this adds no
runtime path worth counting.

## Test Plan
Feature file: `tests/bdd/api/instagram-handle-in-venue-inventory.feature`

Scenarios:
- A venue with an accepted handle returns it in its inventory item, with the
  confidence and the tier that found it.
- A venue whose discovery concluded `not_found` returns the object with a null
  handle, distinguishable from a venue nobody has looked at.
- A venue with no Instagram record at all returns `instagram: null`.
- `cache_flags.instagram` keeps its current boolean value in every case above.
- Every other inventory field is unchanged.
- A page of venues issues one bulk Instagram read, not one per venue.
- A malformed stored record degrades that one item without failing the listing.

Pytest unit tests:
- `tests/test_admin_venue_inventory.py` (or the existing equivalent) — the
  projection for found / low_confidence / not_found / absent records, and the
  defensive path for a record missing `source` or `confidence_score`.

Manual or integration checks:
- Against prod data, confirm the row for a known venue carries its real handle
  (`CHAMPAGNE CLUB` → `champagne_recifee`, `venue_website`, `0.78`).

## Acceptance Criteria
- An inventory item for a venue with a handle carries the handle, url, status,
  confidence, and source.
- "Looked and found nothing" is distinguishable from "never looked".
- `cache_flags.instagram` is unchanged in value and meaning.
- The page still performs one bulk Instagram read.
- A malformed record cannot fail the listing.

## Open Questions
None.
