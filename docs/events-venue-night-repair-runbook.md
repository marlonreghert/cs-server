# Venue-night duplication: the one-off historical repair

The runbook for `plans/260912_events-venue-night-duplication.md` §F. Written
for the operator (or agent) running this at 03:00, not for someone reading
the plan.

**Nothing in this branch's deploy changes a stored row.** Every new
behaviour is behind an admin-config key that defaults to today's behaviour:

| Key | Default | What turning it on does |
|---|---|---|
| `event_attribution_dispute_action` | `"flag"` | `"reattribute"` lets a disputed event move to the venue its own text names |
| `event_attribution_dispute_withhold_enabled` | `false` | `true` lets the dispute's review reason withhold auto-accept (removes the row from serving) |
| `event_dedup_recurring_window_enabled` | `false` | `true` makes two recurring rows with intersecting weekday patterns candidates for one night |
| `event_dedup_single_night_venues` | `[]` | a listed venue's same-night rows collapse into one listing |
| `event_dedup_single_night_default_enabled` | `false` | **every** venue's same-night rows collapse — the chosen scope, see below |
| `event_dedup_lineup_threshold` | `2` | **unchanged by this branch** — §E1's decision procedure below |

`event_dedup_auto_merge_enabled` is **already `true` in production** (set
2026-08-14). That is why every key above ships off: anything that widened the
bar would begin merging on the very next crawl, unattended.

---

## Order, and why it is not negotiable

```
1. before-picture   (read-only)
2. threshold measurement at 1 / 2 / 3   (read-only)
3. attribution repair          ← moves rows BETWEEN venues
4. re-measure                  ← the corpus the bar is set against
5. dedup sweep                 ← merges within a venue
6. after-picture   (read-only)
```

`venue_id` is half the candidate window, so a bar tuned against today's
corpus is tuned against a corpus about to move. Step 3 both **removes false
neighbours** (Casa Forte rows leaving Boa Viagem) and **creates true ones**
(those rows arriving somewhere). Steps 3 and 5 run off-peak (03:00–05:00
Recife): a title-similarity merge supersedes the absorbed row, so its
`occurrence_id` stops being projected on the next projector cycle and
`GET /events/{occurrence_id}` 404s for any client holding a stale list. That
is pre-existing; the mitigation is timing, not code.

Ship any helper script to `/app` inside the container, not `/tmp`, and send
each SSM command standalone.

---

## 1. Before-picture (read-only)

```
curl -s localhost:8080/metrics | grep event_dedup_backlog
curl -s localhost:8080/admin/events/dedup-backlog > /app/reports/backlog_before.json
```

`venue_night_excess_rows` is the number this whole plan is judged on.
**Capture it.** The RCA's 55 groups / 93 excess rows were measured on
2026-09-12 and will have moved — acceptance is a reduction against *this
run's own before*, never against 93.

## 2. Threshold measurement (read-only)

```
python -m scripts.measure_event_dedup --lineup-threshold 1 --recurring-window \
    --report-json /app/reports/dedup_t1.json
python -m scripts.measure_event_dedup --lineup-threshold 2 --recurring-window \
    --report-json /app/reports/dedup_t2.json
python -m scripts.measure_event_dedup --lineup-threshold 3 --recurring-window \
    --report-json /app/reports/dedup_t3.json
```

The dry-run path is strictly read-only, makes no external call, and never
reads or writes any admin-config key.

Diff `dedup_t1.json` against `dedup_t2.json` and enumerate every auto pair
present only at 1 (each carries both titles **and** both event ids, so the
diff is exact). That list is §E1's decision input.

**The threshold moves to 1 only if every one of those pairs is individually
reviewed, is a genuine duplicate, and is added as a pinned pair to
`tests/test_event_dedup.py`'s production-string band tests.** A single
unexplained auto pair blocks the change. If it moves, it moves by
`PUT /admin/config/event_dedup_lineup_threshold` — never a deploy.

## 3. Attribution repair

```
python -m scripts.backfill_event_venue_links --mode disputed-location-text \
    | tee /app/reports/attribution_repair_dryrun.log
# review, then:
python -m scripts.backfill_event_venue_links --mode disputed-location-text --apply \
    | tee /app/reports/attribution_repair_apply.log
```

**Capture the dry-run log to a file before `--apply`.** Attribution repair is
not reversible beyond that report — it is the only record of every changed
row's previous `venue_id`.

Verify by name afterwards: no row at "BeerDock Boa Viagem" should still carry
`location_text = 'CASA FORTE'`.

The dry run also prints the **venue-acquisition backlog**: the `@handle`s an
account's events name that the catalog does not carry. Those are rows nothing
can repair until the venue is added.

## 4. Re-measure

```
python -m scripts.measure_event_dedup --recurring-window \
    --lineup-threshold <chosen> --single-night-all \
    --report-json /app/reports/dedup_after_repair.json
```

`--single-night-all` measures the CATALOG-WIDE scope without writing the
admin-config key. **Read its auto-pair count before you flip anything.** The
venue-night backlog is ~55 groups / ~93 excess rows, so an auto-pair count in
the low hundreds is the expected order (a group of *n* rows yields *n(n-1)/2*
pairs, so a handful of large groups dominates). A count **wildly** larger than
that means something is evaluating pairs it should not — across dates, across
venues, or over non-event rows — which is a **bug, not the intended scope**.
Stop and report rather than applying.

This is the corpus §E1's decision is made against **and** the "before" for
step 5.

## 5. Dedup sweep

```
python -m scripts.measure_event_dedup --apply --recurring-window \
    --lineup-threshold <chosen> --single-night-all \
    --max-auto-pairs <ceiling> \
    --report-json /app/reports/dedup_sweep.json
```

Set `--max-auto-pairs` from **step 4's own auto-pair count plus a stated
margin**. If the sweep's own dry-run pass exceeds it, the script writes
nothing and exits `6` — that is what makes an unattended `--apply` safe: it
stops on a surprise instead of merging its way through one.

The sweep re-measures itself afterwards and exits `1` if any auto pair
remains. It is idempotent, resumable (`--since-venue-id`), and every merge it
performs is reversible per pair through
`POST /admin/events/{absorbed_event_id}/reverse-merge`.

## 5b. Turn the single-night scope on

```
PUT /admin/config/event_dedup_single_night_default_enabled   body: true
```

Do this AFTER step 3's attribution repair, never before: collapsing a
venue-night at a venue that is still holding another venue's rows merges two
different bars into one listing.

## 6. After-picture

Re-read the two gauges and the backlog report.
`venue_night_excess_rows` must be **materially lower** than step 1's value
and `venue_night_groups` must not have grown. Confirm
`events_projected_occurrences` did not collapse, and spot-check one absorbed
pair's survivor through the app-facing list: it must still appear, with the
absorbed rows' acts folded into `lineup`.

## 7. Standing watch, one week

`venue_night_excess_rows` must not climb back toward its step-1 value. If it
does, the forward fix is not holding, and `GET /admin/events/dedup-backlog`
names the venues to look at.

---

## The single-night scope: catalog-wide, decided and why

**The operator chose the catalog-wide flag, not the per-venue list.** Set
`event_dedup_single_night_default_enabled = true`; leave
`event_dedup_single_night_venues` empty.

This was put to them with the tradeoff stated, and accepted explicitly. Record
of the decision, so nobody has to reconstruct it later:

- **What was proposed first:** adding Club Metrópole alone to
  `event_dedup_single_night_venues`.
- **The counter-example that made it a real question:**
  `260812_event-dedup-fuzzy-title.md`'s own measured false-positive corpus
  contains `Bolinha do Cavaco` / `JB do Cavaco` at **Casanova Ecobar** — two
  different acts, one venue, one night, deliberately kept apart by that
  review. Club Metrópole's five acts on one Saturday are the **same shape**.
  Nothing in the rows distinguishes them; the difference is a fact about the
  venue — a club runs one night, a theatre runs a programme.
- **What the operator accepted, knowingly:**
  1. the `Bolinha do Cavaco` / `JB do Cavaco` shape **will re-merge wherever
     it recurs** — this is the decision, not a defect, and
     `tests/test_event_dedup_single_night.py` asserts it as such;
  2. any **undiscovered "programme" venue** running genuinely separate
     same-night events will have one silently absorbed into another, until
     someone notices it in `GET /admin/events/dedup-backlog`.

Two operational consequences worth knowing before you turn it on:

- **The reversal of an individual bad merge is per-pair and cheap:**
  `POST /admin/events/{absorbed_event_id}/reverse-merge` restores the row and
  exactly the sources that moved.
- **Dialling the SCOPE back is not cheap, and there is no exclusion list.**
  The only way to narrow it is to set the flag `false` and enumerate, in
  `event_dedup_single_night_venues`, every venue you still DO want collapsed
  — the whole catalog minus the exception. If this is ever dialled back in
  anger, adding an explicit exclusion key is the obvious follow-up; it was
  deliberately not built ahead of a need for it.

`260812`'s measured evidence puts **Teatro Riachuelo**, **Sempre Rock Bar**
and **Entre Amigos O Bode** in the programme group. They are the first places
to look when auditing the backlog report after the sweep.

## What this does NOT fix on its own

Until the single-night scope is turned on, the Club Metrópole cluster
shrinks — from five rows toward three, if the threshold measurement supports
moving to 1 — but does **not** become one row: no threshold on either
existing signal reaches `ROWKA` or `VITINHO POLÊMICO`. Do not read a green
pipeline as "the reported symptom is gone".

Nor does the single-night scope touch the three worst venues by excess rows.
Measured on the live corpus 2026-09-13: **BeerDock Boa Viagem (19 excess),
zef'as bar (15) and Seu Chico Botequim (10) are attribution problems, not
dedup problems** — 20 of BeerDock's 44 rows carry `location_text = 'CASA
FORTE'`, and zef'as bar's 16-row group is a Natal roundup account
(`oquetemhojeemnatal`) whose rows name 13 different `@handles`, i.e. 13
different venues filed at one. Step 3 is what moves those; collapsing them as
"one venue-night" would be actively wrong. That is the whole reason the
sequence puts the attribution repair first.
