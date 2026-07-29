# Instagram Candidate Loss: Apify Payload Drift + Ignored Venue Subset

## Branch
fix/instagram-candidate-loss

## Goal
Stop silently discarding every Instagram candidate that has an external link,
and make a scoped run actually scoped.

Two independent defects currently make the Instagram pipeline unable to find
handles, and make the one control an operator has over its cost inert.

## Non-goals
- **The `website_uri` backfill.** Persisting the field shipped separately
  (#120); populating it for the existing 1537 venues costs a Google Places call
  each and is a deliberate, separate decision.
- **Changing the cascade's tiers, confidence model or judge.** They are
  downstream of this and were never receiving candidates.
- **The `instagram_posts` job.**

## Evidence

### Defect 1 — every linked profile is dropped
Apify's `instagram-search-scraper` changed shape: `externalUrls[]` elements are
now OBJECTS, e.g.

```
{'title': '', 'lynx_url': 'https://l.instagram.com/?u=...', 'link_type': 'external'}
```

`app/api/apify_instagram_client.py:96` takes `external_urls[0]` verbatim and
passes it to `InstagramProfile.external_url`, declared `Optional[str]`
(`app/models/instagram.py`). Pydantic rejects the dict, the `except` at :110
logs a WARNING and `continue`s — **discarding the entire profile**, not just the
link.

Observed live on the box:

```
[ApifyInstagram] Failed to parse search result: 1 validation error for
InstagramProfile / external_url / Input should be a valid string
```

Control run through the real client: query `"nike"` returned **1** surviving
candidate (`usnikefootball`) out of many — only profiles WITHOUT a link parsed.
Query `"Bar do Cuscuz Recife"` returned **0**, because every match had a link.
That is why a cascade run over 312 venues produced no handles, and why only 139
of 451 rows ever got one from the older job.

The failure is invisible by construction: a per-profile WARNING, nothing
counted, and an empty list returned — indistinguishable from "no results".

### Defect 2 — `venue_ids` is collected, sent, and ignored
`InstagramCascadeService.run()` iterates `list_servable_venue_ids()` and never
consults `config["venue_ids"]`, though the job's `default_config` advertises the
field and the admin modal renders and posts it. A run an operator scoped to two
venues silently becomes a full-catalog run — with `force_refresh: true`, ~451
paid searches instead of 2.

## Current Behavior
Apify is called, returns matches, and the client throws away every profile that
carries an external link. Callers see an empty candidate list. Separately, any
venue subset chosen in the admin modal is discarded and the whole servable
catalog runs.

## Desired Behavior
1. A profile whose `externalUrls` entries are objects parses successfully, with
   the URL extracted from the object.
2. A profile whose entries are plain strings still parses — the old shape must
   keep working.
3. A profile that cannot be parsed at all is **counted**, not only logged, so
   total candidate loss can never again look like "no results".
4. A malformed link never discards an otherwise valid profile: the link is
   dropped, the profile survives.
5. `run()` restricts to `venue_ids` when supplied, and reports which requested
   ids were unknown.
6. An empty/absent `venue_ids` still means the whole servable catalog.

## Implementation Approach

### A. Tolerate both link shapes
A small coercion at the parse boundary: string → itself; object → its URL field
(`lynx_url`, else `url`); anything else → None. Applied before constructing
`InstagramProfile`, so the model keeps its simple `Optional[str]` contract and
the tolerance lives at the edge where foreign data arrives.

`l.instagram.com` wrappers are stored verbatim here — the cascade's extractor
already rejects them as non-profile URLs.

### B. Count what is discarded
`instagram_search_candidates_dropped_total{reason}` incremented wherever a
candidate is skipped: `parse_error`, `no_username`, `error_item`. A silent drop
is what made a total outage look like an empty search; the metric is the fix for
the *class* of bug, not just this instance.

### C. Honour the venue subset
`run()` parses `config["venue_ids"]` with the same `parse_venue_ids` helper the
cascade already uses, intersects with the servable catalog, and reports unknown
ids in the summary rather than failing. Empty means everything, unchanged.

## Data, Config, And API Impact
- **API / persistence / migration:** none.
- **Settings:** none.
- **Behavior change worth flagging:** runs scoped by `venue_ids` will now
  actually be scoped — i.e. dramatically cheaper than the current accidental
  full-catalog behavior.

## Error Handling And Observability
Parsing stays best-effort: one unparseable candidate never fails a search.
The difference is that it is now counted.

New metric: `instagram_search_candidates_dropped_total{reason}`.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-candidate-loss.feature`

Scenarios:
- Keep a candidate whose external link is an object (the real Apify shape).
- Keep a candidate whose external link is a plain string (the old shape).
- Keep a candidate with no external link at all.
- Keep the profile when only its link is unusable.
- Count a candidate that cannot be parsed.
- Restrict a run to the venue ids the operator supplied.
- Report unknown venue ids without failing the run.
- Run the whole catalog when no venue ids are given.

Pytest unit tests:
- `tests/test_apify_profile_parsing.py` — the exact object payload observed in
  production parses and yields the URL; the string shape still parses; a dict
  with no recognisable URL key yields a profile with `external_url=None` rather
  than being dropped; drops are counted by reason.
- `tests/test_instagram_cascade_run_scope.py` — `venue_ids` restricts the run;
  unknown ids are reported not fatal; empty means the full catalog; **the
  cascade is not invoked for venues outside the subset** (the cost guarantee).

Manual or integration checks:
- Against the real actor, confirm a query that previously returned 0 now returns
  candidates, and that a scoped run touches only the named venues.

## Acceptance Criteria
- A search whose results carry object-shaped external links returns those
  candidates instead of dropping them.
- The old string shape still works.
- Dropped candidates are counted with a reason.
- A run scoped to N venue ids considers exactly those that exist, and no others.
- `make test-bdd` and `make test-unit` pass; the `@wip` tag is removed.

## Open Questions
- None. Both defects are reproduced against production data.
