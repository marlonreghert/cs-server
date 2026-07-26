# Closed-Venue Detection From Review Evidence

## Branch
feature/closed-venue-detection

## Goal
Stop serving venues that reviewers report as permanently closed. When a venue's
most recent review states the venue has closed and no newer review contradicts
it, the venue must be flagged and excluded from the Redis serving projection,
so vibes_bot and the app never present it as an open, busy place.

## Non-goals
- Deleting or soft-deleting the venue. Closure is evidence-derived and
  reversible (a venue can reopen); it must not touch `lifecycle_status` or
  trigger the soft-delete sweep.
- Changing the busyness pipeline itself. A closed venue is excluded from
  serving; the accuracy of BestTime readings for open venues is separate work.
- Any vibes_bot or mobile change. Exclusion happens upstream in the projection,
  so downstream repos need no contract change.
- Detecting temporary closure (renovation, holidays, "fechado hoje").
- Language coverage beyond PT-BR review text.

## Evidence
- `docs/vibe-mode-evals/260726_role_agitado.md` (vibes_bot) — the evaluation
  that found this. **Burburinho Recifebar** is served at rank #11 flagged
  `cheio` while its newest review (2026-01-12) reads *"SÓ PRA AVISAR PRA QUEM
  NÃO SABE. ESSE BAR FECHOU."* Its four other reviews are 2018–2023.
- `app/models/venue_review.py` — `VenueReview` already carries `text`,
  `rating`, `publish_time`, and `language`. No new upstream fetch is required.
- `app/services/redis_projection_service.py:66` — reviews are already an RDS
  enrichment table (`google_places.reviews`) projected to Redis, so the
  evidence is present in RDS at projection time.
- `app/services/redis_projection_service.py` `rebuild_redis_from_rds()` — the
  projector's serving source is `list_servable_venue_ids()`, a single gate.
- `migrations/versions/0009_eligibility_serving_view.py` — `serving.eligible_venue`
  is a SQL view: servable iff `lifecycle_status='active'` AND not
  high-confidence ineligible under `admin.eligibility_rule`. Extending this view
  is the correct exclusion point; it is dynamic in both directions with no
  lifecycle change and no soft-delete.
- `app/services/venue_eligibility.py` — `EligibilityResult` already models
  `confidence` (`high`/`low`), the same shape closure detection needs.
- `tests/test_eligibility_serving_view_parity.py` — pins the SQL view against
  the Python predicate; any view change must keep this passing.

## Current Behavior
A venue whose reviews report it closed remains `lifecycle_status='active'`,
stays in `serving.eligible_venue`, is projected to Redis with its live busyness,
and is served normally. Nothing in the pipeline reads review *text*; reviews are
stored and projected verbatim for display only.

## Desired Behavior
1. A closure-detection pass must evaluate each venue's stored reviews and derive
   a closure signal from: a closure phrase in the most recent review, and the
   absence of any newer review that contradicts it.
2. A venue with a high-confidence closure signal must be excluded from
   `serving.eligible_venue` and therefore removed from the Redis projection on
   the next cycle.
3. The signal must be reversible: if a newer non-closure review appears, the
   venue must return to serving on a later cycle with no manual intervention.
4. The venue's RDS row, enrichment, and lifecycle must be unchanged — only its
   servability changes.
5. An operator must be able to see which venues are excluded as closed and why.

## Implementation Approach
**Detection (`app/services/closure_detection_service.py`, new).** A pure
evaluator over a `VenueReviews` payload returning a result with `closed: bool`,
`reason`, `confidence`, and the matched evidence (review date + matched phrase).
Rules, in order:
- Sort reviews by `publish_time` descending; ignore entries with no
  `publish_time` (they cannot be ordered and must not decide closure).
- The **most recent** review must match a closure phrase for closure to be
  considered. A closure phrase deep in history is stale gossip, not evidence.
- Phrases are configured, not hardcoded in the evaluator: normalized,
  accent-insensitive, case-insensitive PT-BR markers ("fechou", "fechado
  permanentemente", "encerrou as atividades", "nao existe mais",
  "nao funciona mais"). Negations and temporary-closure qualifiers ("fechado
  hoje", "fechado para reforma", "vai fechar") must not match.
- Confidence is `high` only when the matching review is also the newest by a
  margin — i.e. no non-matching review exists within the configured recency
  window after it. Otherwise `low`.
- Only `high` confidence excludes from serving. `low` is recorded for operator
  review and does not change serving. This mirrors the existing
  `EligibilityResult.confidence` contract.

**Persistence (migration `0019_venue_closure_signal`).** Add
`admin.venue_closure_signal` (venue_id PK, closed bool, reason text, confidence
text, evidence_publish_time timestamptz, matched_phrase text, detected_at,
updated_at). A table rather than a venue column keeps closure additive and
independently truncatable, and matches how other derived signals are stored.

**Serving (same migration, view replace).** Extend `serving.eligible_venue` with
`AND NOT EXISTS (SELECT 1 FROM admin.venue_closure_signal c WHERE
c.venue_id = v.venue_id AND c.closed AND c.confidence = 'high')`. Update
`tests/test_eligibility_serving_view_parity.py` so the Python predicate and the
view stay pinned together.

**Scheduling.** Run detection as an APScheduler job alongside the existing
enrichment jobs, on its own interval, reading reviews from RDS in bulk (same
`get_enrichment_bulk` pattern the projector uses — one query, not one per
venue). The job writes/updates `admin.venue_closure_signal` and is idempotent;
re-running must not change a venue whose reviews have not changed.

**Config.** Phrase lists, the recency window, and the job interval belong in
admin config with hardcoded defaults, following the `admin_config_service`
pattern already used for eligibility — so a false positive can be corrected
without a deploy.

## Data, Config, And API Impact
- **Migration:** `0019_venue_closure_signal` — new `admin.venue_closure_signal`
  table + `CREATE OR REPLACE VIEW serving.eligible_venue`. EXPAND-only and
  additive; the down path drops the table and restores the previous view body.
- **Config:** new admin-config key for closure phrases, recency window, and
  enable flag, with defaults in code. Detection must be **disabled by default**
  and enabled deliberately after a dry-run review of what it would exclude.
- **API:** new read-only `GET /admin/venues/closed` listing flagged venues with
  reason, confidence, evidence date, and matched phrase. No change to
  `GET /v1/venues/nearby` — its result set simply contains fewer venues.
- **Redis:** no key-format change. A newly-excluded venue is removed by the
  projector's existing reconcile/removal pass.

## Error Handling And Observability
- Detection is best-effort per venue and isolated: a malformed review payload,
  an unparseable `publish_time`, or a Pydantic failure must be logged with the
  venue id and must not abort the job or affect other venues.
- A detection-job failure must never blanket-exclude venues. On failure the
  table is left as-is and the projection continues with the last known state —
  the same fail-safe posture `rebuild_redis_from_rds` uses for a failed serving
  view read.
- Metrics (`app/metrics.py`): `venues_closed_flagged` (gauge, by confidence),
  `closure_detection_runs_total` (counter, by outcome),
  `closure_detection_duration_seconds`, and `venues_excluded_closed` (gauge) on
  the projection side so exclusions are visible next to
  `VENUES_GEO_EXCLUDED`.
- Logs must record venue id, matched phrase, and evidence review date on every
  state change (not on every evaluation), so an operator can audit a flag.

## Test Plan
Feature file: `tests/bdd/persistence/closed-venue-detection.feature`

Scenarios:
- A venue whose newest review reports permanent closure is flagged closed with
  high confidence and is absent from the serving projection.
- A venue whose closure phrase appears only in an old review, with newer
  ordinary reviews after it, remains servable.
- A venue with a temporary-closure phrase ("fechado para reforma") remains
  servable.
- A venue flagged closed that receives a newer ordinary review returns to the
  serving projection on the next cycle.
- A low-confidence closure signal is recorded but does not change serving.
- A venue with no reviews, or reviews lacking `publish_time`, is never flagged.
- A venue flagged closed keeps its `lifecycle_status='active'` and its RDS row
  and enrichment intact.
- With detection disabled by the config flag, no venue is flagged or excluded.
- A malformed review payload for one venue does not prevent other venues from
  being evaluated, and the run summary reports the error naming that venue.

Pytest unit tests:
- `tests/test_closure_detection_service.py` — phrase matching incl. accents and
  case; negation and temporary-closure rejection; newest-review ordering;
  missing/unparseable `publish_time`; confidence assignment; idempotency.
- `tests/test_eligibility_serving_view_parity.py` — extend so the view's closure
  predicate stays pinned to the Python predicate.

Manual or integration checks:
- Dry run against production data with the flag off: list what *would* be
  excluded and hand-verify each venue before enabling. Burburinho Recifebar
  (`ven_45776644447035466e70555263777159513630416d39614a496843`) must appear.

## Acceptance Criteria
- A venue whose newest review reports permanent closure is absent from the
  Redis serving projection after one projection cycle.
- Burburinho Recifebar is flagged closed in a dry run over production data.
- A flagged venue's `lifecycle_status` remains `active` and its RDS row and
  enrichment rows are unchanged.
- A flagged venue returns to serving on a later cycle once a newer ordinary
  review exists, with no manual intervention.
- Detection is off by default; enabling it is a deliberate config change.
- `GET /admin/venues/closed` lists each flagged venue with reason, confidence,
  evidence date, and matched phrase.
- `tests/test_eligibility_serving_view_parity.py` passes with the extended view.
- All new/changed BDD scenarios and pytest tests pass.

## Open Questions
- None. The false-positive risk is handled by shipping disabled-by-default with
  a mandatory dry-run verification before enabling (see Manual checks).
