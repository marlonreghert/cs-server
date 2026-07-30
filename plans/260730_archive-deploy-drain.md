# A Deploy Must Not Destroy An In-Flight Archive Run

## Branch
feature/archive-deploy-drain

## Goal
A deploy must not silently destroy a running archive job. Either the deploy waits
for the run to finish, or the run is stopped in a way that records what it did and
leaves the archive resumable — never a container that vanishes mid-scrape.

## Non-goals
- Resuming an interrupted run automatically. `skip_scope: this_run` already makes
  a manual resume cheap; automatic restart is a separate decision.
- Any change to fetch, poll-budget, or retry behavior.
- Blocking deploys indefinitely. A drain must be bounded — shipping a fix must
  stay possible even with a long run in flight.

## Evidence

**A deploy has already destroyed a run, and it was invisible.** Run
`01KYP1211ZV9M5PCJ4CB8083W5` wrote 51 objects in its final minute and then stopped
dead at 01:36:10Z. A deploy's Zero-Downtime Swap ran 01:35:46→01:36:56Z. The
workflow step is:

```
sudo -E docker-compose -f docker-compose.yml up -d --force-recreate --no-deps cs-server
```

`--force-recreate` destroyed the container mid-run. The trigger was ordinary:
cs-server #119 merged at 01:26:23Z, vibes_bot #145 at 01:26:30Z, deploy at
01:26:33Z. Nobody did anything unusual, and the run stopped at 23 of its venues
with no error anywhere — the position it died at was pure coincidence.

**Nothing detects or reports it.** The container is killed, so:
- the `CancelledError` handler at `venue_photo_archive_service.py:833` may not run
  at all — and even when it does, `_save_run_record` writes to an in-memory dict
  that dies with the process (see `260730_durable-run-records.md`);
- Prometheus counters are per-process and reset, so the run's own numbers vanish;
- no `_latest.json` marker is written, which is correct, but nothing else records
  that a run was interrupted.

The result is a partial S3 partition and no record that it is partial.

**The deploy has no idea a job is running.** `sync_cs_server` clones, builds, and
recreates unconditionally. cs-server exposes `/health` but nothing that says "a
job is in flight", so the workflow could not wait even if it wanted to.

**The cost is real.** Each destroyed venue was already billed; the work is lost
but the money is not refunded, and the venues silently never appear in the
archive.

## Current Behavior
1. Any push to vibes_bot `main` can recreate the cs-server container at any
   moment.
2. An archive run in flight is killed. Venues already paid for are lost.
3. No marker, no record, no metric survives to say it happened.
4. The next run's `latest_run` skip check sees a partition that looks complete but
   is not, so the missing venues are skipped rather than retried.

## Desired Behavior
1. cs-server must expose whether an archive job is currently running, so a
   deployer can ask before recreating it.
2. The deploy must drain: when a job is in flight it waits for a bounded period
   before recreating the container, and reports that it is waiting.
3. If the drain window expires, the deploy proceeds but the interruption must be
   recorded durably — an operator must be able to see that a run was cut short and
   which venues it had reached.
4. On SIGTERM, cs-server must stop starting new venues, let in-flight venues
   finish storing what they have already paid for, and persist the run as
   interrupted.
5. An interrupted run must never leave a partition that later looks complete: it
   must not write the completion marker (already true), and the interruption must
   be discoverable.

## Implementation Approach
- **cs-server:** a lightweight "is a job running" signal — the
  `pipeline_run_registry` already tracks live runs and can answer it — exposed on
  an existing admin/internal route. A SIGTERM handler that flips the run into
  drain mode: stop starting new venues (`_guarded` already early-returns on a
  flag, mirroring `credit_exhausted`), let in-flight ones finish, persist the
  record as interrupted.
- **vibes_bot:** before the Zero-Downtime Swap, poll the signal and wait up to a
  bounded window, logging that it is draining. On expiry, proceed — a fix must
  always be shippable — but the interruption is now recorded on the cs-server
  side rather than silent.
- **Depends on `260730_durable-run-records.md`** for the interrupted record to
  survive the restart that caused it. Without that, the record dies with the
  process and step 3 is unimplementable. Sequence: durable records first.

## Data, Config, And API Impact
- **API:** one read-only "job in flight" signal (internal/admin).
- **Config:** drain timeout on the deploy side; grace period on the cs-server
  side.
- **Deploy:** a new wait step in vibes_bot's workflow before the swap.
- **Persistence:** none of its own; reuses the durable-run-records table.

## Error Handling And Observability
- The drain must be bounded and must fail open: an unreachable signal means the
  deploy proceeds, because a broken health check must not block shipping.
- The interruption must be visible: a counter for interrupted runs and a log line
  naming the job id and how far it got.
- The drain wait must be logged in the deploy output, so a slow deploy is
  self-explanatory rather than mysterious.

## Test Plan
Feature file: `tests/bdd/observability/archive-deploy-drain.feature`

Scenarios:
- While a job runs, the in-flight signal reports it; when idle, it does not.
- On SIGTERM, an in-flight run stops starting new venues.
- On SIGTERM, venues already fetched still store what was paid for.
- An interrupted run is persisted with an `interrupted` status and the count it
  reached.
- An interrupted run writes no completion marker.
- A drain that exceeds its window still terminates rather than hanging.

Pytest unit tests:
- The drain flag's effect on venue scheduling; the signal's shape while running
  and idle; the SIGTERM handler's idempotence under repeated signals.

Manual or integration checks:
- Start a run in prod-like compose, deploy over it, confirm the deploy waits and
  the run finishes; then force expiry and confirm the interruption is recorded.

## Acceptance Criteria
- A deploy started while a job runs waits rather than killing it immediately.
- A drain that expires still deploys, and the interruption is recorded durably.
- On SIGTERM no new venues start and in-flight ones finish storing.
- An interrupted run never writes the completion marker and is visibly
  interrupted afterwards.
- The deploy proceeds when the signal is unreachable.

## Open Questions
- Drain window length. A full archive run can exceed an hour, so waiting for
  completion is not always acceptable; a window in the low minutes plus a recorded
  interruption is likely the right trade, to be confirmed with the user.
