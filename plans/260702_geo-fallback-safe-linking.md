# Geo-Fallback Safe Linking: Fold+Best-Match, Generic-Name Guard, Undoable Links

## Branch
feature/geo-fallback-safe-linking

## Goal
Make the add-venue geo fallback safe against linking the wrong place, and make
every link reversible:

1. **Best-match, not first-match:** `_find_name_match` reuses the accent-folding
   `_fold_text` normalization and ranks candidates (exact folded equality
   first, then address-token overlap) instead of returning the first
   substring hit.
2. **Generic-name guard:** containment (substring) matches are allowed only
   when the shorter folded name has at least 5 characters; shorter names match
   on exact folded equality only ("bar" can no longer link to "barcelona bar").
3. **Undoable links:** the geo-fallback response says whether the link
   created a new catalog row (`newly_linked`), and a new admin endpoint undoes
   a fresh link — reversibly and without poisoning future re-adds.

## Non-goals
- The admin-panel confirm/undo UI — planned in vibes_bot
  (`plans/260702_admin-geo-link-confirm.md`); this plan mints the contract.
- Changing when the geo fallback runs (still only after a BestTime rejection),
  its 50m radius cap, or the create/timeout/rejection paths shipped in PRs
  #65/#67/#68.
- Undo for venues that pre-existed the link (`newly_linked=false` — there is
  nothing to undo) or for old venues (see the 24h guard).

## Evidence
- Matcher today: `_find_name_match` (`app/handlers/add_venue_handler.py:724`)
  lowercases only (no accent folding), matches `==`/`in` both ways, returns the
  FIRST hit. Risks: "bar" links to "barcelona bar" 30m away; two same-brand
  units within 50m resolve arbitrarily; "Laça" never matches BestTime's
  normalized "Laca" (safe-direction miss, still a miss).
- `_fold_text` exists (`add_venue_handler.py:664`, PR #67): accent-fold,
  casefold, strip punctuation, collapse whitespace. The PR #67 reconcile also
  already implements address-token-overlap disambiguation — same technique.
- Link mechanics (`_geo_fallback`, :455-513): computes `was_new`, upserts only
  when new, `record_new_venue_from_discovery()` (increments the month counter
  via `increment_month`), `mark_touched`, address-cache save; the 200 body does
  NOT expose `was_new`.
- Undo mechanics available: `release`-style decrement exists
  (`VenueBudgetDao.decrement_month`, clamped); `RdsVenueStore.soft_delete_venue`
  exists; there is NO hard delete for `venues.venue` (grep) — and hard delete
  would orphan enrichment rows. Plain soft-delete would POISON future re-adds:
  `_preserve_deprecation` (`rds_venue_store.py:82-99`) keeps any deprecated
  venue deprecated on re-upsert, keyed only on `lifecycle_status`, ignoring
  `deprecated_source`.
- `venues.venue.created_at` exists (schema check 2026-07-01) → recency guard.
- Projector drops deprecated venues from serving on its next 2-min cycle
  (serving view excludes non-active) — no direct Redis surgery needed.

## Current Behavior
- First substring hit within 50m is silently linked; response body does not
  say whether a new row was created; a wrong link can only be cleaned up by
  hand (and a soft-delete cleanup would permanently block that venue_id from
  future adds).

## Desired Behavior
- **Matching:** candidates are folded with `_fold_text`; a candidate matches
  when folded names are equal, or when one folded name contains the other AND
  the shorter one is ≥5 characters. Among matches, rank exact equality above
  containment, then higher address-token overlap (fold both addresses, split
  to token sets, intersect), then BestTime order. Top candidate wins.
- **Response:** the `matched_via_geo_fallback` 200 body gains
  `newly_linked: true|false` (true only when the link upserted a new row).
- **Undo:** `POST /admin/venues/geo-link/undo` with `{venue_id}`:
  - venue exists, is active, and `created_at` is within 24h → soft-delete with
    `reason="geo_link_undone"`, `source="admin_geo_link_undo"`, decrement the
    month counter by 1 (the discovery increment), keep the unique-touch ledger
    entry (the BestTime interaction really happened), drop the address-hash
    cache entry for the venue when present. 200 `{status:"undone", venue_id}`.
  - venue already deprecated by a prior undo → 200 `{status:"already_undone"}`
    (idempotent; no second counter decrement).
  - venue missing → 404; venue older than 24h or not undo-eligible → 409 with
    an explanatory detail (protects against misuse on long-standing venues).
- **Re-add works after undo:** `_preserve_deprecation` gains one exemption —
  when the stored row's `deprecated_source == "admin_geo_link_undo"`, an active
  re-upsert IS allowed to reactivate (clearing the deprecation fields). All
  other deprecation sources keep today's resurrect-block.

## Implementation Approach
- **Matcher (`add_venue_handler.py`):** rewrite `_find_name_match` per the
  ranking rules above, reusing `_fold_text`; module-level constant
  `MIN_CONTAINMENT_MATCH_LEN = 5`.
- **Body flag:** thread `was_new` into the 200 body as `newly_linked`.
- **Undo endpoint:** router `POST /admin/venues/geo-link/undo` →
  `add_venue_handler.undo_geo_link(venue_id)` (or a small dedicated handler),
  using `venue_dao`/rds store reads, `soft_delete_venue`, and a budget-service
  method (`release_discovery_slot()` → `decrement_month(1)` — mirror of
  `release_manual_slot`). Metric: `ADD_VENUE_BY_ADDRESS_TOTAL` result label
  `geo_link_undone` (and `geo_link_undo_rejected` for 409s) or a small
  dedicated counter — keep labels consistent with existing style.
- **`_preserve_deprecation` exemption (`rds_venue_store.py`):** if
  `row["deprecated_source"] == "admin_geo_link_undo"` and the incoming venue is
  active, skip preservation (allow reactivation). Mirror the same exemption in
  the BDD fake store to keep parity.

## Data, Config, And API Impact
- API: `newly_linked` added to the geo-fallback 200 body (additive);
  new `POST /admin/venues/geo-link/undo` endpoint (contract for vibes_bot).
- No RDS schema change, no migration, no config. Redis: only the existing
  address-cache key deletion on undo.

## Error Handling And Observability
- Undo path logs venue_id + outcome at INFO (undone) / WARNING (rejected).
- Idempotency + 404/409 as above; counter decrement happens exactly once per
  undone link (guarded by the already-deprecated check).
- Matcher changes carry no new runtime failure modes (pure function); the
  ranking is deterministic.

## Test Plan
Feature file: `tests/bdd/api/geo-fallback-safe-linking.feature`

Scenarios:
- An accent-folded name matches ("Laça Burguer" links to BestTime's "Laca
  Burguer") — the false-negative gap closes.
- With two candidates in radius, the one with higher address-token overlap is
  linked, not the first-listed one.
- A short generic name ("Bar") does not containment-match a longer name
  ("Barcelona Bar"); an exact short name still matches.
- The geo-fallback success body says whether the venue was newly linked.
- Undoing a fresh link deprecates the venue, returns the month-counter slot,
  and the venue leaves the eligible serving set.
- Undo is idempotent (second call reports already undone, no double decrement).
- Undoing a venue older than 24h is rejected with an explanatory error.
- After an undo, re-adding the same venue reactivates it (no deprecation
  poisoning).

Pytest unit tests:
- `_find_name_match` ranking table: exact-vs-containment precedence,
  overlap tie-break, MIN_CONTAINMENT_MATCH_LEN boundary (4/5 chars), accent
  folding, empty candidate names.
- Undo handler: eligibility guards (missing, old, already-undone), counter
  decrement exactly-once, address-cache cleanup.
- `_preserve_deprecation` exemption: `admin_geo_link_undo` reactivates; every
  other source still preserved (parity with the fake store).

Manual or integration checks:
- Panel flow after the vibes_bot half ships: link → confirm banner → undo →
  venue disappears from inventory within a projector cycle → re-add works.

## Acceptance Criteria
- Wrong-place links require both a ≥5-char folded-name containment (or exact
  match) AND winning the address-overlap ranking — "first substring hit" is
  gone.
- Every fresh link is undoable for 24h, exactly once, returning the budget
  slot, without blocking future re-adds of that venue.
- Existing add/rejection/timeout/recovery suites stay green.

## Open Questions
- None.
