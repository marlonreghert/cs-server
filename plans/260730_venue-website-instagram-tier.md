# A Free Tier That Reads The Venue's Own Website

## Branch
feature/venue-website-instagram-tier

## Goal
Find the Instagram handle a venue publishes on its own website. This is the
largest remaining free source of handles, and nothing has ever looked at it.

## Non-goals
- Restoring probe reachability, and the LLM judge.
- Crawling beyond the page the venue's website URL resolves to. One page, one
  request per venue; no site spidering.

## Evidence

Measured against the top-250 Recife venues, the cohort this work is judged on:

    still missing a handle          207
      have a non-instagram website  129   <- reachable by this tier
      have no website at all         76   <- unreachable, by any free source
    instagram link found on the site 55   (43% of those with a site)

The matches are strong, because a venue's own site links its own account:

    Ponte Nova              -> @ponte_nova              1.00
    Buca Trattoria          -> @bucatrattoria           0.96
    Don Francesco Trattoria -> @donfrancescotrattoria   0.95
    Pizzaria Atlantico      -> @pizzariaatlantico       0.76

Nothing else available comes close. The paid Apify search produced candidates for
2 of the 11 top venues tried and none that could be accepted; 6 of the highest-
review venues in Recife currently yield NO candidate from any tier at all.

### Why provenance is 0.40

A footer link is not always the venue's. Real cases seen: the agency that built
the site (`@marketingpararestaurante`), the franchise (`@smartfit`), the mall,
an unrelated partner. So the tier is deliberately weaker than the venue's own
Google listing, and name similarity decides. Fitted against measured true
positives and measured noise:

    provenance   similarity needed   true kept   noise admitted
      0.30           0.88             8/14        none
      0.35           0.75            14/14        none
      0.40           0.62            14/14        none
      0.55           0.25            14/14        three, including the agency

0.40 sits between the worst noise (0.348) and the weakest true match (0.76) with
margin on both sides. Do not raise it without re-measuring: at 0.55 the agency
that builds restaurant websites becomes the Instagram account of every
restaurant it built.

## Current Behavior
`SOURCE_ORDER` is Google listing, archived Maps payload, paid search. The venue's
own website is fetched by nobody. A venue whose Google listing points at
`restaurante.com.br` yields no candidate even when that page links Instagram in
its footer.

## Desired Behavior
1. Read the Instagram profile URL a venue links from its own website.
2. Run after both existing free sources and BEFORE the paid search — it costs a
   single HTTP request and the paid tier bills.
3. Reject link shims and non-profile paths exactly as the other free tiers do.
4. Skip the fetch entirely when the website IS an Instagram URL — tier 1 owns it.
5. Never fail a venue: a timeout, a dead domain, a redirect loop, a huge page or
   a non-HTML response degrades to "no candidate" and the run continues.
6. Be togglable per run like every other tier.

## Implementation Approach

The cascade already turns a website into a handle: for a free source it calls
`reader.website_for(...)` and hands the result to `extract_handle`. So this tier
is a new reader that returns an INSTAGRAM url rather than the venue's own — the
scoring, rejection and persistence paths are reached unchanged.

The reader reads the stored `website_uri`, fetches that one page with a browser
user agent, and returns the first Instagram profile link that is not a shim or a
non-profile path.

Bounded by construction: one request, a timeout, a byte cap on the response, and
redirects followed only to a fixed depth. A venue website is arbitrary
third-party content — the fetch must not be able to hang a 1,400-venue run.

## Data, Config, And API Impact
- Persistence, API, migrations: none. `source` records `venue_website`.
- New per-run toggle `tier_venue_website_enabled`, default on, surfaced in the
  admin options modal like the existing tier toggles.
- New settings for the timeout and byte cap.

## Error Handling And Observability
Every failure mode degrades to "no candidate", and the existing
`instagram_cascade_tier_attempts_total{source}` and
`instagram_handle_rejected_total{reason}` cover the new tier through its source
label. Fetch failures are counted by reason so a systematically unreachable set
of sites is visible rather than silent.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-website-instagram-tier.feature`

Scenarios:
- Find the handle a venue links from its own website.
- Prefer the venue's Google listing when that already carries the handle.
- Skip the fetch when the listed website is itself an Instagram URL.
- Reject a link shim found on the page.
- Reject a post or reel link found on the page.
- Yield nothing when the page links no Instagram at all.
- Yield nothing when the site times out, and let the run continue.
- Yield nothing when the site returns a huge or non-HTML body.
- Run before the paid search, and skip the paid search once it succeeds.
- Reject a footer link whose name does not match the venue.
- Accept a footer link whose name matches the venue.
- Honour the per-run toggle.

Pytest unit tests:
- `tests/test_venue_website_source.py` — extraction from real footer markup;
  shim and non-profile rejection; the byte cap and timeout; that a failure
  returns None rather than raising.
- `tests/test_venue_website_tier_scoring.py` — the fitted weight: every measured
  true positive is accepted and every measured noise case is rejected, using the
  real similarity values recorded in this plan.

Manual or integration checks:
- Re-run the scoped top-250 Recife job and confirm the tier converts venues the
  other tiers could not reach.

## Acceptance Criteria
- A venue whose site links its Instagram gets that handle.
- The agency, franchise and mall cases stay rejected.
- The paid search is not called when this tier succeeds.
- A hostile or dead website cannot fail or hang a run.
- `make test-bdd` and `make test-unit` pass; `@wip` removed.

## Open Questions
- None.
