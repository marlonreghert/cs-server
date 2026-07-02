# Parse BestTime Rejections That Carry venue_info Without venue_id

## Branch
fix/besttime-rejection-venue-info

## Goal
A BestTime create rejection must take the designed rejection path — surface
BestTime's own message and attempt the geo fallback — instead of being
misclassified as an unparseable response. Today a rejection body that includes
a `venue_info` block without a `venue_id` fails envelope validation and returns
502 "BestTime returned an unparseable response".

## Non-goals
- Making unforecastable venues addable (BestTime genuinely has no data for
  them; the geo fallback is the only alternative and already exists).
- Any change to the success path, the analysis tolerance (PR #65), the timeout
  recovery (PR #67), or the vibes_bot panel (it already renders `detail` +
  `besttime_message`).

## Evidence
- Prod 2026-07-02 15:14:47 — add "Mansão da Matuta" (R. do Bonfim 82, Olinda):
  BestTime `POST /forecasts` → **HTTP 404** with a body that parsed fully
  except `failed fields: ['venue_info.venue_id']` → client raised
  `BestTimeInvalidResponseError` → handler returned the bad-response 502.
  BestTime's rejection message never reached the operator; the geo fallback
  never ran; ~1 credit spent (BestTime counts unsuccessful forecasts).
- Model: `NewVenueInfo.venue_id: str` is the ONLY required field
  (`app/models/new_venue.py:29`); every other field is Optional. A rejection's
  partial `venue_info` (no id — nothing was created) therefore fails the whole
  envelope.
- `NewVenueResponse.is_ok()` already guards `venue_info is not None and
  bool(venue_info.venue_id)` — an Optional/None `venue_id` changes no outcome
  logic.
- Client 4xx handling (`besttime_client.add_venue_to_account`): <500 bodies are
  parsed and returned as `NewVenueResponse` precisely so the handler can branch
  on `status`/`message`; the required `venue_id` defeats that design for this
  body shape.
- Handler non-OK path is ready: `_response_ok` false → monthly-cap check →
  `_geo_fallback` (`add_venue_handler.py:180-204`); both geo-fallback outcomes
  already include `besttime_message` (:451, :469).

## Current Behavior
- Rejection with partial `venue_info` → ValidationError → typed
  bad-response error → 502 "unparseable response";
  `result="besttime_bad_response"`; no BestTime message; no geo fallback.

## Desired Behavior
- The same body parses into `NewVenueResponse` (status/message intact,
  `venue_info.venue_id=None`), `is_ok()` is false, and the handler runs its
  existing rejection path: monthly-cap check, then geo fallback, then the
  honest 502/200 outcomes that carry `besttime_message`.
- Truly unparseable envelopes (no usable `status`) keep the bad-response
  classification.

## Implementation Approach
- `app/models/new_venue.py`: `NewVenueInfo.venue_id: Optional[str] = None`.
  No other production change expected — `is_ok()`, the client's non-OK warning
  log, and the handler's non-OK branch already do the right thing with a
  parsed-but-idless response. Verify no caller dereferences
  `venue_info.venue_id` on the non-OK path assuming str.

## Data, Config, And API Impact
None. Error responses for this case change from the generic bad-response 502 to
the richer existing rejection outcomes (which the panel already renders).

## Error Handling And Observability
- This body shape now increments the existing non-OK/rejection metrics instead
  of `besttime_bad_response` / `invalid_response_schema` (those remain for
  genuinely unparseable envelopes).
- The client's existing `add_venue_to_account non-OK: status=… message=…`
  WARNING covers logging; no new logs.

## Test Plan
Feature file: `tests/bdd/api/besttime-rejection-venue-info.feature`

Scenarios:
- A BestTime 4xx rejection whose venue_info has no venue_id is treated as a
  rejection: BestTime's message reaches the error response and the geo
  fallback is attempted (regression of the prod symptom).
- The same rejection with a nearby geo-fallback match completes as
  matched_via_geo_fallback (the rejection path fully works end-to-end).
- A body with no usable status still classifies as the bad-response error.

Pytest unit tests:
- `NewVenueInfo` parses with venue_id absent/None; `is_ok()` false when
  venue_id is None/empty/missing but status is OK; full 404-shape fixture
  (status+message+partial venue_info) parses and routes non-OK.

Manual or integration checks:
- Post-deploy: re-observe an unforecastable-venue add — panel shows BestTime's
  message (or a geo-fallback result), not "unparseable response".

## Acceptance Criteria
- The prod 404 shape (partial venue_info) never raises
  `BestTimeInvalidResponseError`; it takes the rejection path with
  `besttime_message` surfaced.
- Genuinely unparseable envelopes keep the bad-response classification.
- All existing add-venue suites stay green.

## Open Questions
- None.
