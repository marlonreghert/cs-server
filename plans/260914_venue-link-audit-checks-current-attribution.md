# Venue-Link Audit Must Check An Event's Current Attribution, Not Just Its Handle's History

## Branch
fix/venue-link-audit-checks-current-attribution

## Goal
`compute_venue_link_audit` (`plans/260913_venue-handle-link-audit.md`, merged
and deployed as PR #231) checks EVERY live event a handle ever posted against
EVERY venue currently mapped to that handle — including events whose OWN
`venue_id` has ALREADY been correctly repointed elsewhere by an out-of-band
mechanism (the attribution-dispute reattribute action, a historical backfill,
or a future correction). Running the shipped tool against production for the
first time (2026-09-13/14) surfaced exactly this: `beerdock_recife` was
flagged for "BeerDock Boa Viagem" at 17/46 non-corroborating, but all 16 of
the sampled non-corroborating events already carry `venue_id='Beerdock Casa
Forte'`, `linked_by='neighbourhood_match'` — correctly reattributed hours
earlier by `plans/260912_events-venue-night-duplication.md`'s own backfill.
The audit re-flagged an already-fixed case as if it were new.

Fix: a mapped venue V is checked only against events whose CURRENT
`venue_id` is either unset (`None` — never yet resolved, exactly the
`editaisculturape`/`real.botequim` shape this tool was built to catch) or V
itself. An event already, confidently resolved to a DIFFERENT venue is not
evidence about V's mapping at all — it is a closed case.

## Non-goals
- Re-investigating `entreamigosobode` (also flagged, both mapped siblings).
  Checked fresh (see Evidence): unlike `beerdock_recife`, 13 of its 18 live
  events are `venue_id=None` (never resolved), not already-attributed
  elsewhere — this fix's filter does not remove them from either sibling's
  denominator, and does not change either sibling's flagged verdict. Whether
  `entreamigosobode`'s flag is a real gap or a tool limitation (its posts
  may legitimately not restate an identifying place name) is a genuinely
  separate question this plan does not resolve.
- Triaging the other 13 un-triaged flagged pairs from the 2026-09-13
  production run. Out of scope; this plan fixes one confirmed mechanism,
  not every flagged pair.
- Any change to `compute_dedup_backlog`, `resolve_event_venue`,
  `evaluate_attribution_dispute`, or the `venue_link_audit_enabled` default
  (`False`, unchanged).
- Widening what counts as "corroborating" (`venue_corroborates_location_text`
  itself is untouched) — this plan changes which EVENTS are considered, not
  how any one event's text is judged.

## Evidence

### The confirmed false positive (re-checked fresh, 2026-09-14)
Running `scripts/measure_venue_link_audit.py --production-only` (read-only)
against live prod flagged 15 handles / 18 (handle, venue) pairs — far more
than the 3-case seed corpus this tool's own plan validated against. Spot
check on `beerdock_recife` → `BeerDock Boa Viagem` (flagged 17/46):

```
evt_01KZVGAQ07K8MAB3S9ZB2EG87T | 'Sunset com Rodrigo'  | location_text='CASA FORTE'
evt_01M18M4P421XNJ017HZKF8VF0V | 'Shandy Gama...'      | location_text='CASA FORTE'
evt_01M27GY1XX1D6R5X8EXH4FP9QZ | 'SMILE ESPECIAL...'   | location_text='CASA FORTE'
```
All three, re-read live: `venue_id='ven_637a...Jf496843'` (Beerdock Casa
Forte), `linked_by='neighbourhood_match'` — i.e. correctly resolved, not
force-assigned to Boa Viagem at all. 16 of the 17 sampled non-corroborating
events share this exact shape (the 17th, "Oktoberfest BeerDock" /
"Mirante do Paço, Recife", is a genuinely separate, unchecked case — a real
location_text that doesn't corroborate Boa Viagem either, worth an
operator's own look once this fix ships, not folded into this plan).

### Why the fix is exactly "filter by current venue_id," not a broader redesign
`compute_venue_link_audit`'s inner loop (`app/services/venue_link_audit.py`)
iterates `for row in events:` — ALL of `events_by_handle[handle]` — for
EVERY mapped venue, with no reference to `row.get("venue_id")` at all.
`collect_venue_link_audit` already fetches full event rows via
`venue_dao.list_events_by_handle(handle)`, which already carries `venue_id`
on every row — no new DAO read is needed, only reading a field already in
hand.

### Why `None` must stay included, not just "== V"
A naive fix ("only check events where `venue_id == V.venue_id`") was
considered and rejected: `real.botequim`'s 18 events (the tool's own
existing, passing test case) ALL carry `venue_id=None` — neither of its two
mapped venues (`Bar Real Botequim`, `Real Bar e Lanches`) ever clears
`DEFAULT_CONFIDENCE_FLOOR` for either candidate, so both stay unresolved
(re-verified live, 2026-09-14: unchanged from the seed-corpus capture).
Excluding `None` rows would silently zero out the checkable count for BOTH
of `real.botequim`'s mapped venues — flagging neither, a regression on the
one case already proven correct. `editaisculturape`'s 14 events all carry
`venue_id` = its one mapped venue (the force-assign default, no ladder
call) — included under either "== V" or "is None", unaffected either way.
`venue_id in (None, V.venue_id)` is the narrowest filter that fixes
`beerdock_recife` without touching either existing passing case.

### `entreamigosobode` re-checked fresh — this fix does not change its verdict
18 live events: 13 `venue_id=None`, 5 `venue_id='Entre Amigos O Bode'`
(one sibling), 0 `venue_id='Entre Amigos O Bode Espinheiro'` (the other).
Under the fix, "Entre Amigos O Bode"'s denominator is unchanged (18 — all
13 `None` + its own 5 were already included). "Entre Amigos O Bode
Espinheiro"'s denominator drops from 18 to 13 (the 5 attributed to its
sibling are correctly excluded) — but the 13 `None` events, whatever they
non-corroborate today, still count. Whether either sibling's flag survives
this fix is measured, not assumed (see Manual checks) — this plan makes no
claim about the outcome, only that the mechanism differs from
`beerdock_recife`'s.

## Current Behavior
`compute_venue_link_audit` checks every live event a handle ever posted
against every venue currently mapped to that handle, with no reference to
the event's own current `venue_id` — an event already correctly resolved
to a DIFFERENT venue by an unrelated mechanism (the attribution-dispute
ladder, a historical backfill) counts as "non-corroborating" evidence
against whichever venue the audit happens to be checking.

## Desired Behavior
A mapped venue V is checked only against events whose current `venue_id` is
`None` (never resolved — the exact case this tool exists to catch) or `V`
itself. An event already confidently resolved to some OTHER venue never
counts toward or against V's own checkable/non-corroborating totals.

## Implementation Approach
`app/services/venue_link_audit.py::compute_venue_link_audit` — inside the
`for venue in mapped:` loop, before the existing `for row in events:` loop,
filter once: `checkable_rows = [r for r in events if r.get("venue_id") in (None, venue["venue_id"])]`,
then iterate `checkable_rows` instead of `events`. Pure, no new DAO call
(`venue_id` is already on every row `collect_venue_link_audit` fetches).
Update the function's own docstring to state this filter explicitly, so a
future reader does not reintroduce the bug by "simplifying" back to the
handle's full event list.

No change to `venue_corroborates_location_text`, `collect_venue_link_audit`'s
DAO reads, the admin-config default, or the response shape.

## Data, Config, And API Impact
None beyond the one pure-function change above. No new DAO method, no
migration, no new admin-config key, no API shape change.

## Error Handling And Observability
No new runtime path. The existing per-flagged-pair INFO log line
(`collect_venue_link_audit`) is unaffected in shape; its counts will simply
reflect the corrected, narrower denominators.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-handle-link-audit.feature`
(extend the existing file — this is the same feature, corrected)

Scenarios (new):
- A handle whose events have already been correctly reattributed to a
  DIFFERENT venue by an unrelated mechanism is never flagged for its
  original mapped venue on the strength of those already-resolved events
  alone — the `beerdock_recife` shape, pinned with real production
  strings/IDs.
- An event with `venue_id=None` (never resolved) still counts toward a
  mapped venue's checkable/non-corroborating totals exactly as before —
  regression guard so the fix does not over-correct into excluding
  everything.
- The existing `real.botequim` (double-mapped, both venues `None`) and
  `editaisculturape` (force-assigned, single mapped venue) scenarios
  continue to pass unmodified — proof this fix does not regress the
  already-shipped, already-validated cases.

Pytest unit tests (`tests/test_venue_link_audit.py`, extend):
- `compute_venue_link_audit` excludes an event whose `venue_id` is a
  different, specific venue from a mapped venue's checkable count.
- `compute_venue_link_audit` still includes an event whose `venue_id` is
  `None` in a mapped venue's checkable count.
- `compute_venue_link_audit` still includes an event whose `venue_id`
  already equals the mapped venue being checked.
- The real `beerdock_recife`/`Boa Viagem` shape (add as a 4th corpus case
  in `tests/fixtures/venue_link_audit/corpus.json`, real captured strings,
  reviewed for PII before commit — same convention as the existing 3):
  expected NOT flagged once already-reattributed events are excluded.
- The 3 existing corpus cases still pass with their existing expected
  verdicts, unmodified.

Manual or integration checks:
- Run `scripts/measure_venue_link_audit.py --production-only` against real
  production data before and after the fix, read-only via the established
  AWS SSM pattern. Confirm: `beerdock_recife` drops out of the flagged list
  (or its non-corroborating count drops meaningfully — record the real
  number, do not assume zero, since the 17th event, "Oktoberfest BeerDock",
  is a genuine unchecked case that may still count). Confirm `casabacurau`,
  `editaisculturape` remain flagged (already-independently-confirmed real
  cases, unaffected by this fix's mechanism). Record whether
  `entreamigosobode`'s flag changes, without assuming an answer — this
  plan's Evidence section explicitly does not predict one.

## Acceptance Criteria
- `compute_venue_link_audit` never counts an event toward a mapped venue's
  checkable/non-corroborating totals when that event's own `venue_id` is a
  different, specific venue.
- The `real.botequim` and `editaisculturape` corpus cases still pass with
  their original expected verdicts, unmodified — proof of no regression.
- A new `beerdock_recife` corpus case passes with an expected verdict of
  NOT flagged for "BeerDock Boa Viagem."
- `scripts/measure_venue_link_audit.py --production-only`, run against real
  production data, shows `beerdock_recife` no longer flagged (or its
  non-corroborating count measurably reduced, with the reduction explained)
  — recorded in this plan or a linked follow-up note, not merely asserted.
- `make test-unit`, `make test-bdd` pass; no `@wip` tag remains.
- No change to `venue_link_audit_enabled`'s default (`False`) or to any
  other existing admin-config default.

## Open Questions
None. `entreamigosobode`'s status is explicitly named as unresolved in
Evidence and Non-goals — not an open question blocking this plan, since
this plan makes no claim about it either way and ships regardless of what
the Manual check on it finds.
