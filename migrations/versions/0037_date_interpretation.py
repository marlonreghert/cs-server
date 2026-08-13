"""Add `events.post_item_source.date_interpretation` — the model's
structured reading of `date_text`, consulted only as a fallback when the
deterministic finders in `app.services.event_date_resolver` return no match.

See plans/260812_event-attribution-and-dates.md §C.

## Why this column exists

`date_text` stays exactly as it is — verbatim, unchanged — and remains the
input the deterministic finders try FIRST; today's ~97% keep their existing,
proven path with no change at all. For the residual shapes those finders
cannot read at all ("É HOJE", "Quinta (02)"), the model now ALSO returns a
small tagged interpretation of the same text (see
`app.api.openai_event_extraction_client`'s new `date_interpretation` field),
and Python — never the model — turns that into a calendar date
(`app.services.event_date_resolver._interpretation_to_date`).

## The determinism guard this column exists to serve

`app.services.event_identity.compute_source_event_key` hashes the RESOLVED
date. A resolver that can answer differently for the SAME `date_text` across
two extractions of the same post would turn every re-extraction into a
duplicate-event generator — precisely what `0025_multi_event_posts` was
written to prevent. Persisting the interpretation actually used lets a
later re-extraction, when its own fresh `date_text` is byte-identical to
what is stored here, REUSE the stored interpretation instead of trusting a
new (possibly different) model answer — same text in, same date out, same
`source_event_key`, even though a model took part.
`app.services.event_date_resolver.select_date_interpretation_for_reuse` is
the pure function that makes this choice; the two real callers
(`EventExtractionService`, `PromoterCrawlService`) read this column back via
`existing_events` before resolving each event's date.

## Column shape and back-fill

`date_interpretation jsonb NULL` — nullable, additive, no back-fill: every
pre-existing row has no stored interpretation (there is nothing to reuse for
it), which is the correct, honest reading — the very next re-extraction of
that post populates it going forward.

## Rollback

`DROP COLUMN date_interpretation` — safe: no other column or table is
touched, nothing here merges or deletes a row. Losing this column only means
the determinism guard degrades back to "always trust the fresh model
answer" for the ~3% of dates the deterministic finders cannot read alone —
the SAME behaviour every row had before this plan, never a data loss.

Revision ID: 0037_date_interpretation
Revises: 0036_source_media_type
"""
from alembic import op

revision = "0037_date_interpretation"
down_revision = "0036_source_media_type"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.post_item_source
  ADD COLUMN IF NOT EXISTS date_interpretation jsonb;
"""

DOWNGRADE = r"""
ALTER TABLE events.post_item_source
  DROP COLUMN IF EXISTS date_interpretation;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
