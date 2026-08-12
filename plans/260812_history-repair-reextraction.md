# History Repair: Re-extraction — recover what only the model can tell us

## Branch
fix/history-repair-reextraction

## Goal
Recover the two facts no deterministic pass can reach: posts mis-typed as
`event` that are really greetings or recaps, and the roundup events discarded
past the per-post cap. From archived S3 images, never Apify, at a cost estimated
before a single call is made.

## Non-goals
- **Anything a deterministic pass can already fix.** Dates are
  `260812_history-repair-dates.md`; venue links are
  `260812_backfill-misattributed-links.md`. Re-extraction is the expensive last
  resort and must not be used where stored data suffices.
- **Re-crawling.** Every post this plan touches is already archived. No Apify
  call, for any reason.
- **Improving the prompt.** `260812_event-attribution-and-dates.md` §E and
  `260812_events-per-post-cap.md` own that; this plan re-runs what they built.
- **A full corpus re-extraction.** See §B — the target set is narrow by design.

## Evidence

### Two defects survive every deterministic repair
- **`'31 Anos'`** is `post_type = 'event'`, `category = 'party'`, with
  description `"Parabéns pelos seus 31 anos! Feliz aniversário!"`, no lineup and
  no time. It is a birthday greeting. Whether a post *announces* something or
  merely *talks about* it is a judgement about meaning; no stored column encodes
  it, so no script can derive it.
- **Roundups truncated at the cap.** `openai_event_extraction_client.py:524`
  slices `events_raw[:max_events]` **after** generation. The events past index 20
  were produced, paid for, and thrown away — they exist in no column, no
  manifest, and no log. Twenty source posts hit the cap in the 2026-08-12
  snapshot, all from `@oquetemhojeemnatal`.

### Re-extraction is what the venue backfill deliberately refused to do
`260812_backfill-misattributed-links.md` rejected re-running the model, and its
reason applies here with full force: a fresh answer can return a different
`date_text` or a re-phrased `title`, which changes
`compute_source_event_key` and orphans an operator's confirmation. That plan
could avoid the model entirely. **This one cannot**, so the hazard has to be
engineered around rather than sidestepped — see §C. Anyone executing this plan
who has not read that reasoning will reintroduce the bug it avoided.

### The images are already ours
`MediaArchiveStore` holds every post's images and
`read_image_data_uri` already feeds them to the extractor — the same seam the
existing re-extraction path uses. Cost is OpenAI only.

## Current Behavior
A greeting sits in the catalogue as a party. Roundup events past the twentieth
do not exist. Neither is reachable without asking the model again, and nothing
asks it again.

## Desired Behavior
1. Re-extract a narrow, justified set of archived posts.
2. Preserve the identity of events that already exist.
3. Add genuinely new events without disturbing their siblings.
4. Re-type posts the improved prompt now classifies correctly.
5. Never overwrite an operator.
6. Know the cost before spending it.

## Implementation Approach

Two modes of one operator script,
`python -m scripts.reextract_archived_posts --mode {post-type,truncated}`,
dry-run by default, `--apply` to write, resumable, with a hard `--max-posts`.

### A. Prerequisites, enforced not assumed
- `260812_event-attribution-and-dates.md` §E deployed — otherwise the greeting
  re-extraction returns `event` again and we pay to learn nothing.
- `260812_events-per-post-cap.md` deployed with a raised cap — otherwise the
  truncated mode re-truncates at the same point.
- `260812_history-repair-dates.md` **already applied**. Run date repair first:
  it is free and it fixes dates on rows this plan would otherwise re-extract at
  cost.

Assert each by importing a symbol the change introduces and aborting on
`ImportError`, the pattern `backfill-misattributed-links` uses. A no-op run that
bills OpenAI is the specific waste this guards.

### B. Two narrow target sets — never the whole corpus
**Truncated mode** targets exactly the posts the cap-truncation signal from
`260812_crawl-error-visibility.md` §D flags. That set is precise and needs no
heuristic. It is also the higher-value mode: each post is hiding an unknown
number of real events.

**Post-type mode** has no such signal, so it needs a **cheap pre-filter, applied
before any model call**. Candidate shape, to be tuned against the dry run rather
than trusted as written: `post_type = 'event'` **and** an empty `lineup`
**and** `time_known` false **and** a short description. `'31 Anos'` matches all
four.

State the filter's recall honestly in the PR: it will miss greetings that happen
to name performers, and that is the correct trade. **The alternative — filtering
nothing — is a corpus-wide re-extraction whose cost nobody has justified.** If
the filter yields an implausibly large set, stop and narrow it; do not raise
`--max-posts`.

### C. Pin identity across re-extraction — the crux
For each re-extracted post, match returned events against the stored
`post_item_source` rows for that `(source_handle, source_shortcode)` **before
writing anything**:

- **Matched** (same `source_event_index` and a title the existing similarity
  rules call the same): **keep the stored `source_event_key` unchanged.** Update
  only the fields this plan exists to change — `post_type`/`category` in
  post-type mode. Do **not** let a re-phrased title or a re-read `date_text`
  rewrite the key. The stored key is the anchor an operator's confirmation hangs
  on, and preserving it is the entire reason this mode is safe.
- **New** (an index beyond the stored maximum — the truncated tail): a normal
  insert, with a freshly computed key. These are the events the cap ate.
- **Missing** (stored but not returned this time): **leave it exactly as it is.**
  A model that returns nineteen events where it once returned twenty has not
  proven the twentieth was wrong. Never delete on absence.

Reuse the existing re-extraction reconciliation
(`event_reconciliation`'s same-post pairing) rather than writing a second
notion of "the same event within a post". If it cannot express "keep the old
key", extend it there so both paths share one answer.

### D. Never overwrite an operator
`operator_edited_fields` wins field by field. A `confirmed` row is not re-typed
silently — report it as "the prompt now disagrees with a confirmation" and let a
human decide, matching `history-repair-dates` §C.

### E. Know the cost first
The dry run must **estimate spend before any spending**: number of posts, images
per post, and a projected cost from the per-1k input/output pricing that
`260812_events-per-post-cap.md` adds to config. Print it and require `--apply`
to be a separate, deliberate invocation.

Respect the standing spend discipline: **do not run a batch whose estimate
exceeds the agreed threshold without explicit approval.** Start with
`--max-posts` small, inspect the results, then widen. A resumable script makes
that cheap; a single unbounded run does not.

Record actual spend against the estimate afterwards. The estimate is a guess
until it has been checked once.

## Data, Config, And API Impact
- **Migration** — none.
- **Config** — reuses the extraction pricing settings from
  `260812_events-per-post-cap.md`; no new keys.
- **New file** — `scripts/reextract_archived_posts.py`.
- **Rollback:** none for rows already written. Mitigated by dry-run-first,
  `--max-posts`, the captured report, and an RDS snapshot before `--apply`.

## Error Handling And Observability
- Count re-extractions by mode and disposition: matched-and-updated,
  inserted-new, left-missing, skipped-operator, confirmed-conflict, failed.
- Count OpenAI calls and tokens against the estimate.
- **Watch the new-event yield in truncated mode.** If re-extracting a capped
  post yields no events past the old cap, the cap was not the constraint and the
  mode should stop — the assumption behind the whole exercise was wrong.
- A failed post must not abort the run; log, count, continue, stay resumable.

## Test Plan
Feature file: `tests/bdd/enrichment/history-repair-reextraction.feature`

Scenarios:
- Re-type a greeting as other without touching its siblings.
- Keep a genuine announcement typed as an event.
- Recover the events a post lost past the old cap.
- Keep the stored identity of an event that already existed.
- Leave an event alone when re-extraction no longer returns it.
- Never re-type a row an operator confirmed, and report the disagreement.
- Never overwrite a field an operator edited.
- Estimate cost and write nothing without `--apply`.
- Stop at the configured maximum number of posts.
- Refuse to run when the prompt change is not deployed.
- Resume after an interrupted run without redoing completed posts.

Pytest unit tests:
- Identity pinning: a matched event keeps its stored `source_event_key` even
  when the model returns a re-phrased title **and** a different `date_text` —
  this is the regression test for the hazard in §C and must fail loudly if
  anyone "simplifies" the matching.
- A new event past the old maximum index gets a fresh key that equals what a
  clean extraction would compute.
- Absence never deletes.
- The post-type pre-filter, pinned on `'31 Anos'` (must match) and on a real
  announcement with an empty lineup (must not match) — the filter's most
  dangerous false positive.
- Cost estimation arithmetic against known token counts.
- Resumability: a run interrupted mid-set completes exactly the remainder.

Manual or integration checks:
- Dry-run against a restored snapshot; attach the cost estimate and the target
  set size to the PR.
- First `--apply` limited to a handful of posts, results inspected by hand
  before any wider run.
- **No Apify call is permissible in either mode.** Assert it in the test suite,
  not just in review.

## Acceptance Criteria
- `'31 Anos'` is re-typed `other`; a real announcement in the same batch is not.
- A capped post yields its previously-discarded events, and its existing events
  keep their stored keys.
- No operator-edited or confirmed row is silently changed.
- No event is ever deleted because re-extraction stopped returning it.
- Cost is estimated before spending and reconciled against actual afterwards.
- No Apify call is made.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
