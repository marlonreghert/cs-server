# Find The Handle Through Google, And Never Trust It On Its Own

## Branch
feature/google-search-instagram-tier

## Goal
Reach the venues no existing tier can see — the ones with no website at all —
without ever attaching a handle that belongs to somebody else.

## Non-goals
- Replacing the Instagram user search (measure first, retire later).
- Restoring the existence probe.

## Evidence

Of the 290 Recife venues still missing a handle, **278 (96%) produced no
candidate from any tier**. Not rejected — never generated:

    no website at all                    191
    has a website, no instagram link       97
    website IS instagram (anomaly)          2

With no website there is nothing for tiers 1-3 to read, and Instagram's own user
search does not surface small Brazilian venues (measured: "Bar do Cuscuz Recife"
returns zero).

Google does, because it indexes the Instagram profile page itself. A five-venue
probe against `apify/google-search-scraper`, all from the no-website cohort:

    Gildo Lanches                     -> @gildolanchespe
    Jardim do Baobá                   -> @jardim_baoba
    Entre Amigos Praia                -> @entreamigos.praia
    Pátio de São Pedro                -> @saopedrorestaurante
    Basílica e Mosteiro de São Bento  -> @mosteirosaobentolinda

Five for five on a cohort that was previously at zero. It also runs on Apify
credits already in use, so it needs no new provider.

**And the last two are exactly the danger.** Pátio de São Pedro is a public
square, not a restaurant; `@mosteirosaobentolinda` is in Olinda. Both are
plausible and neither is obviously correct. A search result is a GUESS, and the
whole design of this tier follows from that.

## Current Behavior
A venue with no website falls through every tier and is written `not_found`, its
handle discarded. 191 Recife venues and roughly 530 more catalogue-wide sit in
that state.

## Desired Behavior
1. Query Google for the venue's name, its neighbourhood, and "instagram".
2. Read the first Instagram PROFILE link out of the results, rejecting shims and
   non-profile paths exactly as every other tier does.
3. **A candidate from this tier can never be accepted on provenance and name
   similarity alone.** It must be confirmed by the judge.
4. Run after every free tier, so a handle already discoverable for nothing is
   never paid for.
5. Never fail a venue: a failed actor run, a timeout, an empty result set each
   degrade to "no candidate".
6. Be togglable per run, like every other tier.

## Implementation Approach

A source that returns an Instagram URL, so scoring, rejection and persistence are
reached unchanged — the same shape as the venue-website tier.

**The safety property is arithmetic, not judgement.** Provenance is 0.20. The
accept bar in production is 0.65 (0.8 minus an existence bonus that cannot be
collected while Instagram blocks the datacenter IP). Name similarity contributes
at most 0.40. So the ceiling for this tier is

    0.20 + 0.40 x 1.0 = 0.60  <  0.65

A Google-search candidate therefore **cannot** reach the bar by itself, at any
name similarity, however perfect. The only path to acceptance is the judge, which
adjudicates from the 0.30 floor up to the bar and caps a text-only verdict at
0.80. That is the guarantee the operator asked for, and it holds by construction
rather than by choosing a threshold well. It is asserted directly.

The judge is therefore a hard dependency for this tier's yield: with the judge
off, this tier can find candidates but will never accept one. That is the correct
failure mode — silence rather than a wrong handle.

## Data, Config, And API Impact
- Persistence, API, migrations: none. `source` records `google_search`.
- New per-run toggle `tier_google_search_enabled`.
- New settings: the actor id, results per query, and a timeout.
- Cost: one actor run per venue. Measured before any wide run, not assumed.

## Error Handling And Observability
Every failure degrades to "no candidate". The existing
`instagram_cascade_tier_attempts_total{source}` and
`instagram_cascade_results_total{source,result}` cover the tier through its
source label, so its yield and its rejection rate are visible per source from the
first run.

## Test Plan
Feature file: `tests/bdd/enrichment/google-search-instagram-tier.feature`

Scenarios:
- Find the handle Google surfaces for a venue with no website.
- Reject a shim or non-profile link in the results.
- Yield nothing when the search returns no Instagram link.
- Survive a failed actor run, and let the run continue.
- Never accept a Google-search candidate without the judge.
- Accept one the judge confirms.
- Reject one the judge rejects.
- Run only after the free tiers have come up empty.
- Honour the per-run toggle.

Pytest unit tests:
- `tests/test_google_search_source.py` — extraction from a real result payload;
  shim and non-profile rejection; failure modes; the query includes the venue
  name and its neighbourhood.
- `tests/test_google_search_cannot_self_accept.py` — the load-bearing invariant,
  proved across the full similarity range 0.0 to 1.0: no value clears the bar
  without a judge verdict.

Manual or integration checks:
- Run against ~30 venues, hand-label the results, and record the measured
  precision in this plan before any wide run.

## Acceptance Criteria
- A venue with no website can resolve through Google.
- No Google-search candidate is accepted without a judge verdict, at any
  similarity.
- Shims and non-profile paths are rejected.
- A failed actor run cannot fail a venue.
- `make test-bdd` and `make test-unit` pass; `@wip` removed.

## Open Questions
- None blocking. Precision and cost per venue are to be measured on the ~30
  venue sample before a wide run, and recorded here.
