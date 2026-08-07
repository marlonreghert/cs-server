# Date Resolution Correctness — a weekday must corroborate, never replace

## Branch
fix/date-resolution-correctness

## Goal
Stop the resolver silently inventing a date from a weekday when the flyer stated
an explicit one it could not parse, and teach it the Portuguese month
abbreviations that Brazilian flyers actually use. Also make a totally-failed
extraction visible to an operator instead of invisible.

## Non-goals
- **Changing the extraction prompt or the model.** The RCA proves the model is
  blameless here — see Evidence. Touching the prompt would be fixing the wrong
  component.
- **New event fields.** `ticket_info` / `attractions` are
  `260807_event-ticket-info-and-attractions.md`.
- **The console's presentation of review reasons.** That is
  `vibes_bot/plans/260807_review-queue-triage.md`.
- **The `05 e 06/09` date-range gap.** Real, same family, lower severity —
  called out in §D as a deliberate follow-up rather than smuggled in here.

## Evidence

**The model did its job.** The real extractor was called three times against the
real archived caption for shortcode `Dbt0M1ooIPp`
(`🎫 TICKETS | 📍 SECRET CLUB • Club Metrópole | 📅 Sábado • 05/SET`) and
deterministically returned:

```
date_text: "Sábado • 05/SET"
```

That is exactly what `openai_event_extraction_client.py` asks for — "Copy date
and time EXACTLY AS WRITTEN". Explicit day, explicit month, corroborating
weekday, nothing lost or invented. **The resolver is entirely at fault.**

**Defect 1 — pt-BR month abbreviations are not parsed.** `_MONTHS`
(`app/services/event_date_resolver.py:53-57`) contains only full names
(`setembro`, `agosto`, …), and `_TEXTUAL_DATE_RE` is built from those keys.
`_NUMERIC_DATE_RE` needs digits on both sides of the separator. So `05/SET`
matches neither. Measured:

| input | result |
|---|---|
| `05/SET`, `05/set`, `5 SET`, `SET 05`, `08/Ago` | `None` → missing_date |
| `05/09`, `05.09`, `05-09`, `05/09/26` | correct |
| `5 de setembro` | correct (full name) |
| `HOJE`, `AMANHÃ`, `toda quinta` | correct |

**Defect 2 — the weekday fallback silently overrides an explicit date, and this
is the dangerous one.** `_parse_date_text` tries hoje/amanhã → numeric →
textual, then falls through to `:160-163`:

```python
m = _WEEKDAY_RE.search(text)
if m:
    return _next_weekday_on_or_after(anchor_date, _WEEKDAYS[m.group(0).lower()])
```

The weekday wins unconditionally, with no check for a day-of-month numeral about
to be discarded. `resolved_date` is non-None, so `needs_review` is **False** and
the caller cannot tell a confident resolution from one that threw information
away. Measured:

| input | result | flagged? |
|---|---|---|
| `Sábado • 05/SET` | 2026-08-08 (真 date: 5 Sep) | **no** |
| `sexta 15` | next Friday; the `15` is dropped | **no** |
| `Domingo • 20/DEZ` from a 2026-01-05 anchor | 2026-01-11 — a **339-day** error | **no** |

The error is bounded by days-to-next-weekday (0–6), **so the further out the real
event, the worse the miss**. This is precisely what the module's own docstring
and `260804_instagram-event-extraction.md` were written to prevent: "a guessed
date is worse than a missing one — an operator will scan a queue of blanks, but
will not audit a field that looks answered." The blank is the safe failure; this
is the unsafe one, and it is live.

**Defect 3 — a failed extraction is invisible, asymmetrically.**
`list_events_awaiting_decision` (`app/dao/rds_venue_store.py:599-604`) matches
`status='pending_review'` or a promoter event with a NULL location. A
**venue-post** event with `status='extraction_failed'` matches neither and never
reaches the queue. A **promoter-post** one does — but only incidentally, by
slipping through clause 2, not by design. `tests/test_review_queue_completeness.py`
enumerates `pending_review/confirmed/rejected/superseded` for both kinds and
never `extraction_failed`, so the hole is untested as well as unfixed.

A total extraction failure is lost signal on a post we already paid to archive
and classify. It deserves an operator's attention more than a clean unconfirmed
event does, not less.

## Current Behavior
An abbreviated month falls through to the weekday branch, which returns the next
matching weekday and reports no need for review. A venue-post extraction failure
never appears in the review queue.

## Desired Behavior
1. Parse pt-BR month abbreviations in both orders a flyer uses (`05/SET`,
   `SET 05`), accent- and case-insensitively.
2. Use a weekday **only to corroborate**. The weekday-only fallback fires only
   when the text contains no day-of-month numeral at all.
3. When a day-of-month numeral is present but cannot be paired with a
   recognisable month, resolve to **no date** and flag it — prefer the visible
   blank over the plausible guess.
4. When an explicit date and a stated weekday disagree, resolve from the
   explicit date and flag the disagreement, so a flyer typo surfaces instead of
   being silently trusted either way.
5. Keep a bare weekday with no competing numeral resolving as it does today.
6. Keep the recurrence path (`toda quinta`) untouched — there the weekday
   legitimately *is* the whole content.
7. Surface `extraction_failed` events in the review queue, for both source
   kinds, by design rather than by accident.

## Implementation Approach

### A. Month abbreviations
Extend `_MONTHS` with the twelve 3-letter pt-BR abbreviations and rebuild the
textual pattern to accept `DD <sep|space> ABBR` and `ABBR DD`. Match
accent-insensitively.

**Trap:** the abbreviation alternative must require an adjacent day numeral, the
way `_TEXTUAL_DATE_RE` already does. `set`, `mai` and `out` are ordinary
Portuguese words; a bare 3-letter match against free caption prose would invent
dates out of sentences.

### B. Weekday corroborates, never replaces
Before the weekday fallback, check whether the text contains a bare day-of-month
numeral that no earlier branch consumed. If one is present, return **no date**
rather than the weekday's guess.

This is the load-bearing change and it is deliberately conservative: it converts
a silent wrong answer into a visible blank, which is the direction the project's
stated principle points. It will move some currently-"resolved" events into the
review queue. That is the fix working, not a regression.

**Trap:** do not fire this guard merely because a weekday token is present.
`este sábado` with no competing numeral must keep resolving. The guard is
"numeral present AND unresolved", not "weekday present".

**Trap:** the recurrence branch runs before this and must stay that way. `toda
quinta` has no explicit date to corroborate and must not be dragged into the new
rule.

### C. Disagreement is a third outcome
When an explicit date parses **and** a weekday is stated **and** they name
different days, resolve from the explicit date — it is the more precise claim —
and set a distinct review reason (`weekday_mismatch`). A flyer that says
"Sábado 05/09" when 5 September is a Tuesday has a typo somewhere, and which
half is wrong is an operator's call, not ours.

### D. Two related gaps, named and not fixed
- `05 e 06/09` silently keeps the second date and drops the first — same
  "resolved but wrong, unflagged" family. Not fixed here; it needs its own
  decision about whether a range becomes two events.
- Bare weekday abbreviations (`sáb`, `dom`, `seg`…) are absent from `_WEEKDAYS`.
  Harmless today. Adding them would *widen* the weekday branch, so it must not
  be done in the same change that narrows it.

### E. Make a failed extraction visible
Add `extraction_failed` to the queue predicate explicitly, for both source
kinds, and extend the predicate matrix test to cover it — the hole existed
because the matrix never enumerated that status.

## Data, Config, And API Impact
- **Migration:** none.
- **API:** `review_reason` may now carry `weekday_mismatch`; the queue returns
  `extraction_failed` events. Both additive.
- **Behaviour:** some events that previously resolved to a (wrong) date will now
  resolve to no date and appear in the queue. **This is the point of the change**
  and should be stated in the PR, because it will look like a regression in the
  metrics — `event_extraction_posts_total{outcome="no_date"}` will rise.
- **Rollback:** revert. Nothing written differently, no schema moves.

## Error Handling And Observability
The resolver stays pure and never reads the wall clock.

Metrics: `event_extraction_posts_total` gains `weekday_mismatch`. Worth watching
after deploy — a sudden mass of `no_date` means the guard is firing on a form we
should be parsing, which is a signal to add a pattern, not to loosen the guard.

## Test Plan
Feature file: `tests/bdd/enrichment/date-resolution-correctness.feature`

Scenarios:
- Resolve `05/SET` to 5 September.
- Resolve `SET 05` to 5 September.
- Resolve an abbreviated month case- and accent-insensitively.
- Resolve `Sábado • 05/SET` to 5 September — the operator's real case, where the
  weekday must not win.
- Refuse to guess from `sexta 15`: no date, flagged.
- Refuse to guess when an unparseable month leaves a bare numeral: no date,
  flagged.
- Keep resolving a bare weekday with no competing numeral.
- Keep resolving `toda quinta` through the recurrence path.
- Flag `weekday_mismatch` when an explicit date and a stated weekday disagree,
  and resolve from the explicit date.
- Never match a month abbreviation inside ordinary prose with no adjacent day.
- Show a venue-post `extraction_failed` event in the review queue.
- Show a promoter-post `extraction_failed` event in the review queue.

Pytest unit tests:
- The full input→output table from the Evidence section, asserted verbatim, so a
  future edit that re-widens the weekday branch fails loudly.
- The 339-day case (`Domingo • 20/DEZ` from a January anchor) pinned explicitly
  as no-date-and-flagged — the worst observed instance.
- The guard does NOT fire for a bare weekday.
- The recurrence path is unaffected.
- The queue predicate matrix extended with `extraction_failed` for both source
  kinds.

Manual or integration checks:
- Re-extract the real Métropole posts and confirm SECRET CLUB resolves to
  5 September rather than sitting blank or claiming 8 August.

## Acceptance Criteria
- `05/SET`, `SET 05` and their case/accent variants resolve correctly.
- `Sábado • 05/SET` resolves to 5 September.
- A day numeral that cannot be paired with a month yields no date and a flag,
  never a weekday guess.
- A bare weekday still resolves; recurrence still resolves.
- A weekday disagreeing with an explicit date is flagged, not silently trusted.
- A month abbreviation never matches without an adjacent day numeral.
- `extraction_failed` events appear in the queue for both source kinds.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None.
