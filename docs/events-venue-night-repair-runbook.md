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
    --lineup-threshold <chosen> --single-night-venue <id> ... \
    --report-json /app/reports/dedup_after_repair.json
```

This is the corpus §E1's decision is made against **and** the "before" for
step 5.

## 5. Dedup sweep

```
python -m scripts.measure_event_dedup --apply --recurring-window \
    --lineup-threshold <chosen> --single-night-venue <id> ... \
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

## What this does NOT fix on its own

Until `event_dedup_single_night_venues` is populated, the Club Metrópole
cluster shrinks — from five rows toward three, if the threshold measurement
supports moving to 1 — but does **not** become one row: no threshold on
either existing signal reaches `ROWKA` or `VITINHO POLÊMICO`. Do not read a
green pipeline as "the reported symptom is gone".

Which venues belong on that list is a product judgement, not a measurement.
A club runs one night; a theatre and a bookshop run a programme. `260812`'s
own measured evidence puts Teatro Riachuelo, Sempre Rock Bar and Entre Amigos
O Bode firmly in the second group — and `Bolinha do Cavaco` / `JB do Cavaco`
at Casanova Ecobar is the same shape as Club Metrópole's five acts, which is
exactly why this is a per-venue list and not a rule.
