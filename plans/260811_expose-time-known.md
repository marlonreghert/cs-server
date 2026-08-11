# Expose `time_known`

## Branch
feature/expose-time-known

## Goal
Tell an API consumer whether an item's start time was actually read from the
post, or whether only a date was.

## Non-goals
- **Changing date resolution.** `time_known` is already computed and already
  stored; this exposes it.
- **Formatting.** How a console renders an unknown time is the console's call.

## Evidence
Every item in production reads `03:00:00Z` — midnight in Recife — because the
resolver found a date and no clock time. `EventOut` does not carry the flag that
says so: `time_known` lives inside the `raw_extraction` JSONB blob, which
`_EVENT_SELECT` fetches but `EventOut(**row)` silently discards, since Pydantic
drops undeclared keys.

The console therefore cannot tell "starts at midnight" from "we don't know when
it starts". Rendering the current data as `00:00` would assert a midnight start
for essentially every item, and a genuine midnight show is ordinary in
nightlife, so the two cases can never be told apart by inspecting the time.

An earlier plan asserted this field was already served. It was not — found when
`260811_event-columns-and-dataset-pickers.md` §B stopped rather than infer
"unknown" from the clock reading midnight.

## Current Behavior
`time_known` is computed, persisted inside `raw_extraction`, and dropped at the
API boundary.

## Desired Behavior
1. `EventOut` carries whether the start time is known.
2. An item predating the flag reports it as unknown rather than guessing.

## Implementation Approach
Add `time_known: bool` to `EventOut`, read from the same place the resolver
wrote it.

**Default to `False`, not `True`.** An item with no recorded flag is one whose
time we cannot vouch for, and the honest rendering of "we don't know" is safer
than asserting a start that was never read. This is the same fail-toward-honest
direction the date work already took.

Prefer a real column over reaching into the JSONB at serve time if the resolver
already has somewhere natural to put it — a field every read has to dig out of a
blob is one the next consumer will also drop. If adding a column, back-fill from
`raw_extraction` where present and leave the rest false.

## Data, Config, And API Impact
- **Migration** only if a column is added; state which was chosen and why.
- **API:** `EventOut` gains `time_known`. Additive; no released mobile build
  reads these endpoints — confirm rather than assume.
- **Rollback:** revert.

## Test Plan
Feature file: `tests/bdd/enrichment/expose-time-known.feature`

Scenarios:
- Report a known start time as known.
- Report a date-only item's time as unknown.
- Report an item predating the flag as unknown.
- Report a genuine midnight start as known.

Pytest unit tests:
- Serialisation carries the flag for both values.
- A missing flag serialises as `False`, never `True`.
- The resolver's existing outputs map to the flag as expected, including the
  recurrence and range paths added recently.

## Acceptance Criteria
- `EventOut.time_known` reflects whether a time was read.
- A genuine midnight start is distinguishable from an unknown one.
- Absent data reports unknown.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
