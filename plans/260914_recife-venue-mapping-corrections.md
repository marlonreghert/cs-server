# Recife Venue-Mapping Corrections: Drop Spurious Handle Mappings And Backfill Stuck/Wrong Events

## Branch
fix/recife-venue-mapping-corrections

## Goal
Three `kind='venue'` crawl targets have a wrong `instagram.handle` → venue
mapping, independently re-confirmed against real production data today
(2026-09-14, `i-0893fb6d283243480` / `vibes_bot-cs-server-1`):

- **`editaisculturape`** is force-assigned to one venue ("Casa da Cultura de
  Pernambuco") though it is really a statewide culture-department bulletin
  account with no single correct venue. Fix: remove the mapping entirely and
  reclassify the crawl target as a promoter.
- **`real.botequim`** and **`marcellos_music_bar`** are each double-mapped to
  a correct venue plus a spurious, unrelated, distant one that shares only a
  generic name token. The spurious candidate is diluting/blocking auto-link
  for every event under that handle. Fix: drop the spurious mapping.

For all three, correcting the mapping alone does not fix the events already
written under the wrong (or no) venue — this plan also designs and ships an
idempotent backfill for those existing rows, extending the one existing tool
built for this class of correction (`scripts/backfill_event_venue_links.py`)
rather than inventing new sweep infrastructure.

**`casabacurau` is REMOVED from this plan's scope** — independent
verification against real production data refutes the claim that its posts
describe a different, mismapped venue. See Evidence.

## Non-goals
- `casabacurau` — see Evidence for the full refutation. Not fixed here;
  flagged as a finding for the plan's author to reconcile, not silently
  dropped.
- `botecobeer.jsp` — a confirmed defect of a different, bigger shape (the
  real venue this handle posts about is not in the catalog at all; needs a
  venue ADD, not a relink). Explicitly out of scope per the task brief.
- The other 9 flagged-but-unconfirmed handles from
  `plans/260913_venue-handle-link-audit.md`
  (`downtownbeergarden_`, `garage66.recife`, `entreamigosobode`,
  `conchittasbar`, `rockandribsmarcozero`, `lacasarecife`,
  `emporio_universitarioo`, `saladerebocorecife`, `tatubola.bar`).
- No change to `venue_corroborates_location_text`, `compute_venue_link_audit`,
  `gate_auto_link`'s thresholds (`DEFAULT_CONFIDENCE_FLOOR`/`DEFAULT_MARGIN`),
  or any existing admin-config default.
- No change to `event_dedup_handle_time_match_enabled`,
  `venue_link_audit_enabled`, or `event_venue_advisor_enabled` (all three
  stay at their current, unrelated values).
- No change to `DISPUTE_METHODS`/`brand_root_venues`'s bounding (the
  catalog-wide fuzzy-name-match design was already tried and rejected —
  see Evidence's "Torre Malakoff" / "Torra Café" collision, restated below).
- No widening of `scripts/backfill_event_venue_links.py`'s two EXISTING modes
  (`handle-mention`, `disputed-location-text`) — this plan only ADDS new
  modes alongside them.
- The single `casabacurau` event that names "Clube Ferroviário de Afogados"
  by text (see Evidence) is NOT acted on here — one non-corroborating event
  clears neither `venue_link_audit`'s own 2-event bar nor any dispute rung,
  and a single-event, single-plan special case is out of proportion to this
  plan's actual scope. Left for a future, separately-scoped look if anyone
  revisits it.

## Evidence

### Independent re-verification method
Read-only, via AWS SSM → `docker exec vibes_bot-cs-server-1` →
`python3 -e /app/<script>.py` (`PYTHONPATH=/app`), scripts shipped with
`--parameters file:///path.json` (never inline `commands=[...]`, per the
known multiline-shorthand bug). No write commands were issued against
production at any point. `crawl_scheduler_enabled=true` in prod (confirmed
live) — the per-target crawl schedule is genuinely active, so the mapping
fix below has an immediate, not merely theoretical, effect on future crawls.

### `editaisculturape` — CONFIRMED, force-assigned bulletin account
`crawl_target`: `kind='venue'`, `enabled=true`. One mapped venue in
`instagram.handle`: **Casa da Cultura de Pernambuco**
(`ven_41714e6a4378514669675852637771596f734161655f6e4a496843`, address
"R.Floriano Peixoto - São José, Recife"). 14 live events, all `post_type=
'other'` (not `'event'` — see Implementation Approach for why this matters
to the new backfill mode's selection), all `linked_by=null` (confirming
force-assignment, never through the resolution ladder). Status: **12
`accepted`, 2 `pending_review`** (`review_reason='date_range'`, unrelated to
venue). 10 of 14 have empty `location_text`; the 4 with text, and their real
`name_similarity` score against the mapped venue (re-measured live, not
assumed):

| event_id (short) | location_text | score vs "Casa da Cultura de Pernambuco" | permalink |
|---|---|---|---|
| …DQYX7A7TT7C97GH8HTG6 | "Casa de Câmara e Cadeia de Brejo da Madre de Deus" | 0.4688 | p/DdHUZFtpyiA/ |
| …E71B91DWGGVT1TP2SXQZ | "Torre Malakoff" | 0.2791 | p/DdG8pTYuQs5/ |
| …DH8BQ6Y66Y3MNY2DCR85 | "Mapa Cultural de Pernambuco" | 0.8571 | p/DdKKUQ_JG3X/ |
| …H5SF1EK4BZKTEWN25TRC | "Pernambuco" | 0.5714 | p/Dc8my2eOaBe/ |

Both permalinks in the task brief are real and match. "Casa de Câmara e
Cadeia de Brejo da Madre de Deus" and "Torre Malakoff" are real, different,
specific places, ~200km and ~2km away respectively, neither the mapped
venue — genuine evidence against a single-venue mapping. The two
high-scoring rows ("Mapa Cultural de Pernambuco" 0.8571, "Pernambuco"
0.5714) score high only because they share the token "Pernambuco" with the
mapped venue's own name — both are generic statewide references, not a
specific place, and correctly should NOT resolve to any one venue.
`venue_link_audit.py`'s own docstring independently documents this same
handle/venue pair and count (2 decisive non-corroborating posts, clearing
its `MIN_NON_CORROBORATING_EVENTS=2` bar) — this plan's fresh read matches
it exactly.

### `real.botequim` — CONFIRMED, spurious duplicate mapping
`crawl_target`: `kind='venue'`, `enabled=true`. Two mapped venues:
**Bar Real Botequim** (`ven_34733246316d424f333867526377715a6371543255344a4a496843`,
"Av. Dezessete de Agosto, 1761 - Casa Forte Recife - PE") — correct — and
**Real Bar e Lanches** (`ven_45383746385231355143695251707a58683571634658784a496843`,
"Rua Cardeal Arcoverde 1921 - Pinheiros **São Paulo** - SP") — spurious, a
real but unrelated venue ~2,150 km away. 18 live events, **all 18**
`venue_id=null`, `status='pending_review'`, `linked_by=null`;
`review_reason`: 16× `unresolved_venue`, 2× `missing_date; unresolved_venue`.
`post_type`: 4 event / 9 menu / 4 promotion / 1 food (not all `'event'`).

Every event's `location_text` is one of two verbatim spellings of "Real
Botequim — Av. 17 de agosto, 1761 - Casa Forte" / "Real Botequim\nAv. 17 de
agosto, 1761 - Casa Forte" — the venue's own name and address, restated
across 6 distinct posts. Real, freshly measured `name_similarity` scores:

| mapped venue | score |
|---|---|
| Bar Real Botequim (correct) | **0.4615** |
| Real Bar e Lanches (spurious) | 0.3768 or 0.3881 |

**Correction to the task brief's framing:** the brief frames this as the
spurious second candidate diluting the auto-link margin. The re-measured
score shows the correct venue's OWN rung-4 name-match score (0.4615) is
already **below `DEFAULT_CONFIDENCE_FLOOR` (0.55)** on its own — this is not
a margin/runner-up problem, it fails the absolute floor regardless of
whether a second candidate exists at all. Dropping the duplicate mapping
alone will not make a future re-run of the SCORED ladder (rung 4) auto-link
these events, because "Real Botequim" (the caption's own short form) simply
does not score highly enough against "Bar Real Botequim" (the catalog name)
by this scorer. This directly shapes the backfill design below — see
Implementation Approach.

### `marcellos_music_bar` — CONFIRMED, spurious duplicate mapping
`crawl_target`: `kind='venue'`, `enabled=true`. Two mapped venues:
**Marcello's Music Bar**
(`ven_4d56375362503034666b4e52637771595155536f7351334a496843`, "R. Bom
Conselho, 40 - Arruda Recife - PE") — correct — and **Marcelo Bar**
(`ven_7732387a3830527a35586252637778495a46316a6552724a496843`, "R. Dona
Leopoldina, 632 - Centro **Fortaleza - CE**") — spurious. Note: this is even
more clearly unrelated than the task brief's paraphrase suggested — Marcelo
Bar is not merely a different Recife neighborhood, it is a different city
and state entirely (~800 km away).

7 live events. **6** are `venue_id=null`, `status='pending_review'`,
`linked_by=null`, `review_reason='unresolved_venue'` — the target rows for
this plan. The **7th** (`evt_01M23WBAXK4W06ZG25J807QEHN`) is already
`status='accepted'`, `venue_id`=Marcello's Music Bar, `linked_by=
'sibling_merge'`, `review_reason='sources_disagree'` — already correctly
resolved by an unrelated mechanism; excluded by construction from every
selection in this plan (its `venue_id` is not null). `post_type`: 6 event, 1
promotion. All 6 target events' `location_text` is a spelling of "Rua Ramiz
Galvão, 82 – Arruda (Pátio da Feira do Arruda)", one of them prefixed
"Marcello's Music Bar" / "Marcello's House". Real measured scores range
0.2105–0.4474 for Marcello's Music Bar and 0.2308–0.4082 for Marcelo Bar —
**both candidates score below the 0.55 floor for every single event** (a
street-address-only string never scores well against any venue NAME by this
scorer) — the same "below floor regardless of the duplicate" shape as
`real.botequim`, independently confirmed here too.

### `casabacurau` — REFUTED, not a confirmed defect
The task brief's central claim: "the account's posts describe a different,
real, already-catalogued venue: 'Clube Ferroviário de Afogados' … location_text
names `Rua Capitão Lima, 100`, which is Clube Ferroviário de Afogados's
real, catalogued address, not Casa Bacurau's" (citing permalink
`p/Db4LdoUtNRj/`).

Independent verification directly refutes this:
- Casa Bacurau's OWN catalog address (`venues.address`, live, unedited by
  this investigation) is **"R. Cap. Lima, 100 - Santo Amaro, Recife - PE,
  50040-080"** — i.e. "Rua Capitão Lima, 100" IS Casa Bacurau's own address,
  not someone else's.
- "Clube Ferroviário de Afogados" does exist in the catalog, active
  (`ven_3875515066524b54446e5a52637771667775376e6564464a496843`), but its
  own catalogued address is **"R. Vinte e Um de Abril, 721 - Afogados
  Recife - PE 50820-000"** — a different street, ~4.4 km away
  (lat/lng confirm this), in a different named neighborhood (Afogados vs.
  Casa Bacurau's Santo Amaro).
- The cited permalink `p/Db4LdoUtNRj/` is real and matches two real events
  ("Debate na Casinha ~ estadual", "Coisas leves") — both with
  `location_text='Rua Capitão Lima, 100'`, i.e. exactly Casa Bacurau's own
  address, not Clube Ferroviário's.
- A broader catalog search for `%cap. lima%` turns up **five other
  distinct, real, active venues** on the same street (Galo Padeiro #82,
  Café do Brejo #20, Casa Capitão #124, an unnamed venue #195, Vapor -
  Cozinha Afetiva #210) — a genuine commercial street in Santo Amaro. Casa
  Bacurau at #100 is entirely consistent with this pattern, not an outlier
  or a plausible data-entry collision. A dedicated search for any OTHER
  venue at "…Lima, 100" specifically returns only Casa Bacurau itself.
- Of Casa Bacurau's 34 live events (all `accepted`, all `venue_id`=Casa
  Bacurau, all `linked_by=null`, none with empty `location_text`), **33 of
  34 (97%)** self-corroborate the current mapping: 28 cite "Rua Capitão
  Lima, 100" verbatim (some prefixed "CLUBE DAS RATA PESO —" or "FLIPERAMA
  da casinha;" — a nickname/sub-event framing, not a different venue), 3
  cite "CASA BACURAU"/"Casa Bacurau —…" directly (one scoring a perfect
  1.0), 1 cites "Casinha" alone (a plausible informal nickname). **Only 1 of
  34** (`evt_01M0Z0FR0MKYQMXCWV7E7SNR9B`, "NO BREGA", a brega DJ night,
  permalink `p/Dc0seMCx1Xr/`) names "Clube Ferroviário de Afogados"
  directly and is genuine, isolated, non-corroborating evidence — 1 event,
  not 31, and not the permalink the brief cited as its main evidence.

**Conclusion: the `casabacurau` → Casa Bacurau mapping is correct.** The
task brief's premise for this handle does not survive independent
verification against real production data — this is exactly the kind of
unverified-extrapolation mistake `plans/260914_venue-link-audit-checks-
current-attribution.md`'s own history (a "3-event spot check" over-
generalized to "16 of 17") already burned this repo on once. `casabacurau`
is removed from this plan's fix scope (see Non-goals); a reviewer should
re-check where the "Clube Ferroviário de Afogados" claim in the original
task brief actually came from before it is acted on anywhere else.

### The correction mechanism (existing, not new plumbing)
The handle→venue mapping is **not** stored on `events.crawl_target`
(`app/dao/rds_venue_store.py` `get_crawl_target`/`upsert_crawl_target`/
`update_crawl_target`/`delete_crawl_target` — this table is the handle's OWN
crawl config: cadence, cursors, `kind`, `enabled`; deleting this row would
stop crawling the handle entirely, which is wrong for `real.botequim` and
`marcellos_music_bar`, who should keep being crawled). The real mapping is
`instagram.handle` (`venue_id` PRIMARY KEY, `instagram_handle`, `payload`,
`deleted_at`, `updated_at` — migration `0001_baseline_schemas.py`; a
double-mapped handle is simply TWO rows, one per venue_id, sharing the same
`instagram_handle` string), read everywhere via
`VenueRepository.list_instagram_handles()` →
`instagram_handle_sources.group_venue_ids_by_handle(...)`.

Two existing, already-shipped, audited DAO methods correct it — no new
plumbing needed:
- `VenueRepository.delete_venue_instagram(venue_id)` → soft-deletes that
  venue's `instagram.handle` row (`deleted_at=now()`), which drops it out
  of `list_instagram_handles()`'s `WHERE deleted_at IS NULL` read
  immediately — used today by `google_places_enrichment_service.py`.
- `VenueRepository.set_venue_instagram(VenueInstagram(...))` → upserts
  (creates or reactivates) a venue's `instagram.handle` row — used today by
  `instagram_cascade_service.py`, `instagram_enrichment_service.py`,
  `google_places_enrichment_service.py`.

Both write through `RdsVenueStore.upsert_enrichment`/`soft_delete_enrichment`,
which append a row to **`audit.enrichment_history`**
(`schema_name, table_name, venue_id, payload, operation, timestamp`) on
every call — an append-only, already-existing audit trail for this exact
table. `redis_projection_service.py` already registers
`"instagram.handle": (VenueInstagram, "set_venue_instagram",
"delete_venue_instagram")` in its RDS→Redis re-assertion map, so a write
through these methods reaches the serving projection automatically within
`REDIS_PROJECTION_MINUTES` — no separate propagation step.

`_chain()` (`app/services/instagram_crawl_service.py`) reads
`group_venue_ids_by_handle` fresh on every crawl: `len(venue_ids) == 1` goes
through `EventExtractionService`'s force-assign path (no ladder, no
floor/margin — the shipped, accepted design for a genuinely single-venue
handle, `plans/260806_venue-post-multi-event.md §D`); `len(venue_ids) > 1`
goes through `_chain_shared_handle`'s scored ladder; `len(venue_ids) == 0`
under `kind='venue'` logs a warning and **archives/extracts nothing** —
which is why `editaisculturape` additionally needs its `kind` changed (see
Implementation Approach), while `real.botequim`/`marcellos_music_bar` do
not (they land on exactly 1 remaining venue_id, which is the correct,
already-proven single-venue force-assign path).

### The dedup-merge audit trail does not apply here (checked, not assumed)
`events.event_merge_suggestion` (migration `0039_event_merge_suggestions.py`)
is scoped to fuzzy-title/shared-lineup DEDUP merges only — `event_id`/
`candidate_event_id` are both `events.post_item` rows being considered for
ABSORPTION into each other, with a `moved_source_ids`/reverse-merge
mechanism specific to that. It has no concept of "venue reattribution" and
is never touched by `scripts/backfill_event_venue_links.py`.
`plans/260912_events-venue-night-duplication.md` states this explicitly,
twice: "Attribution repair is **not** reversible beyond the dry-run report;
that report is the only record of previous `venue_id`s." That IS the real,
existing equivalent for a venue relink — a captured dry-run report file, not
a database-level undo — and this plan follows the same convention rather
than inventing a stronger guarantee the rest of the codebase does not have.
`audit.enrichment_history` (above) covers the MAPPING correction
(`instagram.handle`); it does not and cannot cover the EVENT row backfill
(`events.post_item` is not one of its `_ENRICHMENT` tables).

## Current Behavior
- `editaisculturape`: every post force-assigned to "Casa da Cultura de
  Pernambuco" regardless of content; 12 wrong events served today.
- `real.botequim` / `marcellos_music_bar`: every event stuck at
  `venue_id=None`, `pending_review`, invisible in the app, because a
  spurious second mapped venue exists (though, per Evidence, even removing
  it will not let rung 4 alone resolve them — see Implementation Approach).
- `casabacurau`: correctly mapped and correctly serving; no defect.

## Desired Behavior
- `editaisculturape`'s handle no longer force-assigns any venue; its 14
  already-wrong events are detached and given a fair, catalog-wide shot at
  auto-resolution through the existing ladder, landing whatever a fresh
  evaluation of their own frozen text produces (predicted, per Evidence's
  score table: unresolved / `pending_review`, since none of its events'
  scores clear the floor against any candidate — to be confirmed by the
  actual dry-run, not assumed).
- `real.botequim` / `marcellos_music_bar` map to exactly their correct
  venue; their existing stuck events are force-assigned to that venue
  (mirroring the exact behavior a fresh crawl of an already-single-mapped
  handle already performs unconditionally for every future post) and
  re-enter `accepted` wherever no unrelated review reason remains.
- `casabacurau` is untouched.

## Implementation Approach

### 1. The mapping fix (per handle, one-line DAO calls)
- `real.botequim`: `venue_repository.delete_venue_instagram(
  "ven_45383746385231355143695251707a58683571634658784a496843")` (Real Bar e
  Lanches). Leave Bar Real Botequim's row untouched.
- `marcellos_music_bar`: `venue_repository.delete_venue_instagram(
  "ven_7732387a3830527a35586252637778495a46316a6552724a496843")` (Marcelo
  Bar). Leave Marcello's Music Bar's row untouched.
- `editaisculturape`: `venue_repository.delete_venue_instagram(
  "ven_41714e6a4378514669675852637771596f734161655f6e4a496843")` (Casa da
  Cultura de Pernambuco — the only mapped venue). Additionally
  `venue_repository.update_crawl_target("editaisculturape", {"kind":
  "promoter"})` — verified (not assumed) this changes exactly one thing that
  matters here: `_chain()` routes a `kind='promoter'` target straight to
  `chain_promoter` (full-catalog candidates, the standard per-post
  resolution ladder every other promoter account already uses), instead of
  `kind='venue'` with zero mapped venues, which logs a warning and archives/
  extracts NOTHING for every future post — silently wasting the Apify spend
  already being made on this handle. `chain_promoter` needs no other
  wiring change. This also removes the handle from
  `collect_venue_link_audit`'s own `list_crawl_targets(kind="venue")` scope
  going forward, which is correct — there is no longer a fixed mapping to
  audit.

These three calls are the only write path; a small operator script (not a
sweep) invoking the two DAO methods directly, one line per handle, run once
under `execute-feature`'s explicit go-ahead gate (see Manual/integration
checks).

### 2. The historical backfill — two new modes on the existing tool, not new infrastructure
Extend `scripts/backfill_event_venue_links.py` (already the tool for "repair
`events.post_item` rows whose venue attribution needs correcting," already
dry-run-by-default / `--apply` / idempotent / resumable / no network client
/ operator-edit-always-wins) with two new `--mode` values, alongside (never
replacing) the two existing ones. Both new modes MUST NOT filter on
`post_type == 'event'` the way `disputed-location-text` does — verified live
that `editaisculturape`'s 14 target rows are ALL `post_type='other'`; that
filter is specific to that mode's own semantics, not a universal safety
rule, and reusing it here would silently exclude every real target row.

**`--mode force-reassign`** (new engine — handles both "detach" and
"force-assign-from-unresolved," since both are the same shape: an
unconditional, operator-pinned `venue_id` write, never a ladder re-run).
Takes `--handle`, `--from-venue-id` (a specific id, or the literal `none`),
`--to-venue-id` (a specific id, or the literal `none`) — one explicit,
reviewable invocation per handle, never a generic sweep. Selection:
`source_handle == handle AND linked_by IS NULL AND venue_id == (from_venue_id
or NULL)`. Applies the SAME skip table every existing mode already applies
(confirmed / manual_link / superseded / operator_edited_venue — an
operator's own prior correction always wins, unconditionally, exactly as
today), then sets `venue_id = to_venue_id` (or `NULL`), `location_resolution
= RESOLUTION_MANUAL` when `to_venue_id` is set (this is an operator-pinned
correction, not a fresh ladder verdict — `RESOLUTION_MANUAL` is the existing
sentinel for exactly that, otherwise unused by any automated path) or
cleared when detaching to `NULL`, folds `unresolved_venue` out of
`review_reason` on a successful assign (reusing the existing
`_fold_review_reason`/`_fold_no_venue_reason` machinery — already imported),
and recomputes `status` through the SAME `is_clean_extraction`-based tail
`decide_one`/`decide_one_disputed` already use. Concretely:
  - `--handle real.botequim --from-venue-id none --to-venue-id
    ven_3473...4a4a496843` (Bar Real Botequim) — 18 rows.
  - `--handle marcellos_music_bar --from-venue-id none --to-venue-id
    ven_4d56...7351334a496843` (Marcello's Music Bar) — 6 rows (the 7th,
    already `venue_id`-set, is excluded by the `venue_id == from_venue_id`
    filter by construction).
  - `--handle editaisculturape --from-venue-id
    ven_41714e6a...5f6e4a496843 --to-venue-id none` — 14 rows (detach).

**`--mode unresolved-venue`**: reuses `decide_one`'s EXISTING engine
(`resolve_event_venue` against the full catalog, `gate_auto_link` at the
unchanged floor/margin) completely unchanged — only the SELECTION is new:
`venue_id IS NULL`, optionally scoped by a repeatable `--handle` filter
(this plan always scopes it — `--handle editaisculturape` — never invoked
catalog-wide here, though the capability is now available for a future,
separately-reviewed general sweep, closing the gap this investigation found:
no such sweep exists anywhere in the repo today). Run AFTER the detach step
above, as a second, separate pass, so `editaisculturape`'s now-`venue_id=
NULL` rows get the identical fair shot at auto-resolution `chain_promoter`
already gives every future post from this handle.

`real.botequim`/`marcellos_music_bar` deliberately do NOT go through
`unresolved-venue` — per Evidence, their correct venue's own rung-4 score
(0.4615, 0.2105–0.4474) is below the 0.55 floor regardless of the spurious
duplicate, so a scored re-run would leave them exactly as stuck as today.
`force-reassign` mirrors what the corrected single-venue mapping already,
unconditionally, does for every future post under that handle — the
correct, already-proven mechanism for a genuinely single-venue handle,
applied retroactively.

**Refactor note (in-scope cleanup, not a new abstraction for its own
sake):** the skip-table check and the `is_clean_extraction`-based status
tail are already duplicated once, between `decide_one` and
`decide_one_disputed` (confirmed by reading the file). Adding a third
decision function without factoring this out would make it three copies;
`execute-feature` should extract the shared skip-table + status-restoration
tail into one helper both existing functions and the new `force-reassign`
decision call, removing the existing duplication rather than adding to it.

**Idempotency (by construction, matching the existing two modes' own
posture):** every new mode's selection excludes rows the mode has already
corrected — `force-reassign`'s selection requires `venue_id == from_venue_id`
literally, so a row that already moved to `to_venue_id` no longer matches on
a second run; `unresolved-venue`'s selection requires `venue_id IS NULL`, so
a row that resolved on the first run is excluded on the second, and a row
that legitimately stayed unresolved produces the same "still unresolved"
verdict (an `unchanged` outcome, no write) both times.

### 3. Sequencing
Mapping fix → mapping fix → mapping fix (three independent DAO calls, any
order) → `force-reassign` for `real.botequim` and `marcellos_music_bar`
(independent of each other and of `editaisculturape`) → `force-reassign`
(detach) for `editaisculturape` → `unresolved-venue --handle
editaisculturape` (must run after the detach). Each handle's fix is its own
commit and its own BDD/pytest scenario per the task brief's requirement —
sequencing within one plan does not mean one commingled change.

## Data, Config, And API Impact
- `instagram.handle`: 2 soft-deletes (Real Bar e Lanches, Marcelo Bar), 1
  soft-delete (Casa da Cultura de Pernambuco) — each appends one
  `audit.enrichment_history` row.
- `events.crawl_target`: one field update (`editaisculturape.kind`:
  `'venue'` → `'promoter'`). No schema change.
- `events.post_item`: `venue_id`, `location_resolution`, `review_reason`,
  `status`, `linked_at` updated on 14 (real.botequim) + 6
  (marcellos_music_bar) + 14 (editaisculturape) = 34 rows, all via the
  existing `update_event` allowlist — no new column, no migration.
- No API response shape change; no admin-config key added or changed.

## Error Handling And Observability
No new runtime path in the live serving/crawl pipeline — `chain_promoter`
and the single-venue force-assign path are both existing, exercised code.
The backfill script keeps its existing `ArithmeticImbalance` /
`WriteAffectedNoRows` hard-stops (report printed, non-zero exit, no partial
silent state) unchanged, now covering the two new modes too. No new
Prometheus metric is needed — `CRAWL_RUNS_TOTAL{handle_kind=...}` already
reports `editaisculturape` under its new `kind` label for free once the
crawl_target row is updated.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-mapping-correction.feature`

Scenarios (each its own commit/step-definition unit per the task brief):
- Dropping a spurious duplicate venue mapping lets a handle's
  previously-stuck events become force-assignable to its one remaining
  venue — pinned with `real.botequim`'s real captured strings/ids (Bar Real
  Botequim vs. the spurious Real Bar e Lanches mapping, all 18 events
  `venue_id=None` before, force-assigned after).
- The same shape for `marcellos_music_bar` (Marcello's Music Bar vs. the
  spurious Marcelo Bar mapping) — a second, independent scenario/commit
  proving the mode generalizes across two real, differently-shaped corpora,
  not one lucky case; also asserts the 7th, already-`sibling_merge`-resolved
  event is left untouched.
- Removing a force-assigned mapping from a multi-venue bulletin account
  clears its events' wrong `venue_id` and lets them re-enter resolution —
  pinned with `editaisculturape`'s real captured data (14 events, the
  "Torre Malakoff" / "Casa de Câmara e Cadeia de Brejo da Madre de Deus"
  posts), asserting the detach step and the subsequent `unresolved-venue`
  pass both run and produce the measured (not merely predicted) outcome.
- Repointing a single wrong venue mapping to a specific correct venue
  reattributes its affected events — **pinned with a clearly-labeled
  SYNTHETIC fixture, not live data**: `casabacurau`, the task brief's
  intended real-world target for this scenario shape, was independently
  investigated and refuted (see Evidence) and has no confirmed real
  replacement at plan time. This scenario exists to prove the
  `force-reassign(from=X, to=Y)` code path generically and keep it ready for
  a future confirmed case; it is not applied against production in this
  plan (see Manual/integration checks — no `--to-venue-id` other than
  `none` is ever run against real data here).
- A row protected by the existing skip table (an operator-edited `venue_id`,
  or an already-`confirmed`/`manual_link`/`superseded` row) is never touched
  by either new mode, matching the existing modes' own guarantee.
- Running any new mode a second time with `--apply` changes nothing (true
  idempotency, not just "no error") — one scenario per new mode.

Pytest unit tests (`tests/test_backfill_event_venue_links.py`, extend):
- `force-reassign`'s selection: only rows matching `handle` + `linked_by IS
  NULL` + `venue_id == from_venue_id` are selected; a row with any other
  `venue_id`, a non-null `linked_by`, or a different handle is excluded.
- `force-reassign` detach (`to=None`) clears `venue_id`, clears
  `location_resolution`, folds `unresolved_venue` in (not out), and drives
  `status` to `pending_review` via `is_clean_extraction`.
- `force-reassign` assign (`to=<id>`) sets `venue_id`, sets
  `location_resolution=RESOLUTION_MANUAL`, folds `unresolved_venue` out, and
  drives `status` to `accepted` when no other review reason remains — and
  correctly stays `pending_review` when an unrelated reason (e.g.
  `missing_date`) is still present, pinned against `real.botequim`'s own
  real 2 `missing_date; unresolved_venue` rows.
- `unresolved-venue`'s selection: only `venue_id IS NULL` rows, scoped to
  the given `--handle` set when provided.
- `unresolved-venue` reuses `resolve_event_venue` unchanged — a fixture
  event whose text clears the floor against the full catalog auto-links; one
  that doesn't (pinned against `editaisculturape`'s real low-scoring rows)
  stays unresolved.
- The shared skip table (confirmed / manual_link / superseded /
  operator_edited_venue) applies identically under both new modes — reuse
  or extend the existing `TestPerStatusActionTable`/`TestOperatorEditedFields`
  coverage rather than re-deriving it.
- Idempotency: a second `--apply` of each new mode is a true no-op (reuse
  the existing `TestIdempotency` pattern).
- Arithmetic balance holds for both new modes (reuse `TestArithmeticBalance`).

Manual or integration checks:
- Dry-run (no `--apply`) each new mode invocation against real production
  data first, via the established read-only AWS SSM pattern, and record the
  real before/after counts in a follow-up note — **do not run `--apply`
  against production during this plan's own execution; `execute-feature`
  must get explicit operator go-ahead before any live write**, per this
  repo's own convention (see the `casabacurau` refutation above for exactly
  why this gate matters).
- After a real `--apply` (once approved): re-run
  `scripts/measure_venue_link_audit.py --production-only` read-only and
  confirm `real.botequim`/`marcellos_music_bar`/`editaisculturape` drop out
  of (or measurably change in) the flagged list, and that `casabacurau`
  remains unflagged/unchanged (a regression guard proving this plan touched
  nothing there).
- Confirm the Redis-projected `instagram.handle` fields for the three
  touched venues reflect the correction within one `REDIS_PROJECTION_MINUTES`
  window after `--apply`.

## Acceptance Criteria
- `real.botequim`'s `instagram.handle` row for "Real Bar e Lanches" is soft-
  deleted; "Bar Real Botequim" untouched; all 18 previously-stuck events
  carry `venue_id`="Bar Real Botequim" after `force-reassign --apply`; the 2
  rows with an unrelated `missing_date` reason correctly remain
  `pending_review` for that reason alone.
- `marcellos_music_bar`'s "Marcelo Bar" row is soft-deleted; "Marcello's
  Music Bar" untouched; all 6 previously-stuck events carry `venue_id`=
  "Marcello's Music Bar"; the pre-existing `sibling_merge` event is
  unchanged.
- `editaisculturape`'s "Casa da Cultura de Pernambuco" row is soft-deleted;
  `crawl_target.kind` is `'promoter'`; all 14 events are detached
  (`venue_id=None`) and then re-evaluated once through
  `unresolved-venue`, landing wherever that fresh evaluation genuinely
  produces (recorded, not assumed).
- `casabacurau` is untouched by any part of this plan — verified via the
  post-`--apply` audit re-run above.
- A second `--apply` of every new mode invocation changes nothing.
- `make test-unit`, `make test-bdd` pass; no `@wip` tag remains after
  `execute-feature`.
- No change to `venue_link_audit_enabled`, `event_dedup_handle_time_match_
  enabled`, `event_venue_advisor_enabled`, or any other pre-existing
  admin-config default.

## Open Questions
- Where did the task brief's "Clube Ferroviário de Afogados" / "Rua Capitão
  Lima, 100" claim for `casabacurau` actually come from? It does not match
  the real catalog data (see Evidence) — worth tracing before it resurfaces
  in a future plan. Does not block this plan, since `casabacurau` is simply
  excluded from scope.
- Should the single genuinely anomalous `casabacurau` event ("NO BREGA" /
  `evt_01M0Z0FR0MKYQMXCWV7E7SNR9B`, naming "Clube Ferroviário de Afogados")
  ever be looked at on its own? Not blocking — explicitly named a non-goal
  above; raised here only so it is not silently lost.
- Exact final field values `force-reassign`'s detach branch should leave on
  `location_resolution`/`confidence` — this plan specifies the intent
  (clear `location_resolution`, leave `confidence` alone) but
  `execute-feature` must confirm against `update_event`'s real column
  allowlist and the fake/real DAO parity tests before finalizing
  `Decision.write_fields()` for the new modes.
