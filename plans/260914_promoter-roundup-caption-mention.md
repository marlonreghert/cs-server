# Promoter roundup posts must not caption-mention-pin every event to one lucky catalog hit

## Branch
fix/promoter-roundup-caption-mention

## Goal

Stop the resolution ladder's rung 5 (`METHOD_CAPTION_HANDLE_MENTION`) from
force-linking every event in a multi-venue promoter roundup post to whichever
ONE of the post's several named venues happens to already be in our catalog,
and repair the 10 live rows this has already produced.

## Non-goals

- **Not `EventExtractionService._extract_one`'s single-venue-pinning closure,
  and not `events.crawl_target.kind` reclassification.** The task that
  motivated this plan assumed `oquetemhojeemnatal` was mapped `kind='venue'`
  to a single venue (mirroring PR #227's BeerDock shape). Production evidence
  (below) disproves this: the handle is already `crawl_target.kind='promoter'`
  and `promoter_account.status='active'`, has no `instagram.handle` row at
  all, and 807 of its events already resolve through the full resolution
  ladder to dozens of distinct venues. There is no reclassification to do.
- **Not the same defect as PR #227 (`fix/events-venue-night-duplication`).**
  #227's Defect 2 was a single-venue-mapped handle whose fixed-venue closure
  never consulted `location_text` at all — the "same handle, several branches
  of one brand" shape (BeerDock). This defect lives one layer deeper: the
  promoter path's own ladder (`resolve_event_venue`) DOES consult
  `location_text` for every event, and rungs 1-4 already work correctly here
  (see Evidence) — only rung 5, the demoted, POST-level caption fallback, has
  a narrow but real gap specific to a roundup account whose caption names
  many venues and only one happens to be catalogued.
- **Not `venue_link_audit.py`/`venue_link_audit_reviewer.py`.** That pass
  audits whether a `kind='venue'` crawl target's OWN mapped venue is
  corroborated by its posts — a mapping-correctness question for a
  single-venue account. `oquetemhojeemnatal` is `kind='promoter'` and has no
  such mapping to audit; that pass is structurally not the right tool here.
- **Not widening rung 4 (name match) or rung 1-3.** Measured (below): rungs
  1-4 already resolve the large majority of this handle's events correctly.
  Only rung 5's ambiguity check is wrong.
- **Not a general "is this account a roundup" classifier.** The fix is a
  per-post structural signal (do this POST's own extracted events name more
  than one place), not an account-level heuristic — an account can post both
  single-venue content and roundups on different days, and the fix must not
  care which.
- **Not gating auto-merge, dedup, or display-title selection.** Those are
  downstream of `venue_id` and untouched — once these 10 events correctly
  detach to `venue_id = NULL`, they are simply unresolved, exactly like the
  hundreds of sibling rows in this same corpus already are.

## Evidence

All queried live against production RDS on 2026-09-14 (SSM exec into
`vibes_bot-cs-server-1`), and all file/line references re-read from this
worktree on `main` at commit `646e055`.

### The premise in the task brief does not match production

- `events.crawl_target` for `oquetemhojeemnatal`: `kind='promoter'`,
  `enabled=true`, actively crawled (`last_run_at` 2026-09-13,
  `last_run_results=8`, `cursor_posts_at` 2026-09-12).
- `events.promoter_account` for `oquetemhojeemnatal`: `status='active'`.
- `instagram.handle` has **zero** rows mapping `oquetemhojeemnatal` to any
  venue.
- 807 events are sourced from this handle. Venue-id distribution: 527 are
  correctly `venue_id IS NULL` (`venue_not_in_catalog` — a real place we do
  not carry), and the remaining ~280 spread across at least 19 distinct
  venues, not one. `linked_by` breakdown: 188 `handle_mention` (rung 1), 52
  `name_match` (rung 4), 10 `caption_handle_mention` (rung 5) — the last
  group is where the whole reported defect lives.

### The actual incident post, decoded

`instagram.com/p/Dc_QMPayGaz/` (the task's cited post) produced 11 events.
One, "Samba", correctly resolved via rung 1 (`location_text = "@seuchicobotequim"`,
`linked_by=handle_mention`) — a real Chico Botequim event this account also
announced that night. The other 10 ("Luau", "Le Villi", "Segunda de
Vagabundo", "After", "Universal Park", "Forró & Sertanejo", "Segunda
Diferente", "DJs", "Festival do Japão", "Josy Ribeiro") each carry their OWN,
DIFFERENT `location_text` (`@teatroalbertomaranhao.oficial`,
`@eskinaprime`, `@bardokupontanegra`, `@universalparkbrasil`, `@sigatrapia`,
`@sigamatutano`, `@bluebuddhabar`, `@partagenorte`) — none of which are known
venue handles in our catalog. All 10 carry `linked_by='caption_handle_mention'`,
`venue_id` = Seu Chico Botequim.

### Root cause, precisely: rung 5's "unambiguous" check counts CATALOG hits, not places named

`resolve_event_venue` (`app/services/event_venue_resolution.py:565-780`),
rung 5 (`:713-767`): when rungs 1-4 give nothing, it re-reads the whole POST
CAPTION, collects every `@`-mention that resolves to a KNOWN (catalogued)
venue (`_known_venue_mentions`, `:715-717`), and auto-links via
`METHOD_CAPTION_HANDLE_MENTION` **only when exactly one distinct catalogued
venue is named** (`:721-729`) — refusing when the caption names *more than
one* catalogued venue (`:731-767`, `METHOD_AMBIGUOUS_CAPTION_REFUSAL`), per
the module's own stated intent ("A caption naming several known venues... is
worthless as evidence for any ONE event and is refused rather than guessed").

The gap: this post's caption plausibly names on the order of ten venues, but
**only one of them (`@seuchicobotequim`) happens to intersect our ~3,600-venue
catalog**. From the ladder's point of view that reads as "the caption
unambiguously names one known venue" — the exact shape the refusal rule
exists to prevent, missed because the ambiguity check is scoped to catalog
membership instead of to how many places the post's OWN extracted events
actually name. `resolve_event_venue` is called once per event
(`build_location_text_attribute_fn`, `event_venue_resolution.py:804-899`,
called from `PromoterCrawlService._process_post`,
`promoter_crawl_service.py:762-769`) and has no visibility into sibling
events' `location_text` values today — the caller (`_process_post`) already
holds `prepared_events` (every event this post yields, each with its own
`location_text`) before building that closure, but never passes it in.

### Confirmed: this is the ENTIRE current blast radius

Catalog-wide (not scoped to this handle): exactly **10** `events.post_item`
rows anywhere carry `linked_by='caption_handle_mention'`, `status='accepted'`,
and all 10 belong to this ONE post
(`source_handle='oquetemhojeemnatal', source_shortcode='Dc_QMPayGaz'`) — the
only post catalog-wide whose sibling events' own `location_text` values are
more than one distinct value AND which also produced a `caption_handle_mention`
link. No other handle or post currently exhibits this shape. This is a live,
real, but narrow defect: 10 wrongly-served rows today, with the code path
capable of reproducing it on any future promoter roundup post.

### The re-decision engine the backfill already uses never trusted rung 5 anyway

`scripts/backfill_event_venue_links.py`'s `decide_one` (`:398-436`) calls
`resolve_event_venue` with **`caption=None`** (`:416`) — deliberate,
per the script's own docstring: it repairs from "the event's OWN stored
`location_text`... never a fresh model call," and the post's caption is not
stored per-event. With `caption=None`, rung 5 can never fire in this engine
(`_known_venue_mentions(None, ...)` returns nothing), so `decide_one` already
re-derives strictly from rungs 1-4. For these 10 rows (own `location_text` a
bare `@handle` not in the catalog), that means rung 1 fails, rung 4 does not
run at all (`_strip_handles_for_name_match` strips the whole string to
nothing — the docstring's own "nothing with real alphabetic content survives"
case), and the row falls through to `_venue_not_in_catalog_result` — the SAME
outcome hundreds of this handle's other rows already correctly carry. **The
backfill needs no core-ladder fix to correct the 10 rows — it only needs a
new selection filter**, since none of the script's four existing modes
(`handle-mention`, `disputed-location-text`, `force-reassign`,
`unresolved-venue`) select on `linked_by='caption_handle_mention'`
(`_select_candidates`, `:777-798`): `handle-mention` matches only
`METHOD_HANDLE_MENTION` (`:798`, exact rung-1 value) and `force-reassign`
requires `linked_by IS NULL` (`:788`), which a rung-5-linked row never is.

## Current Behavior

- `resolve_event_venue`'s rung 5 auto-links an event with no per-event
  evidence purely because the post's caption happens to name exactly one
  venue this catalog carries — even when the same caption plainly names
  several other, different venues we do not carry.
- `scripts/backfill_event_venue_links.py` has no selection mode that can
  touch a `linked_by='caption_handle_mention'` row at all.

## Desired Behavior

1. Rung 5 must refuse to auto-link an event when the SAME POST's own
   extracted events collectively name more than one distinct place via their
   OWN `location_text` — regardless of how many of those places are
   catalogued. A post yielding events whose own location texts disagree is,
   by definition, a roundup; the caption is then worthless as per-event
   evidence, exactly as the module's existing docstring already claims for
   the "several *known* venues" case, now extended to "several places,
   known or not."
2. A single-venue account's ordinary post (one event, or several events that
   all share one `location_text`, or events with no `location_text` at all)
   is unaffected: rung 5 keeps working exactly as it does today for that
   shape, which is its actual common case.
3. The 10 live, wrongly-pinned rows detach to `venue_id = NULL` with the
   same `venue_not_in_catalog`-shaped outcome their sibling rows already
   carry, via the backfill tool, not a one-off manual UPDATE.

## Implementation Approach

One branch, two commits, mirroring this repo's established "code fix +
backfill mode" pairing (e.g. `260914_recife-venue-mapping-corrections.md`'s
`force-reassign`/`unresolved-venue` modes).

### 1. `event_venue_resolution.py` — sibling-aware rung 5

- `resolve_event_venue` gains one new keyword parameter,
  `sibling_location_texts: Optional[list[str]] = None` (mirrors
  `same_account_venues`'s existing shape: caller-supplied, `None`-safe,
  changes nothing for a caller that does not pass it).
- Rung 5's `len(distinct_caption_venues) == 1` branch (`:721-729`) gains one
  additional guard before auto-linking: the DISTINCT, non-blank values of
  `sibling_location_texts` (excluding blanks) must number at most 1. When
  they number more than 1, fall through exactly as the existing
  `len(distinct_caption_venues) > 1` branch already does (log, and return
  either `_venue_not_in_catalog_result` when this event's own text named an
  unrecognized handle, or `METHOD_AMBIGUOUS_CAPTION_REFUSAL` otherwise) — no
  new outcome branch, reuse the existing tail.
- `build_location_text_attribute_fn` (`:804-899`) gains the same new
  parameter and threads it straight through to `resolve_event_venue`.
- Both call sites that build this closure per post —
  `PromoterCrawlService._process_post` (`promoter_crawl_service.py:762-769`)
  and `EventExtractionService._run_handles`'s multi-venue branch
  (`event_extraction_service.py:893-903`) — already hold every event this
  post yields (`prepared_events` / the loop's own `kept_events`) before
  building the closure; both pass
  `sibling_location_texts=[ev["location_text"] for ev in prepared_events]`.
  No other call site of `build_location_text_attribute_fn` needs a change
  (a caller that never had sibling events to begin with passes nothing,
  which is the existing default).
- Ships as an unconditional correctness fix, not behind an admin-config
  flag: this tightens rung 5 to match its OWN already-documented, already-
  shipped intent (refuse a roundup caption as per-event evidence) rather
  than introducing new behavior: `hide_promoter_events` are followed for
  new admin-config gates in this repo when a behavior is a genuine POLICY
  choice; this is a defect in an existing safety check, and gating it would
  mean shipping code that ships knowingly wrong by default. The measured
  blast radius (10 rows, one handle, one post) also means there is no
  meaningful rollout-risk to stage behind a flag.

### 2. `scripts/backfill_event_venue_links.py` — new selection mode

- New mode `MODE_CAPTION_HANDLE_MENTION = "caption-handle-mention"`, added
  to `MODES` and the CLI `--mode` choices.
- `_select_candidates` gains one branch:
  `e.get("linked_by") == METHOD_CAPTION_HANDLE_MENTION` — the same shape as
  the existing default `handle-mention` branch, filtered on the other method
  constant.
- Reuses `decide_one` completely unchanged (already re-derives from rungs
  1-4 only, per Evidence above — no sibling-location-text plumbing is needed
  in the backfill script, since its `caption=None` call already can never
  reach rung 5).
- Dry-run first (`python scripts/backfill_event_venue_links.py --mode
  caption-handle-mention`), inspect the 10-row report, then `--apply`.
  Idempotent and resumable exactly like every other mode (inherited, not
  reimplemented).

## Data, Config, And API Impact

- No schema change, no new admin-config key, no new Redis key.
- `resolve_event_venue`/`build_location_text_attribute_fn` gain one new
  optional keyword parameter each — non-breaking for every existing caller
  and test that does not pass it.
- 10 `events.post_item` rows change `venue_id` (Chico Botequim → `NULL`),
  `location_resolution` (`auto` → `unresolved`), `linked_by` (
  `caption_handle_mention` → `NULL`), and gain `review_reason =
  venue_not_in_catalog` — via the backfill script, matching its own existing
  write contract (never `merge_touched_events`, left for the next real
  reconciliation). Downstream: these 10 events stop being served (Seu Chico
  Botequim's card in the Eventos tab loses 10 events it never actually
  hosted); vibes_bot and mobile need no change (they only ever see what the
  projection already selects).

## Error Handling And Observability

- No new runtime path beyond the existing rung 5 branch and the existing
  `EVENT_VENUE_LINK_TOTAL{method="ambiguous_caption_refusal"|"venue_not_in_catalog", result=...}`
  counters, which already exist and already cover this outcome — the fix
  changes which branch a call reaches, not what gets counted.
- The existing `logger.info` at the ambiguous-caption-refusal branch
  (`event_venue_resolution.py:754-758`) already logs the mention count; no
  change needed there.
- The backfill script's own existing balance/write-failure checks and
  before/after report (`Report`, unchanged) cover this new mode for free.

## Test Plan

Feature file: `tests/bdd/enrichment/promoter-roundup-caption-mention.feature`

Scenarios:
- A promoter post yielding several events whose own `location_text` values
  name different places, where exactly one of those places is catalogued:
  the event naming the catalogued place still resolves to it via rung 1 (own
  evidence unaffected); every OTHER event in the same post does NOT resolve
  to that catalogued venue via the caption fallback, and instead resolves
  `unresolved` with `venue_not_in_catalog`.
- A promoter post yielding one single event whose own `location_text` is
  empty/uninformative, and whose caption names exactly one catalogued venue:
  rung 5 still auto-links it (regression guard — the common case is
  unaffected).
- A promoter post yielding several events that all share the SAME
  `location_text` (a single-place programme announced per-slot): rung 5
  still auto-links via the caption when it independently names exactly one
  catalogued venue (regression guard — same-place siblings are not a
  roundup).
- The backfill script's new `--mode caption-handle-mention`, dry-run then
  `--apply`, against a fixture row shaped like the 10 live rows: detaches
  `venue_id`, sets `review_reason=venue_not_in_catalog`, and is idempotent
  on a second `--apply`.

Pytest unit tests:
- `resolve_event_venue` with `sibling_location_texts` carrying 2+ distinct
  values: rung 5 refuses even when exactly one caption mention is
  catalogued.
- `resolve_event_venue` with `sibling_location_texts=None` or a single
  distinct value: byte-for-byte unchanged from today's behavior (pinned
  against the existing rung-5 test cases in
  `tests/test_event_venue_resolution.py` — no existing assertion may need to
  change value, only gain the new parameter at its default).
- `scripts/backfill_event_venue_links.py`'s `_select_candidates` with mode
  `caption-handle-mention`: selects only `linked_by='caption_handle_mention'`
  rows, never `handle_mention`/`name_match`/`location_tag` rows.

Manual or integration checks:
- Dry-run `--mode caption-handle-mention` against production, confirm the
  report shows exactly the 10 known rows before `--apply`.
- After `--apply` and the next scheduled crawl of `oquetemhojeemnatal`,
  confirm a re-crawl of a re-fetched roundup post (or a replay via `mode
  handles` against the archived post) no longer reproduces the same 10-way
  mis-link.

## Acceptance Criteria

- `resolve_event_venue` refuses rung 5 for any event whose post has
  sibling events naming more than one distinct place, whether or not those
  places are catalogued.
- All 10 currently-live rows sourced from `oquetemhojeemnatal`/
  `Dc_QMPayGaz` with `linked_by='caption_handle_mention'` are detached from
  Seu Chico Botequim in production via `scripts/backfill_event_venue_links.py
  --mode caption-handle-mention --apply`, and the resulting state matches
  the script's own before/after report with zero unaccounted rows.
- No other currently-`accepted` event's `venue_id` changes.

## Open Questions

None.
