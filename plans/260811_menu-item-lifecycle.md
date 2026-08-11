# Menu Item Lifecycle — one dish, one row, one year

## Branch
feature/menu-item-lifecycle

## Goal
A dish stays on the books for a year after it was last seen, a newer posting of
the same dish replaces the older one, and a dish nobody has mentioned in a year
stops being presented as current.

## Non-goals
- **Deleting anything.** Expiry hides; the row and its provenance remain.
- **Replacing a venue's whole menu from one post.** See §B for why that is not
  inferable from what a post actually says.
- **Expiring events or promotions.** An event has a date and passes on its own;
  a promotion's lifecycle is a separate decision nobody has made yet.
- **Serving menus to the app.** Admin-only, as with everything here.

## Evidence

### A dish has no end signal
`260811_post-items-and-categories.md` made menu items first-class: they are
extracted, typed and stored. Nothing ages them out. An event passes when its
date does; a daily special runs until the venue quietly stops posting it, and no
post ever says "we stopped serving the risotto".

Left alone, the menu grows monotonically and eventually presents dishes the
kitchen abandoned months ago as current.

### A dish also has no date, so it never merges
Cross-post identity is `(venue_id, calendar date, normalized_title)`, and
`compute_event_identity` returns `None` without a `starts_at`. The production
menu item makes the problem concrete:

```
title            'Especial do dia'
starts_at        NULL
is_recurring     true
recurrence_text  'de segunda a sexta'
```

No date, therefore no identity, therefore no merge. Every re-extraction of that
post — and every future post about the same dish — creates another row. The
duplication the event path spent three rounds eliminating is wide open on the
menu path, and it will not surface as obviously, because nobody is reviewing
menu items yet.

## Current Behavior
Menu items never merge, never expire, and accumulate one row per extraction.

## Desired Behavior
1. The same dish at the same venue is one row, however many posts mention it.
2. Re-seeing a dish refreshes how recently it was seen.
3. A dish unseen for a year is no longer presented as current.
4. The expiry window is configurable without a deploy.
5. Expiring hides a dish; it never deletes it.
6. Events and promotions are untouched.

## Implementation Approach

### A. A dish's identity is its venue and its name
Give `post_type = 'menu'` items an identity of **`(venue_id, normalized_title)`**
— deliberately **without** a date, because a dish is the same dish whenever it
is posted. Reuse `normalize_title`; do not write a second normalisation.

This is a genuine departure from event identity and the reason must stay
visible in the code: for an event, the date is *what distinguishes* two
otherwise identical listings — "Karaoke" on Friday and on Saturday are two
events. For a dish, the date is *noise about when someone posted it*.

**Only menu items.** An event or promotion keeps its dated identity exactly as
today, and the plan requires a test proving that.

`venue_id` is still required. An unresolved menu item has no identity, stays
unmerged and stays queued — same posture as everywhere else: no venue, no guess.

### B. A newer posting replaces the older
When a dish's identity matches an existing row, update it in place: refresh
`last_seen_at`, attach the new source, and merge fields under the rules that
already exist — a null never overwrites a known value, and
`operator_edited_fields` wins.

**Do not infer that a new menu post replaces a venue's entire menu.** A post
showing today's special says nothing about the other nine dishes; treating it as
a wholesale replacement would silently expire dishes still being served, from
evidence that does not support it. Per-dish replacement is what the data
actually licenses.

### C. Expiry is a window, not a job
Derive expiry from `last_seen_at` plus a configurable window defaulting to
**365 days**. Compute it at read time rather than mutating rows on a schedule:
a stale row that becomes fresh again the moment its dish is re-posted is exactly
the desired behaviour, and a nightly job would have to un-expire it, which is a
second mechanism that can disagree with the first.

Put the window in **admin config**, not a constant — venue types, busyness
labels and the category vocabulary are all runtime-configurable in this project
for the same reason: the first value is a guess and changing it must not need a
deploy.

Expose the derived state on the API so the console can show and filter it. An
expired dish stays queryable and keeps every source it ever had.

**Expiry must not resurrect an item into the review queue.** The queue shows
what needs a decision; a dish going quietly stale is not a decision, it is the
absence of one.

## Data, Config, And API Impact
- **Migration `0036_menu_last_seen`** only if `last_seen_at` is not already on
  `post_item` in a usable form — `post_item_source` carries per-source
  timestamps, so check before adding a column, and say which you found.
- **Back-fill:** existing menu items take their newest source's `last_seen_at`.
  That is a fact already recorded, not an invention.
- **Config:** `menu_expiry_days`, default 365.
- **API:** `EventOut` gains the derived expiry state. Additive.
- **Rollback:** revert; nothing is deleted, and read-time derivation leaves no
  residue.

## Error Handling And Observability
Count menu merges and current-versus-expired menu items. **Watch the expired
share:** a sudden jump means either a venue stopped posting or extraction
stopped recognising its dishes, and those need telling apart.

## Test Plan
Feature file: `tests/bdd/enrichment/menu-item-lifecycle.feature`

Scenarios:
- Keep one row when two posts announce the same dish.
- Refresh a dish's last-seen when it is posted again.
- Keep two different dishes at one venue apart.
- Keep the same dish at two venues apart.
- Present a dish seen this month as current.
- Stop presenting a dish unseen for over a year as current.
- Present a long-unseen dish as current again once it is re-posted.
- Keep an expired dish and its sources queryable.
- Never queue a dish for review because it expired.
- Leave an unresolved menu item unmerged and queued.
- Leave events and promotions merging exactly as before.
- Honour a changed expiry window from configuration.

Pytest unit tests:
- Menu identity: same dish differing in case and accents; different dishes;
  same dish at different venues; a null venue.
- Event and promotion identity are unchanged, asserted against pre-change
  behaviour.
- Expiry at the boundary: one day inside, exactly on, one day outside.
- `operator_edited_fields` survives a re-posting.
- A null in a newer post never overwrites a known value.
- The back-fill takes the newest source's timestamp.

## Acceptance Criteria
- One row per dish per venue, refreshed on re-posting.
- A dish unseen beyond the window is not presented as current, and returns when
  re-posted.
- The window is configurable; expiry hides and never deletes.
- Events and promotions are unaffected.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None. If `last_seen_at` cannot be derived without a new column, add one and say
so; do not read it out of a JSONB blob at serve time — that is the bug
`260811_expose-time-known.md` was written to fix.
