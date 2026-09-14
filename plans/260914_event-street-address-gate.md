# Serve an event only when its venue has a street-level address on file

## Branch
feature/event-street-address-gate

## Goal

Add a serving-projection gate: an event is never surfaced to the app unless
the venue it is linked to has a specific, street-level address on file —
closing the confidence gap `260914_promoter-roundup-caption-mention.md`
exposed, where an event could resolve confidently to a real catalog venue
whose own location is itself barely known.

## Non-goals

- **Not a venue-servability rule.** `serving.eligible_venue` (consulted by
  `RdsVenueStore.list_events_for_projection`) already governs whether a
  VENUE is open/eligible/servable at all; this plan does not touch it and
  does not duplicate its concerns (blocked venues, closures, etc. stay
  exactly as they are).
- **Not a check on the raw post caption/`location_text` containing a street
  address.** Investigated and rejected — see Evidence: almost no venue's own
  Instagram account states its own street address in a caption (it doesn't
  need to; its followers already know where it is), so gating on the RAW
  TEXT evidence would exclude most of the catalog's legitimate venue-post
  events, which is not what "an event must not be surfaced unless [it] gives
  a specific street-level address" can sensibly mean once the actual data is
  looked at. The predicate instead reads the RESOLVED VENUE's own stored
  `venues.address.street` — see "Where the address lives" below for why this
  reading is the one the evidence supports.
- **Not scoped to promoter-sourced events only.** Investigated — see
  Evidence: address completeness is a fact about the VENUE, not about who
  posted about it, and the measured blast radius is identical (zero) whether
  scoped or not. Scoping it to promoter events only would leave the same
  confidence gap open for a venue-post event whose own venue's address
  happens to be incomplete, or for a future venue force-reassigned by
  `scripts/backfill_event_venue_links.py --mode force-reassign` to a
  street-incomplete venue. Applying the gate universally is simpler, costs
  nothing measured today, and closes the gap for every path that writes
  `venue_id`, not just the promoter one.
- **Not a backfill of `venues.address`.** No currently-live, currently-
  servable event is excluded by this gate (measured below) — there is
  nothing to backfill to preserve today's serving surface. Improving
  address coverage for the 101 street-incomplete venues (none of which
  currently have any live events) is the address-backfill pipeline's own
  ongoing work (`plans/260906_address-components-backfill.md` and
  successors), not this plan's.
- **Not a change to `get_address`/`get_address_bulk`'s existing column
  lists** beyond what `_EVENT_SELECT`'s own join needs (see Implementation
  Approach) — `get_address_bulk` stays the narrow lat/lng/neighborhood
  projection it already is for its existing caller.

## Evidence

Queried live against production RDS on 2026-09-14. File/line references
re-read from this worktree on `main` at commit `646e055`.

### Where the address lives, and how complete it is

- `venues.address` (`app/dao/rds_venue_store.py:435-454`, `get_address`) has
  `street`, `neighborhood`, `city`, `postal_code`, `raw_text`, `lat`, `lng`,
  plus provenance `*_source` columns per structured field — built by
  `plans/260906_address-components-backfill.md`/`260906_events-venue-bairro-
  and-ticket-url.md` and their successors. `get_address_bulk`
  (`rds_venue_store.py:1446-1465`) is a narrower `lat, lng, neighborhood`
  projection built specifically for the events projection's existing venue
  join — it does not currently select `street` at all.
- Catalog-wide: 3,550 of 3,651 venues (97.2%) have a non-blank `street`; 101
  do not.
- **Of the currently-servable event population** (`post_type='event'`,
  `status IN ('accepted','confirmed')`, `venue_id IS NOT NULL`,
  `superseded_by IS NULL` — the exact `is_selectable` row-local shape),
  **100% (529/529) map to a venue with a non-blank `street`.** The
  `has_street=false` bucket returned zero rows; a follow-up query for "5
  servable events at a street-incomplete venue" returned an empty result set.
  **Every one of the 101 street-incomplete venues currently has zero live
  events.** This holds even for the ladder's `vsg_`-prefixed venue ids seen
  in `260914_promoter-roundup-caption-mention.md`'s own evidence — those
  currently-servable rows also resolve to a `venues.address` row with a
  street.
- Conclusion: **this gate's measured current blast radius is zero.** It is
  a forward guard, not a remediation of any currently-wrong serving — the
  `oquetemhojeemnatal` incident itself is already fully addressed by
  `260914_promoter-roundup-caption-mention.md` and needs nothing from this
  gate to be fixed.

### The established pattern for a venue-level fact inside a row-local predicate

`is_selectable`'s own docstring (`app/services/event_projection_selection.py:1-25`)
draws a boundary: the pure function covers only what a single
`events.post_item` row can answer about itself, explicitly excluding "venue
servability (external: `serving.eligible_venue`)". But it ALSO explicitly
folds in `venue_name` and `last_seen_at` — both really joined-in, cross-table
facts — specifically because `_EVENT_SELECT`'s own `LEFT JOIN`/`LATERAL`
already puts them on the row BEFORE the predicate runs, "so no second,
per-row fetch is needed." That is the precedent this plan follows for
`street`: joined onto the row the same way `venue_name` already is, so it
becomes row-local by the time `is_selectable` sees it — and, critically, so
the in-memory fake store (which "calls this function directly," per the same
docstring) can enforce and BDD-test the exact same rule the real SQL does,
which a venue-level `serving.eligible_venue`-style subquery could not give
us without a second, parallel implementation in the fake.

### Why not model this the other way, as a `serving.eligible_venue`-style SQL subquery instead

`list_events_for_projection` (`app/dao/rds_venue_store.py:1381-1438`) already
adds one venue-level condition this way:
`e.venue_id IN (SELECT venue_id FROM serving.eligible_venue)`. That view
encodes venue open/closed/blocked eligibility — a materialized DB view with
its own maintenance path, appropriate for a multi-column, evolving
eligibility rule. A single nullable-string check on `venues.address.street`
does not need a view; joining it onto `_EVENT_SELECT` (already done for
`venue_name`) is simpler and keeps the rule inside the one place
(`is_selectable`) that is already pinned by a Python/SQL parity test.

## Current Behavior

- `is_selectable` (and `RdsVenueStore.list_events_for_projection`'s SQL
  transliteration of it) never look at `venues.address` at all. An event
  auto-linked to a venue whose address is nothing but a raw, ungeocoded
  string is served exactly like one linked to a venue with a full street
  address.

## Desired Behavior

- An event is selectable only when, in addition to every existing
  `is_selectable` criterion, its linked venue's `venues.address.street` is
  present and non-blank — gated by a new, live-read admin-config flag so an
  operator can disable it with no deploy if address data quality regresses.
- The flag defaults **ON**: the measured blast radius is exactly zero, so
  shipping it enabled changes no currently-served row while immediately
  closing the gap for the next low-confidence resolution.

## Implementation Approach

One branch, single-purpose commit(s).

1. `app/models/event_address_completeness.py` (new, mirrors
   `app/models/promoter_event_visibility.py`'s exact shape):
   `ADMIN_CONFIG_REQUIRE_STREET_ADDRESS_KEY =
   "admin_config:event_require_street_address"`,
   `DEFAULT_REQUIRE_STREET_ADDRESS = True`,
   `validate_require_street_address_config` (plain boolean, raises on
   anything else), `load_require_street_address` (Redis-mirror-with-
   fallback, never raises, `fallback_reason` on any problem), and a pure
   `has_street_address(street: Optional[str]) -> bool` (`street is not None
   and street.strip() != ""`) that both the Python predicate and any future
   caller share — one definition of "blank," not two.
2. `app/container.py`: register `event_require_street_address` in the
   admin-config validator map, alongside `hide_promoter_events` and
   `post_category_vocabulary`.
3. `app/dao/rds_venue_store.py`: extend `_EVENT_SELECT` with
   `LEFT JOIN venues.address adr ON adr.venue_id = e.venue_id` and add
   `adr.street AS venue_street` to the column list (mirrors the existing
   `LEFT JOIN venues.venue v` / `v.venue_name` pair exactly — same join
   shape, same nullability contract for an unmapped/unaddressed venue).
   `list_events_for_projection`'s WHERE clause gains
   `AND (:require_street_address = false OR (adr.street IS NOT NULL AND
   btrim(adr.street) <> ''))`, with the live admin-config value threaded in
   as a bound parameter exactly like `recurring_cutoff` already is.
4. `app/services/event_projection_selection.py`: `is_selectable` gains one
   new keyword parameter, `require_street_address: bool = False` (pure-
   module default, matching `max_recurring_source_age`'s own pattern — every
   production caller passes the LIVE config value explicitly). When True,
   `row.get("venue_street")` must pass `has_street_address`.
5. The in-memory fake store applies `is_selectable` directly (existing
   parity property, `event_projection_selection.py`'s own docstring) — needs
   its `_merged_view`/event fixture shape extended with a `venue_street`
   field sourced the same way `venue_name` already is, so a BDD scenario
   against the fake is evidence the real SQL implements the same rule.

## Data, Config, And API Impact

- New admin-config key `admin_config:event_require_street_address` (bool),
  default `true`, validated/persisted through the existing
  `AdminConfigService` dispatch — same shape as `hide_promoter_events`.
- `is_selectable`'s signature gains one new keyword parameter with a
  pure-module default (non-breaking for existing callers/tests that don't
  pass it).
- `_EVENT_SELECT` gains one LEFT JOIN and one column — used by every caller
  of that shared query string (`get_event`, admin listing, etc.), not only
  the projection; all gain a `venue_street` field in their result dict,
  additive and unused by any caller that does not read it.
- No Redis key format change. No app-facing API response field is removed,
  renamed, or added — the Redis serving projection simply contains fewer
  rows for any FUTURE event whose venue lacks a street address; vibes_bot
  and mobile need no code change.
- Admin console listing (`admin_events_router.list_events`) is unaffected —
  confirmed not to depend on `is_selectable`
  (`260914_musical-events-scope.md`'s own evidence, re-confirmed here) — an
  excluded event stays fully visible to an operator for review.

## Error Handling And Observability

- `load_require_street_address` never raises: missing key, invalid JSON, or
  invalid shape all fall back to `DEFAULT_REQUIRE_STREET_ADDRESS`, logged as
  a warning with a `fallback_reason`, matching every other admin-config
  loader in this module family.
- No new Prometheus metric family: this is a row-local `is_selectable`
  exclusion, the same class of check `venue_id IS NOT NULL`/`superseded_by`
  already are, and those carry no dedicated metric either — the count of
  excluded rows is directly observable as a before/after diff of
  `list_events_for_projection`'s own result size, which
  `RedisProjectionService`'s existing `EVENTS_TOTAL` gauge already reports
  on every projection cycle.

## Test Plan

Feature file: `tests/bdd/persistence/event-street-address-gate.feature`

Scenarios:
- An otherwise-selectable event linked to a venue with a non-blank `street`
  is selected when `event_require_street_address` is `true` (the default).
- An otherwise-selectable event linked to a venue with a blank/missing
  `street` is NOT selected when `event_require_street_address` is `true`.
- The same street-incomplete-venue event IS selected when an operator sets
  `event_require_street_address` to `false` (the rollback path, no deploy).
- Every other existing `is_selectable` scenario (status, `venue_id`
  presence, `superseded_by`, recurring source-freshness, past-grace) is
  unaffected when the new flag is at its default `true` value and the
  venue's `street` is present — a pure regression guard that the new check
  does not change any existing outcome for an address-complete venue.

Pytest unit tests:
- `is_selectable(row, now=..., require_street_address=True)` with
  `venue_street=None`/`""`/whitespace-only: excluded. With a real street:
  included (holding every other field constant).
- `has_street_address` boundary cases: `None`, `""`, `"   "`, a real street
  string.
- `load_require_street_address`: missing key → default; invalid JSON →
  default + `fallback_reason`; non-bool value → default +
  `fallback_reason`.
- `list_events_for_projection`'s SQL against a live/staging Postgres (this
  repo's existing "no local Postgres in CI/dev" parity-pinning convention,
  per `event_projection_selection.py`'s own docstring) confirms the SQL
  condition and the Python predicate agree on a street-complete and a
  street-incomplete fixture row.

Manual or integration checks:
- After deploy, confirm `admin_config:event_require_street_address` reads
  back `true` via `GET /admin/config/event_require_street_address`, and that
  a production count of `list_events_for_projection`'s result size is
  unchanged before/after (matching this plan's own zero-blast-radius
  measurement).

## Acceptance Criteria

- `is_selectable` excludes a row whose linked venue has no street address
  only when `require_street_address` is true, and never otherwise.
- `list_events_for_projection`'s SQL and the Python predicate agree, pinned
  by a parity test.
- Production event count served by the projection is unchanged immediately
  after deploy (measured zero blast radius holds).
- An operator can disable the gate via admin config with no deploy.

## Open Questions

None.
