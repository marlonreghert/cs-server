# The Cascade Must Score Without A Working Probe

## Branch
fix/cascade-scores-without-a-probe

## Goal
Let a candidate be accepted on the evidence that is actually available, instead
of failing every venue for want of a signal production cannot collect.

## Non-goals
- Restoring probe reachability (needs an egress change; separate decision).
- The website-scrape tier.
- Enabling the LLM judge.

## Evidence

A scoped run over the top-250 Recife venues, after the probe fix shipped:

| venue | source | confidence | status |
|---|---|---|---|
| Bar do Cuscuz | google_website | 0.750 | low_confidence |
| Tatu Bola | apify_search | 0.488 | not_found |
| 9 others | none | 0.0 | not_found |

Bar do Cuscuz resolved `@bardocuscuzrecife` from the venue's OWN Google listing
and was still not accepted.

Two independent causes.

### 1. The bar assumes a signal production cannot collect
Deployed config sets `instagram_auto_accept_threshold: 0.8` (the code default is
0.75). Tier 1 scores `0.75 provenance + 0.15 existence + 0.40 x similarity`.
Instagram blocks the datacenter IP, so the existence bonus is permanently 0 and
tier 1's ceiling is 0.75 — below 0.8 forever. The strongest free source in the
system cannot accept a single venue.

### 2. Similarity is measured against nothing
`name_similarity` compares the venue name to the probe's display name. A blocked
probe has no display name, so the comparison scores 0.0 — while the handle
itself sits right there, unused. Measured on real pairs:

    Bar do Cuscuz        vs @bardocuscuzrecife  -> 0.733
    Entre Amigos O Bode  vs @entreamigosobode   -> 0.914
    Teatro Jorge Amado   vs @teatrojorgeamado   -> 0.941

Recorded so it is not rediscovered: with the existence component unavailable,
provenance alone (0.75) is already 94% of the 0.8 bar, so similarity barely
discriminates for tier 1. That is acceptable ONLY because tier 1's candidate is
the Instagram URL the venue itself publishes on its own Google listing. It would
not be acceptable for a scraped source, and the weights must be revisited before
any lower-provenance tier is added.

## Current Behavior
A candidate is measured against a fixed bar that includes points for a check
that cannot run, and its name is compared against a display name that does not
exist. Tier 1 always lands on 0.750 and is recorded `low_confidence`; the paid
tier lands below 0.5 and is recorded `not_found`, discarding the handle.

## Desired Behavior
1. Compare the venue name against the candidate handle when no display name is
   available, treating handle punctuation as word separators.
2. Prefer a real display name when one exists — the handle is a fallback, not a
   replacement.
3. When existence cannot be checked, do not require the points that check would
   have contributed: the acceptance bar drops by exactly the existence bonus.
4. When existence IS checked and the profile is confirmed absent, reject as now.
5. Never raise the bar above the configured threshold.
6. Record which signals were unavailable, so a decision can be explained later.

## Implementation Approach

Two small changes in `instagram_cascade_service.py`.

`name_similarity` gains a handle fallback: display name first, else the handle
with `.` and `_` read as spaces. This adds a signal that was always derivable
and always thrown away.

Acceptance becomes relative to what could be measured. When the probe returns
neither present nor absent — unknown or blocked — the effective threshold is
`accept_threshold - EXISTENCE_BONUS`. A candidate is not penalised for a check
the platform could not perform. The configured threshold is unchanged and is
still the bar whenever the probe works.

## Data, Config, And API Impact
- Persistence, API, migrations: none.
- No config change: `instagram_auto_accept_threshold` keeps its meaning for a
  working probe.
- Behavioral: venues whose handle comes from their own Google listing will be
  accepted while the probe is blocked. That is the intent.

## Error Handling And Observability
The stored `discovery.signals` gains the effective threshold and the reason it
differed, so a past acceptance can be explained without re-running anything.

## Test Plan
Feature file: `tests/bdd/enrichment/cascade-scores-without-a-probe.feature`

Scenarios:
- Accept a handle from the venue's own Google listing when the probe is blocked.
- Accept it when the probe is merely unknown.
- Reject the handle when the probe confirms the profile is absent.
- Hold the full bar when the probe works and the profile is present.
- Compare the venue name against the handle when no display name is available.
- Prefer the display name over the handle when both exist.
- Record which signals were unavailable on the stored record.
- Keep a weak paid-search candidate below the bar even when the probe is blocked.

Pytest unit tests:
- `tests/test_cascade_scoring_without_probe.py` — the exact production numbers
  (0.75 tier-1 ceiling vs the 0.8 configured bar); the handle-similarity pairs
  measured above; that the effective bar never exceeds the configured one; and
  that a confirmed-absent profile is still rejected however high it scores.

Manual or integration checks:
- Re-run the scoped top-250 Recife job and confirm tier-1 venues now persist a
  handle with status `found`.

## Acceptance Criteria
- Bar do Cuscuz resolves and is accepted from its Google listing.
- A confirmed-absent profile is still rejected.
- With a working probe, the configured threshold is unchanged.
- A paid-search candidate with no verification stays below the bar.
- `make test-bdd` and `make test-unit` pass; `@wip` removed.

## Open Questions
- None.
