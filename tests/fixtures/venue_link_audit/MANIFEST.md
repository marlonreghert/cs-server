# Provenance manifest — `venue_link_audit/corpus.json`

See `plans/260913_venue-handle-link-audit.md` Evidence section. Matches the
provenance-manifest convention `tests/fixtures/dedup_agentic_discovery/
MANIFEST.md` already established in this repo: per-item sourcing, reviewed
for PII before commit, so a future reader never has to guess which field is
real data and which is a labeled reconstruction.

None of this manifest's production evidence was obtained by writing
anything: every number and `location_text` value below came from a
read-only Python scan shipped to the `vibes_bot-cs-server-1` container on
EC2 instance `i-0893fb6d283243480` via AWS SSM `send-command`, run on
2026-09-13 against `origin/main` at `8df2823` — not this branch's own new
code, which did not exist on that host at scan time.

## Per-case provenance

- **`real_botequim_double_mapped_handle`** — `venue_name`/`neighborhood` for
  both `Bar Real Botequim` (Recife, the correct venue) and `Real Bar e
  Lanches` (São Paulo, the wrong one sharing generic name tokens) are
  verbatim from a live `RdsVenueStore.get_venue`/`get_address_bulk` read;
  both venue_ids map to the SAME `instagram.handle` row
  (`group_venue_ids_by_handle("real.botequim")` returned both, live). The 18
  `location_text` values are verbatim from the 18 live, non-superseded
  events under `source_handle='real.botequim'`
  (`RdsVenueStore.list_events_by_handle`) — two spellings the real crawl
  actually produced (`\n`-separated and `—`-separated), reproduced in their
  real observed proportions. `venue_id` values in this fixture
  (`bar_real_botequim`, `real_bar_e_lanches`) are readable placeholders for
  the real, opaque `ven_...` ids (which are recorded in
  `plans/260913_venue-handle-link-audit.md`'s own Evidence section for
  anyone cross-checking against production).
- **`editaisculturape_force_assigned_wrong_venue`** — `venue_name`/
  `neighborhood` for `Casa da Cultura de Pernambuco` are verbatim from a
  live read. All 14 `location_text` values (10 empty, 4 with text) are
  verbatim from the 14 live, non-superseded events under
  `source_handle='editaisculturape'`. Two of the four non-empty values
  ("Casa de Câmara e Cadeia de Brejo da Madre de Deus", "Torre Malakoff")
  are the decisive evidence this plan's Evidence section cites as
  currently undetected by `evaluate_attribution_dispute`; in production,
  12 of these 14 events are `status='accepted'` (served) and the other 2
  are `pending_review` for an unrelated reason (`review_reason=
  'date_range'`) — not captured in this fixture, which only needs
  `location_text` and the expected flag verdict.
- **`teatroluizmendonca_benign_control`** — `venue_name`/`neighborhood` for
  `Theater Luís Mendonça` are verbatim from a live read. All 10
  `location_text` values are verbatim from the 10 live, non-superseded
  events under `source_handle='teatroluizmendonca'`. In production, the one
  exception ("Teatro Nacional") already carries `review_reason=
  'sources_disagree'` and sits in `pending_review` via an existing,
  unrelated mechanism — not itself reproduced in this fixture, which exists
  only to pin that this handle's AUDIT verdict is "not flagged."

- **`beerdock_recife_already_reattributed_elsewhere`** — added by
  `plans/260914_venue-link-audit-checks-current-attribution.md`.
  `venue_name`/`neighborhood` for `BeerDock Boa Viagem` are verbatim from a
  live read. Running the shipped audit against production for the first
  time (2026-09-13/14) re-flagged this handle at 17/46 non-corroborating —
  16 of the 17 sampled events already carry `venue_id='beerdock_casa_forte'`
  (a real, different, specific venue, re-verified live), correctly
  reattributed hours earlier by `plans/260912_events-venue-night-
  duplication.md`'s own backfill (`linked_by='neighbourhood_match'`). This
  fixture is a REPRESENTATIVE SUBSET of the real 46 live events (the 16
  reattributed ones plus the one genuinely still-unresolved
  "Mirante do Paço, Recife"), not the full set — the remaining ~29 events'
  `location_text` values were not individually re-fetched and are not
  fabricated here. Sufficient to prove the fix (expected verdict: NOT
  flagged, since 16/17 are excluded from the check and 1 alone stays below
  the pattern bar of 2) without inventing unverified data for the rest.

## PII review

No personal names appear in any `location_text` value above — every one
names a venue, a municipal/state cultural-bulletin topic, or a street
address. No redaction was needed.

## What this fixture does NOT prove

`teatroluizmendonca`'s benign verdict here is "not flagged by THIS sweep" —
it says nothing about the separate, already-working `sources_disagree`
mechanism that independently flags its one "Teatro Nacional" row in
production. This fixture corpus tests the venue-link-audit comparison and
aggregation in isolation, the same way `compute_dedup_backlog`'s own tests
never exercise `evaluate_attribution_dispute`'s live wiring.
