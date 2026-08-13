# Handle Attribution Hardening — an @handle is not a venue name

## Branch
fix/handle-attribution-hardening

## Goal
When an event names a specific Instagram handle we do not carry, say so. Never
fuzzy-match the handle's own text against a different venue's name and link it
automatically.

## Non-goals
- **Re-litigating the ladder's order.** `260812_event-attribution-and-dates.md`
  §A shipped and is verified working in production (see Evidence). This plan
  fixes what happens *after* rung 1 misses.
- **Adding venues to the catalog.** `venue_not_in_catalog` turns that into a
  ranked backlog; filling it is a separate, largely non-engineering decision.
- **Repairing existing rows.** `260812_backfill-misattributed-links.md` — which
  **must not run until this plan lands**, see §Evidence.

## Evidence

### Live verification of §A — the core fix works
An incremental promoter crawl of `oquetemhojeemnatal` on 2026-08-13 produced 21
events that linked to **seven different venues**, each from its own
`location_text`. Before §A, all 20 events of a roundup post inherited one
caption mention. Link methods on that run:

```
handle_mention 5    name_match 4    (unlinked) 12
caption_handle_mention: 0
```

The caption rung never won once — per-event evidence took every decision. That
part is correct and this plan does not touch it.

### Three of nine links are confidently wrong, from rung 4
```
@mahalilacafe  -> Maria Café       (x2)   linked_by=name_match, auto, review_reason NULL
@espaco.muta   -> Espaço Tucano           linked_by=name_match, auto, review_reason NULL
```

"Mahalila Café" and "Espaço Muta" are not in the catalog. Rung 1 correctly finds
no known venue. Rung 4 then takes the **raw `location_text` string** — which is
literally `@mahalilacafe` — and name-matches it against every venue, scoring
above the confidence floor on the shared token *café* plus the *ma* prefix.
`@espaco.muta` matches "Espaço Tucano" on *espaço*.

These land as `RESOLUTION_AUTO` with **no review reason**, so an operator never
sees them. A wrong link that presents itself as confident is worse than no link:
it sends a user to the wrong venue and it looks settled.

**A handle is not a name.** `@mahalilacafe` is an exact identifier that either
resolves or does not. Treating its characters as a fuzzy venue name is a
category error, and it is the direct cause of all three bad links.

### `venue_not_in_catalog` never fires
All 12 unlinked rows from the same run carry `review_reason = unresolved_venue`,
though every one names a specific unknown handle — `@casadaribeira`,
`@torresmofest`, `@sescrn`, `@mccufrn` and so on. That is exactly the case
`260812` added `METHOD_VENUE_NOT_IN_CATALOG` for.

The constant exists (`event_venue_resolution.py`, and
`REVIEW_REASON_VENUE_NOT_IN_CATALOG` in `event_reconciliation`) and the fallback
at the end of `resolve_event_venue` looks correct on a reading:
`_known_venue_mentions` returns `unrecognized_event_handle`, and rung 5's
fallback returns `METHOD_VENUE_NOT_IN_CATALOG` when it is set.

**It nevertheless does not reach the stored row.** Diagnose this properly before
changing anything — three hypotheses worth eliminating in order, and *do not
assume the first is right*:

1. Rung 4 resolves above the floor first (this is provably what happens for the
   three bad links above), so the fallback is never reached. §A's fix may
   resolve much of this on its own — **re-measure before writing more code**.
2. The promoter path reaches the resolver through a different entry point that
   does not map `resolution.method` onto `review_reason`.
3. `event_reconciliation` overwrites the reason downstream.

### This blocks the backfill
`260812_backfill-misattributed-links.md` re-resolves 487 rows through this same
ladder. Its own evidence says the 492 usable `location_text` values name **159
distinct handles, only about a dozen of which map to a venue we carry** — so the
overwhelming majority of backfilled rows take exactly the path that produces
these false positives. Running it today would replace 487 caption-derived wrong
links with a fresh crop of fuzzy-derived wrong links, and the new ones would be
`auto` with no review reason.

## Current Behavior
An unresolvable @handle in `location_text` falls through to fuzzy name matching
on the handle's own characters, producing confident automatic links to unrelated
venues; and the `venue_not_in_catalog` reason that should describe these rows
never reaches the database.

## Desired Behavior
1. Never name-match against text that is an @handle.
2. Record `venue_not_in_catalog` when an event names a handle we do not carry.
3. Keep §A's per-event precedence exactly as it is.
4. Leave genuine name matching — on real place names — working.

## Implementation Approach

One commit per section, one branch, one PR.

### A. Strip handles before name matching, and short-circuit when nothing remains
Rung 4 must never see an `@`-token. Remove every `@handle` from `location_text`
before name matching; if what remains is empty or has no alphabetic content,
rung 4 **does not run at all** for that text.

`@mahalilacafe` leaves nothing → no name match → falls through to the
`venue_not_in_catalog` fallback, which is the correct answer.
`Conchittas Bar — Rua da Imperatriz, 218` is untouched and still matches.
`@obarpraia Ponta Negra` keeps `Ponta Negra` — a real place name and legitimate
matching input.

**Do not solve this by raising the confidence floor.** The floor is a global
knob; these matches score highly *because* the fuzzy comparison is being fed the
wrong kind of string. Raising it would suppress genuine name matches elsewhere
and leave the category error in place.

Rung 3 (neighbourhood/address) is bounded to a caller-supplied candidate set and
is not affected — but assert that with a test rather than assuming it.

### B. Make `venue_not_in_catalog` actually reach the row
Follow the Evidence's three hypotheses **in order**, and re-measure after §A
before writing code for §B: §A alone may fix the majority, since the rows that
were being captured by rung 4 will now fall through to the correct fallback.

Whatever the cause, the acceptance test is end-to-end and not a unit assertion
on the resolver's return value: extract an event whose `location_text` names an
unknown handle, and assert the **stored** `review_reason` is
`venue_not_in_catalog`. The defect this plan is fixing is precisely that the
resolver's answer was right and the stored row's was not.

Keep `unresolved_venue` for its own meaning — the event named nothing usable at
all. The two must stay distinguishable; that distinction is the whole point.

### C. Never auto-link on evidence an operator cannot check
A rung-4 name match derived from text that contained an @handle must not produce
`RESOLUTION_AUTO`. If §A leaves genuine name text and it matches, that is a
normal name match and behaves as today.

State the general rule in the module docstring, because it is the lesson these
three rows taught: **an automatic link needs evidence of the venue's identity,
not a coincidence of characters.**

## Data, Config, And API Impact
- **Migration** — none. No schema change; `venue_not_in_catalog` is an existing
  constant and `review_reason` is free text.
- **Config** — none. Explicitly **not** a confidence-floor change (§A).
- **API** — none. `review_reason` gains no new value; it gains the value it was
  always supposed to carry.
- **Rollback:** revert. Rows written under the current behaviour keep their
  values until the backfill runs.

## Error Handling And Observability
- Count rung-4 invocations that were **skipped** because the text was
  handle-only. A high rate is the expected steady state for promoter roundups
  and confirms the fix is doing work.
- Count `venue_not_in_catalog` resolutions, and log the handle. This is the
  venue-acquisition backlog: the most frequently named unknown handles are the
  venues most worth adding.
- **Watch the auto-link rate.** It should fall, and that is success, not
  regression — the links being removed were wrong. Say so in the PR so nobody
  reads the drop as a fault.

## Test Plan
Feature file: `tests/bdd/enrichment/handle-attribution-hardening.feature`

Scenarios:
- Refuse to link an event whose location text names an unknown handle.
- Record `venue_not_in_catalog` on that event.
- Never link "@mahalilacafe" to "Maria Café".
- Never link "@espaco.muta" to "Espaço Tucano".
- Keep linking an event whose location text is a real venue name.
- Keep linking an event whose location text is a known handle.
- Match on the place name left after a handle is stripped.
- Keep `unresolved_venue` for an event that names nothing at all.
- Keep the per-event over caption precedence unchanged.
- Keep the neighbourhood rung working for a multi-branch account.

Pytest unit tests:
- Handle stripping: handle-only; handle plus a place name; a name with no
  handle; an empty string; a handle containing dots and underscores
  (`@espaco.muta`, `@bar54_`, `@letra_a_` are all real production values).
- Rung 4 is not invoked at all when nothing alphabetic remains.
- The three production false positives, pinned by **name**: assert
  `@mahalilacafe` does not resolve to "Maria Café" and `@espaco.muta` does not
  resolve to "Espaço Tucano". These are the regression tests; anything that
  loosens the rule must break one of them first.
- The six production links that were **correct** on the 2026-08-13 run still
  resolve to the same venues — `@seuchicobotequim`, `@semprerockbar`,
  `@tavernapubnatal`, `@obarpontanegra`, `@bar54_` — so this plan cannot fix the
  false positives by breaking the true ones.
- End-to-end: stored `review_reason` is `venue_not_in_catalog`, asserted on a
  persisted row, not on a resolver return value.

**Assertions must name the venue, not count the links.** A count-based
assertion already stayed green here against a deliberately reintroduced
wrong-handle bug.

Manual or integration checks:
- Re-run the same incremental promoter crawl after deploy and confirm the three
  bad links do not reappear and the 12 unlinked rows now read
  `venue_not_in_catalog`. One target, once — it costs Apify results.

## Acceptance Criteria
- No event links to a venue on the strength of an @handle's characters.
- An event naming an unknown handle stores `review_reason =
  venue_not_in_catalog`.
- An event naming nothing usable still stores `unresolved_venue`.
- The six correct links from the 2026-08-13 run still resolve identically.
- §A's per-event precedence is provably unchanged.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
