# Match Venue Names The Way Venues Actually Name Themselves

## Branch
fix/venue-name-matching

## Goal
Stop rejecting correct handles because the venue's Google name carries a
category prefix, a neighbourhood suffix, or an accent the handle drops.

## Non-goals
- The judge, the probe egress, and any change to provenance weights.

## Evidence

From the live run over the top-250 Recife venues, with the venue-website tier
enabled. Each of these FOUND the right link and then rejected it on the name
check alone:

| venue | handle found | similarity | confidence | bar | outcome |
|---|---|---|---|---|---|
| Pizzaria Atlântico Graças | `@pizzariaatlantico` | 0.762 | 0.705 | 0.65 | accepted |
| Restaurante Parraxaxá Boa Viagem | `@parraxaxaoficial` | 0.439 | 0.576 | 0.65 | REJECTED |
| Casa da Cultura de Pernambuco | `@casadaculturape` | 0.489 | 0.596 | 0.65 | REJECTED |

Three failure modes, all of them ordinary:

- **Category prefix** — "Restaurante", "Pizzaria", "Bar" are in the Google name
  and never in the handle.
- **Location suffix** — "Boa Viagem", "RioMar Recife", "Marco Zero". Measured:
  `Camarada Camarão RioMar Recife` vs `@camaradacamarao` scores 0.62;
  `Bode do Nô Boa Viagem - Restaurante` vs `@bodedono` scores 0.37.
- **Folding** — accents and punctuation the handle cannot contain.

Character-ratio similarity punishes all three, and it punishes the LONGEST venue
names hardest — which are exactly the prominent venues this work targets.

## Current Behavior
`name_similarity` lowercases both sides and takes a `difflib` ratio. A venue
whose distinctive name sits inside the handle, surrounded by words the handle
omits, scores low and is rejected.

## Desired Behavior
1. Compare with accents and punctuation folded away.
2. Compare the venue's DISTINCTIVE core — its name minus category and location
   words — as well as its full name, and take the better of the two.
3. Treat one string containing the other as strong evidence.
4. Require a minimum core length before containment counts, so a short generic
   core cannot match an unrelated longer handle.
5. Never score below what the current comparison gives — this only ever adds
   evidence.
6. Keep every measured noise case rejected.

## Implementation Approach

One function, three comparisons, take the maximum: the folded full name, the
folded distinctive core, and a containment check that scores 0.95 when either
folded string contains the other and the core is at least five characters.

The stop-word list covers Brazilian venue categories and Recife locality names.
Recorded honestly: it is a heuristic list fitted to this catalogue, not a general
solution, and it will need extending when the product leaves Recife.

Recorded tradeoff: containment is deliberately generous, so a venue whose core is
a common word could match an unrelated business using that word. The minimum
length blunts this, and provenance bounds the damage — the candidate still had to
appear on the venue's own website or its own Google listing.

## Data, Config, And API Impact
None. Persistence, API, migrations and config are untouched.

## Error Handling And Observability
No new failure modes; the function is pure. The recorded
`discovery.signals.name_similarity` continues to explain each decision.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-name-matching.feature`

Scenarios:
- Match a venue whose name carries a category prefix.
- Match a venue whose name carries a neighbourhood suffix.
- Match a venue whose handle drops the accents.
- Match a venue whose handle appends a suffix like "oficial".
- Reject the web agency that built the venue's site.
- Reject an unrelated business found on the page.
- Never score a pair lower than the plain comparison does.

Pytest unit tests:
- `tests/test_venue_name_matching.py` — every pair measured in production, true
  and noise, asserted through the real scoring at the real provenance weight;
  the monotonicity property; the minimum core length; and that folding alone
  never manufactures a match.

Manual or integration checks:
- Re-run the scoped top-250 Recife job; Parraxaxá and Casa da Cultura must
  persist a handle.

## Acceptance Criteria
- The three production cases above all resolve.
- All four measured noise cases stay rejected.
- No pair scores lower than it does today.
- `make test-bdd` and `make test-unit` pass; `@wip` removed.

## Open Questions
- None.
