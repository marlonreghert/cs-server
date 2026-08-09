"""Add `events.crawl_target` — the schedule and cursor for an incremental,
per-handle Instagram crawl. See plans/260809_scheduled-incremental-instagram-
crawl.md.

Chains from 0029_blocked_venues, the current head (the plan text names
0028_event_ticket_info_and_attractions as head; 0029_blocked_venues merged to
main before this migration was written, so this one chains from the ACTUAL
head instead — the plan's chain reference was already stale). This migration
adds one new table to the `events` schema (created by 0022) and touches
nothing else 0022-0029 own.

`handle` is the PRIMARY KEY, not `venue_id` and not a surrogate id: the plan's
own measurement (1,114 non-deleted `instagram.handle` rows resolve to only
1,066 DISTINCT handles — 48 venue rows share a handle with another venue, e.g.
`Entre Amigos O Bode` / `Entre Amigos O Bode Espinheiro` both post as
`@entreamigosobode`) means a `venue_id`-keyed schedule would crawl — and BILL
— a shared handle twice per cycle. Keying on the handle crawls it once;
venue attribution for a `kind='venue'` target is resolved downstream (by
whichever venues currently reference that handle in `instagram.handle`), not
stored redundantly here. `kind` distinguishes a venue handle from a promoter
handle so one table can schedule both without a second, near-identical table
— `events.promoter_account` keeps its own registry (candidacy, discovery,
mention counts) and is untouched by this migration; a promoter that should be
crawled gets a `crawl_target` row ALONGSIDE its `promoter_account` row, not
instead of it.

Every column beyond the key is scheduling/cursor state, never event content:
  - `enabled` / `cron` / `timezone` — whether and when this target's schedule
    fires. `cron` is NOT NULL with no default: a target is created BY an
    operator WITH a schedule, never inserted schedule-less and left to invent
    one. `timezone` defaults to `America/Recife` — the cron fires in LOCAL
    time (an operator saying "Friday night" means Friday night in Recife).
    `cron` is validated AND translated (app.services.instagram_crawl_service.
    validate_crontab / build_cron_trigger) before it ever becomes an
    APScheduler trigger — APScheduler's OWN day_of_week numbering
    (0=Monday..6=Sunday) differs from standard Unix cron's (0=Sunday..
    6=Saturday), and its `CronTrigger.from_crontab` does NOT translate
    between them, so this column's value is interpreted as standard cron at
    both the admin-router write boundary and `crawl_schedule_sync`'s fire
    boundary, through the same shared function; this column itself just
    stores whatever string is given, unmodified.
  - `crawl_reels` — whether this target also gets a second, independent
    reels run (§E of the plan): some venues announce events only as a reel,
    never a grid post.
  - `classify_images` — whether the scheduled crawl's own archiving step
    runs the SAME photo classifier `VenuePhotoArchiveService` already uses
    (`app.services.instagram_crawl_service.InstagramCrawlChainer`), so an
    image-only flyer (no event-marker caption at all — the flyer graphic
    carries every word) still reaches extraction instead of being silently
    skipped as `not_event_like`. Defaults TRUE: the scheduled crawl replaces
    a manual archive run that already classified, so matching its WEAKER,
    caption-only behavior by default would be a coverage regression, not
    parity. FALSE is an explicit, operator-chosen cheap-mode opt-out per
    target — never the default a target silently inherits.
  - `initial_lookback` / `results_limit` — the two bounds a brand-new target
    (null cursor) is seeded under (§C): a date bound (what the operator
    reasons about) AND a count cap (what stops a prolific account from
    spending the whole run on its first tick). Both nullable: null falls back
    to the settings-level default (`crawl_default_initial_lookback` /
    `crawl_default_results_limit`), so a target need not repeat the global
    default to use it.
  - `cursor_posts_at` / `cursor_reels_at` — the newest POST timestamp this
    target has successfully ingested, one per stream (§B, §E: two independent
    streams, because a venue can post grid content and no reels for a month —
    a single shared cursor would drag the reels bound forward past reels that
    were never fetched, and they would never be seen). Stored and read in
    UTC — the actor's `onlyPostsNewerThan` filter is documented UTC, and only
    the CRON TRIGGER is local; conflating the two shifts every bound by three
    hours. Both start NULL (a target that has never been crawled) and advance
    ONLY on a successful crawl that returned at least one kept post — a
    failed or empty run must leave them untouched, or the failure window is
    skipped forever, silently.
  - `last_run_at` / `last_run_results` / `last_run_cost_usd` /
    `consecutive_failures` — the operational record §H's per-run accounting
    needs: when this target last ran, how many billed results it returned,
    what that cost, and how many times in a row it has failed (so a dead
    handle can be skipped once it crosses a threshold, rather than burning
    the shared monthly budget on retries forever). `last_run_results`/
    `consecutive_failures` default to 0 (NOT NULL) so a freshly created
    target reads as "nothing has happened yet" rather than NULL, matching
    `events.promoter_account`'s own convention for its equivalent counters.
  - `notes` — free-text operator annotation, no behavior.

`ck_crawl_target_kind` constrains `kind` to `('venue', 'promoter')` — the only
two values `events.promoter_account.status` conventions and this plan's own
vocabulary use; nothing else is a valid crawl target kind.

`ix_crawl_target_enabled` supports the scheduler's own read pattern: "every
enabled target," on every registry sync, mirroring
`ix_promoter_account_status`'s existing precedent for indexing the column a
periodic job filters on.

NO BACK-FILL — deliberately: inventing a schedule for any of the 1,066
distinct handles in the catalog today is precisely the unaffordable case the
plan's §Evidence rules out (one pass at 10 posts/handle is ~$30, six times the
operator's $5 balance). An existing venue or promoter has NO schedule until an
operator explicitly gives it one.

Additive only: one new table, one new index, one new check constraint. The
downgrade drops exactly what this migration created and touches no other
table. Safe: losing a target's cursor only means its NEXT manual/admin crawl
re-fetches a lookback window (costs money, matching a brand-new target's
first-ever crawl) — it never deletes, merges, or reinterprets an existing
`events.event` row, `instagram.handle` row, or `events.promoter_account` row.

Revision ID: 0030_crawl_target
Revises: 0029_blocked_venues
"""
from alembic import op

revision = "0030_crawl_target"
down_revision = "0029_blocked_venues"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS events.crawl_target (
  handle              text PRIMARY KEY,
  kind                text NOT NULL,
  enabled             boolean NOT NULL DEFAULT true,
  cron                text NOT NULL,
  timezone            text NOT NULL DEFAULT 'America/Recife',
  crawl_reels         boolean NOT NULL DEFAULT false,
  classify_images     boolean NOT NULL DEFAULT true,
  initial_lookback    text,
  results_limit       integer,
  cursor_posts_at     timestamptz,
  cursor_reels_at     timestamptz,
  last_run_at         timestamptz,
  last_run_results    integer NOT NULL DEFAULT 0,
  last_run_cost_usd   double precision,
  consecutive_failures integer NOT NULL DEFAULT 0,
  notes               text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_crawl_target_kind CHECK (kind IN ('venue', 'promoter'))
);

CREATE INDEX IF NOT EXISTS ix_crawl_target_enabled
    ON events.crawl_target (enabled);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS events.ix_crawl_target_enabled;
DROP TABLE IF EXISTS events.crawl_target;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
