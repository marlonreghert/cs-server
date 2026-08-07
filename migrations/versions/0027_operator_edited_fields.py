"""Add `events.event.operator_edited_fields` — the field-level protection
column that replaces the row-level confirmed freeze.

See plans/260807_auto-accept-and-field-level-protection.md §B. Chains from
0026_event_sources, the current head — this migration adds ONE nullable
column to `events.event` and touches nothing else 0022-0026 own.

`operator_edited_fields jsonb` holds the union of every field name an
operator has PATCHed on this event (`app.routers.admin_events_router.
patch_event` writes it from `EventPatch.model_dump(exclude_unset=True)`'s
keys), accumulated across successive patches. `app.services.
event_reconciliation.reconcile_post_events`'s confirmed-row branch reads it
to decide, PER FIELD, whether a re-extraction may update a field (not
operator-edited) or must keep the operator's value and flag a divergence
(operator-edited) — replacing today's blunt "a confirmed event freezes
EVERY field" rule, which also freezes fields the operator never touched.

NULLABLE, NO DEFAULT, NO BACK-FILL — deliberately. An existing row (every row
before this migration runs) has no record of which fields, if any, an
operator corrected; inventing one would either freeze everything (silently
re-creating today's over-broad rule) or freeze nothing (silently discarding
whatever an operator already protected by confirming the row). NULL is read
by the reconciler as "unknown" and gets today's whole-row protection, so no
operator's past work is put at risk by this migration alone. A row only gets
the finer, per-field behaviour once it is next PATCHed.

jsonb (an array of field-name strings), not `text[]`: every other list-typed
`events.event` column (`lineup`, migration 0023) already reads/writes
through the `_EVENT_JSONB_COLUMNS` CAST(:col AS jsonb) + json.dumps() pattern
in `app.dao.rds_venue_store.RdsVenueStore` — reusing it here (rather than
introducing a second, `text[]`-shaped binding path) is what keeps this
migration itself free of any direct jsonb/array bind of its own: it is a
bare `ADD COLUMN`, no data to write, so it cannot reproduce the jsonb-bind-
with-no-psycopg-adapter defect the previous migration in this series hit.

No new index, no new constraint: nothing queries or uniques on this column.

Additive only: one new nullable column on `events.event`, no other table
touched. Downgrade drops exactly that column — safe, because dropping it
only reverts every row to the NULL-means-whole-row-protection rule this
migration's upgrade already treats as the safe default; no row's current
protection changes shape as a result of the downgrade.

Revision ID: 0027_operator_edited_fields
Revises: 0026_event_sources
"""
from alembic import op

revision = "0027_operator_edited_fields"
down_revision = "0026_event_sources"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.event
  ADD COLUMN IF NOT EXISTS operator_edited_fields jsonb;
"""

DOWNGRADE = r"""
ALTER TABLE events.event
  DROP COLUMN IF EXISTS operator_edited_fields;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
