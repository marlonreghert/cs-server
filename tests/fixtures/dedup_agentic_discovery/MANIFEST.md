# Provenance manifest — `dedup_agentic_discovery/corpus.json`

See `plans/260913_dedup-agentic-mitigation-discovery.md` Phase 1. Matches the
provenance-manifest convention `tests/fixtures/besttime/PROBE_MANIFEST.md`
already established in this repo: per-item sourcing, reviewed for PII before
commit, so a future reader never has to guess which field is real data and
which is a labeled reconstruction.

None of this manifest's production evidence was obtained by writing
anything: every number below came from a read-only Python scan shipped to
the `vibes_bot-cs-server-1` container on EC2 instance `i-0893fb6d283243480`
via AWS SSM `send-command`, run against the ALREADY-MERGED `main` branch
(commit `33ff939`, PRs #227-#229) — not this branch's own new code, which
did not exist on that host at scan time.

## Per-case provenance

- **`club_metropole_fragmentation`** — titles verbatim from
  `plans/260912_events-venue-night-duplication-rca.md`. `venue_id`,
  `instagram_handle`, `event_id` and the exact lineup membership were not
  captured in the RCA text and are reconstructed to match the RCA's own
  stated aggregate (exactly 3 of 5 posts share one performer, producing
  exactly 3 of the 10 pairs sharing a name — see the case's own
  `provenance` field for the exact reconstruction rule).
- **`beerdock_multi_location_misattribution`** — venue names, the mapped
  handle, and the `CASA FORTE`-shaped location_text are real (RCA, plus a
  2026-09-13 production re-scan confirming at least one live
  "BeerDock Boa Viagem" row still carries `location_text = "Beer Rock - Casa
  Forte"`, and that "Beerdock Casa Forte" exists in the catalog). Individual
  post titles are synthetic placeholders — the RCA never quoted BeerDock's
  per-post titles.
- **`oquetemhojeemnatal_xepa_bar_roundup`** — venue name/id, the
  `oquetemhojeemnatal` handle, every `location_text`, and every title are
  verbatim from the 2026-09-13 production scan (see "Production scans"
  below). `event_id`/`starts_at` were not captured by that scan (it read
  title/location_text/handle only) and are reconstructed placeholders.
- **`casa_bacurau_unrelated_venue_gap`** — `event_id`, the mapped venue_id
  (handle `casabacurau`), the title, and the `location_text` are verbatim
  from the 2026-09-13 scan; the scan also ran the real
  `app.services.event_venue_resolution.resolve_event_venue` ladder against
  it and confirmed the rung-4 `name_match` to `"Clube Ferroviário de
  Afogados"` at score 1.0. `starts_at` is a reconstructed placeholder. This
  is a GENUINE production finding, not a constructed example — see "Why
  this is the real 'barchef' case" below.
- **`ação_leitura_non_duplicate_control`**,
  **`oficina_workshops_non_duplicate_control`**,
  **`ferias_amigos_park_non_duplicate_control`**,
  **`casanova_ecobar_non_duplicate_control`** — titles and venues verbatim
  from `plans/260812_event-dedup-fuzzy-title.md`'s own Evidence section (the
  false-positive corpus that set the existing auto-merge bar), already
  committed and reused elsewhere in this repo
  (`tests/bdd/steps/event_dedup_fuzzy_title_steps.py`). No new PII exposure:
  these strings are already in this worktree's own history.

## PII review

- No private individual's full legal name appears anywhere in this corpus.
  Two performer/DJ stage names appear (`Cleo Lima`, `Flô Core`, in the real
  `oquetemhojeemnatal_xepa_bar_roundup` titles) and one real author's name
  appears in the reused 260812 control (`Marcelino Freire`, `Jeferson
  Tenório`) — all names a venue itself published on a public promotional
  Instagram post, the same risk level this repo's own test suite already
  carries elsewhere.
- Every `instagram_handle`/`source_handle` value is a business or promoter
  account handle, never a private individual's account.
- No raw caption text is reproduced verbatim from any real post; every
  `caption` field in the corpus is `null`. Only `location_text` (a short,
  structured field the extraction model reads off a flyer, not free prose)
  and the post's own `title` are carried for the real cases.

## Why this is the real "barchef" case

The task that produced this corpus was told that a literal search of
production for the handle `barchef` had already found nothing (confirmed
again here: the 2026-09-13 scan's own literal search for `barchef` found two
handles, `barchef.riomar` and `barchef.boteco`, and ZERO live events sourced
from either — see raw scan output below). Rather than construct a synthetic
stand-in for the "hidden-promoter"/unrelated-venue pattern the plan's own
BDD feature describes, the scan instead ran the REAL resolution ladder
(`resolve_event_venue`, identity rungs only where relevant, full rung 4
otherwise) against every live, single-venue-mapped event's own
`location_text` with no `@` mention, looking for a rung-4 `name_match` to a
DIFFERENT, already-catalogued venue OUTSIDE the mapped venue's own brand-root
set. Scanned 226 qualifying events (bounded scan, capped for a read-only
production query); found 8 such matches. `casa_bacurau_unrelated_venue_gap`
is the cleanest of the 8 (score 1.0, the matched venue is a real, totally
unrelated establishment, and `casa` — the mapped venue's own leading token —
is itself one of `event_attribution_dispute.DEFAULT_BRAND_ROOT_STOPWORDS`, so
rung 3 is structurally disabled for it and cannot be accused of silently
covering the gap). A second, independent instance at the SAME handle
(`casabacurau` → `"Casinha"` → `"Restaurante Casinha Mineira"`, score 0.95)
corroborates that this is a real, repeating pattern for this account, not a
one-off coincidence.

## Raw production scan evidence (read-only, 2026-09-13)

Two SSM `send-command` invocations against `i-0893fb6d283243480`
(`vibes_bot-cs-server-1`), both read-only Python scripts using the same
`VenueRepository(client=None, rds_store=RdsVenueStore(settings.
rds_sqlalchemy_url))` DAO construction this repo's own scripts use. Key
figures, for traceability:

- `total_events_all=1376`, `live_events=1043` (post_type=event,
  status != superseded).
- `resolution_queued_count=44` (RESOLUTION_QUEUED — `location_resolution IS
  NULL AND venue_id IS NULL`).
- `total_handles=2014`, `handles_mapping_to_multiple_venues_count=57`.
- `promoter_accounts_by_status={"active": 2}`, `promoter_accounts_total=2`.
- `pending_merge_suggestions=29`.
- `hidden_promoter_scan_events_scanned=226`,
  `hidden_promoter_candidates_found=8`.
- `barchef_handle_matches=["barchef.riomar", "barchef.boteco"]`,
  `barchef_event_matches=0`.
- "BeerDock Boa Viagem": `live_event_count=26`, handle `beerdock_recife`;
  sampled `location_text` values include `"BOA VIAGEM"` (x5), `"Beer Rock -
  Bon Viagem"` (x2), and `"Beer Rock - Casa Forte"` (x1) — the mis-attributed
  shape is still live today, not fully repaired.
- "Xepa Bar": `live_event_count=3`, handle `oquetemhojeemnatal`; every
  sampled `location_text` names a different, uncatalogued `@handle`
  (`@casadorock666`, `@abissal_bp`, `@carrancas_rn`).
- "Seu Chico Botequim" (included for contrast, NOT added to the corpus as a
  case): `live_event_count=33`, handle `oquetemhojeemnatal`; every sampled
  `location_text` is the venue's OWN handle (`@seuchicobotequim`) — this one
  resolves correctly today (rung 1, handle_mention), unlike its four
  siblings in the same roundup account.

Full scan output (JSON) is not committed here — it is a point-in-time
production read with real `venue_id`/`event_id` values already reproduced
in `corpus.json` where used as a case; the rest is summarized above rather
than duplicated.
