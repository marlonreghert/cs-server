"""Add `events.event.ticket_info` and `events.event.attractions` — a
non-URL ticket reference and structured performer data, both currently
discarded. See plans/260808_event-ticket-info-and-attractions.md.

Chains from 0027_operator_edited_fields, the current head — this migration
adds two nullable columns to `events.event` and touches nothing else 0022-
0027 own.

`ticket_info text`: a free-text ticket reference copied verbatim, the same
way `price_text`/`location_text` already are — a bare emoji label, a
WhatsApp number, an @handle, a lote deadline. Joins `PROTECTABLE_EVENT_
FIELDS`/`_TEXT_FIELDS` in app.services.event_reconciliation (an ordinary
scalar, merged exactly like `price_text`), never `attractions`'s union
treatment.

`attractions jsonb`: a list of `{name, type, stage, styles}` objects — the
acts a flyer names, each tagged with whether it is a DJ set or a live act,
which stage it plays, and its musical styles (constrained to
`taxonomy.musica`). Additive across the posts announcing one event (the same
union rule `lineup` already uses), so it is deliberately NOT added to
`PROTECTABLE_EVENT_FIELDS` — see app.services.event_reconciliation.
union_attractions and app.services.event_merge's two merge branches.

NULLABLE, NO DEFAULT, NO BACK-FILL — deliberately, for both columns. Neither
describes information the model was ever asked to produce on earlier runs;
NULL is the historically accurate value, not a placeholder needing repair. A
row gets real data once re-extracted, consistent with 0027's own no-back-
fill rationale and the "re-extraction is operator-triggered" stance in
plans/260806_multi-event-posts.md.

`attractions` is jsonb, not `text[]` or a second table: it reuses the exact
`_EVENT_JSONB_COLUMNS` CAST(:col AS jsonb) + json.dumps() pattern
`app.dao.rds_venue_store.RdsVenueStore` already uses for `lineup` and
`operator_edited_fields` — this migration itself writes no jsonb value at
all (a bare ADD COLUMN, no data to touch), so it cannot reproduce the
jsonb-bind-with-no-psycopg-adapter defect a previous migration in this
series hit.

No new index, no new constraint: nothing queries or uniques on either
column, and neither participates in `uq_event_source_post`.

Additive only: two new nullable columns on `events.event`, no other table
touched. Downgrade drops exactly those two columns — safe, and unlike
0026's downgrade, needs NO destructive-refusal guard: dropping `ticket_info`/
`attractions` only discards per-row detail captured after this migration
ran, it never merges or deletes a row, and no other column's meaning changes
as a result.

Revision ID: 0028_event_ticket_info_and_attractions
Revises: 0027_operator_edited_fields
"""
from alembic import op

revision = "0028_event_ticket_info_and_attractions"
down_revision = "0027_operator_edited_fields"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.event
  ADD COLUMN IF NOT EXISTS ticket_info text,
  ADD COLUMN IF NOT EXISTS attractions jsonb;
"""

DOWNGRADE = r"""
ALTER TABLE events.event
  DROP COLUMN IF EXISTS ticket_info,
  DROP COLUMN IF EXISTS attractions;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
