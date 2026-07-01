# Parse The Real BestTime POST /forecasts Response (Add-Venue Unblocker)

## Branch
fix/besttime-add-venue-response-parse

## Goal
A successful BestTime venue create must persist the venue. Today it cannot:
`NewVenueResponse` rejects BestTime's real success payload, the handler
misclassifies the `ValidationError` as a transport failure, returns
`502 "BestTime is unavailable: ValidationError"`, releases the quota slot, and
drops a venue that BestTime **created and charged for**. Every manual add fails
this way even when BestTime is healthy — proven live in prod on 2026-07-01
22:35:50 with "Laça Burguer Boa Viagem" (created on BestTime as
`ven_77374d31…`, absent from our store).

## Non-goals
- Recovering the already-created Laça Burguer venue into our store (operational
  action, tracked separately — targeted upsert / sync / re-add after this fix).
- Any change to the BestTime request side (verified correct against the docs:
  `POST /api/v1/forecasts`, query params `api_key_private`/`venue_name`/
  `venue_address`).
- Any change to `GET /forecasts/week/raw2` parsing (`WeekRawResponse`) — that
  endpoint's shape is different and works.
- Retry logic or BestTime-outage handling (502/timeout classification stays).

## Evidence
- Prod log 2026-07-01 22:35:50 (first-ever successful create response):
  `ValidationError: 7 validation errors for NewVenueResponse — analysis.N.day_int
  Field required`, with input per entry shaped
  `{'day_info': {'day_int': …, …}, …, [… hourly ints …]}` — i.e. the real
  `POST /forecasts` analysis nests `day_int` (and day metadata) inside
  `day_info`, with the hourly list alongside; our model requires top-level
  `day_int`.
- Model: `app/models/new_venue.py:42` — `analysis: list[WeekRawDay]`;
  `WeekRawDay` (`app/models/week_raw.py:7`) requires top-level `day_raw` +
  `day_int` (`day_info` optional). One malformed entry fails the WHOLE envelope
  (`NewVenueResponse.model_validate(body)` at
  `app/api/besttime_client.py` `add_venue_to_account`), even though
  `venue_info` — the part that matters — parsed fine.
- Handler: `app/handlers/add_venue_handler.py:134-147` — the bare `except` around
  `add_venue_to_account` labels ANY exception (including our own parse bug) as
  `BestTime transport error` → 502 `BestTime is unavailable: {type}` →
  `result="besttime_error"` metric; slot released; nothing persisted.
- Analysis is already best-effort downstream: the handler caches week_raw days
  in a per-day try/except (`add_venue_handler.py:187-195`), and the model
  docstring says "`analysis` may be partial or empty on fresh creates".
- Docs (documentation.besttime.app, New Foot Traffic Forecast): success is
  HTTP 200 with `status`, `venue_info`, `analysis` (one object per weekday);
  `venue_id` is deterministic on name+address geocode (re-adds cannot
  duplicate).

## Current Behavior
- BestTime returns HTTP 200, `status="OK"`, full `venue_info`, and a 7-day
  `analysis` in its real (day_info-nested) shape.
- `NewVenueResponse.model_validate` raises; the client re-raises; the handler
  reports 502 "BestTime is unavailable"; the quota slot is released; the venue
  exists on BestTime but never reaches RDS/serving; the operator sees a
  misleading outage error.

## Desired Behavior
- The client parses the real response: `status` + `message` + `venue_info`
  decide the outcome; `analysis` is parsed tolerantly — entries in the real
  nested shape are accepted (day_int lifted from `day_info`, hourly list
  captured), and any entry that still cannot be parsed is dropped with a
  WARNING log and a metric, never failing the envelope.
- A response whose envelope truly cannot be parsed (no usable `status` /
  `venue_info`) is surfaced as its own legible failure: a distinct error
  classification (e.g. `invalid_response_schema` metric label and a 502 body
  that says the response was unparseable — NOT "BestTime is unavailable").
- Handler behavior on success is unchanged downstream: persist, mark ledger,
  inline Google enrichment, best-effort week_raw caching of whatever analysis
  days parsed, inline live forecast.
- `GET /forecasts/week/raw2` parsing is untouched.

## Implementation Approach
**1. Model (`app/models/new_venue.py`, optionally `week_raw.py`).**
- Give `NewVenueResponse.analysis` a create-endpoint day model (or a validator on
  the existing one) that accepts the real shape: top-level `day_int`/`day_raw`
  when present (current tests' shape) OR nested `day_info.day_int` + sibling
  hourly list (real shape), normalizing to the existing `WeekRawDay` so
  `set_week_raw_forecast` keeps working unchanged.
- Make `analysis` parsing per-entry tolerant: collect good entries, drop bad
  ones (WARNING + metric), never raise for analysis alone. `is_ok()` stays
  `status == "OK" and venue_info.venue_id`.

**2. Client (`app/api/besttime_client.py` `add_venue_to_account`).**
- Split parsing: validate the envelope minimally (status/message/venue_info);
  on envelope-parse failure raise/return a distinct, typed outcome and increment
  `BESTTIME_API_ERRORS_TOTAL{error_type="invalid_response_schema"}` instead of
  letting a Pydantic error masquerade as a transport error.

**3. Handler (`app/handlers/add_venue_handler.py`).**
- Narrow the `except` around the BestTime call: transport errors (httpx) keep
  the current 502 "BestTime is unavailable"; a response-schema failure returns
  502 with an honest detail (e.g. "BestTime returned an unparseable response")
  and its own metric label (`result="besttime_bad_response"`), so operators can
  tell our parse bug from their outage. Quota slot released in both cases.

## Data, Config, And API Impact
- None to RDS/Redis/config. The add endpoint's success/error contract keeps its
  status codes; only the misleading 502 detail string gains an honest variant.
- Metrics: new `error_type="invalid_response_schema"` label value on
  `BESTTIME_API_ERRORS_TOTAL`; new `result="besttime_bad_response"` label value
  on `ADD_VENUE_BY_ADDRESS_TOTAL`; optional counter/label for dropped analysis
  entries.

## Error Handling And Observability
- Per-entry analysis drops: WARNING with venue_id + entry index (no full payload
  dump per repo logging policy) + metric.
- Envelope-parse failure: ERROR log naming the missing pieces, distinct metric
  labels (above), honest 502 detail.
- No behavior change for genuine transport errors/5xx/timeouts.

## Test Plan
Feature file: `tests/bdd/api/besttime-add-response-parse.feature`

Scenarios:
- Adding a venue when BestTime returns its real success shape (analysis entries
  with day_int nested under day_info) persists the venue, marks the ledger,
  enriches, and returns 201 — the regression that motivated this fix.
- Analysis entries that parse are cached as week_raw days; an unparseable entry
  is dropped without failing the add.
- A response with a valid envelope and completely unparseable analysis still
  persists the venue (analysis is optional).
- A truly unparseable envelope returns the distinct bad-response 502 (not
  "BestTime is unavailable") and releases the quota slot.
- A genuine transport error still returns the existing unavailable 502.

Pytest unit tests:
- Model: real-shape fixture (verbatim from the prod log's structure) parses;
  legacy top-level shape still parses; mixed good/bad entries → good kept, bad
  dropped; `is_ok()` unaffected by analysis.
- Client: envelope-failure path increments `invalid_response_schema` and does
  not raise a bare ValidationError as transport.
- Handler: classification split (httpx error vs schema error → different
  metric labels + details).

Manual or integration checks:
- After deploy: re-add or sync a real venue and confirm 201 + persistence
  (Laça Burguer recovery doubles as the prod verification).

## Acceptance Criteria
- A BestTime success response in the real prod shape results in a persisted,
  enriched, servable venue and a 201 — no ValidationError anywhere in the path.
- Partial/malformed analysis never fails an add; parsed days are cached.
- Parse failures and transport failures are distinguishable in logs, metrics,
  and the 502 detail.
- `week/raw2` behavior and all existing add-venue scenarios stay green.

## Open Questions
- None.
