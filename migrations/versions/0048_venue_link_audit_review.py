"""Add `events.venue_link_audit_review` —
plans/260914_agentic-venue-resolution-fallback.md §5 ("Persistence — a new
table at the audit's own grain").

One row per FLAGGED `(handle, venue_id)` pair the agentic reviewer has
looked at: the consensus verdict, the evidence quote it rests on, the field
that evidence matched, the model's one-line reason, the model id, K, the
per-verdict tally, the audit counts as of the review, and a nullable
operator decision with its own timestamp and note.

## Why the primary key is `(handle, venue_id)`, not an event

`app.services.venue_link_audit.compute_venue_link_audit` flags a MAPPING —
"this `kind='venue'` crawl target's own posts give no evidence for the venue
it is mapped to" — and its unit of output is
`MappedVenueAudit(venue_id, checkable_count, non_corroborating_count)` under
one handle, aggregated ACROSS that handle's whole event corpus. A verdict
about that mapping is therefore true (or false) of the pair, not of any one
event: the reviewer reads many events to reach one answer, and the flag it
suppresses is raised once per pair. An event-grain table would have to
re-state the same verdict once per event, then keep those copies consistent
as the corpus grows — and would have nothing to key on for the suppression
decision §5 actually needs, which asks a pair-level question ("is this pair
still suppressed?").

## Why `events.event_venue_link_candidate.llm_recommendation` (0047) was rejected

That column was considered first and does not work here, for two independent
reasons measured live in plan §B:

1. It is PER-EVENT — one advisory recommendation attached to one ranked
   candidate row of one event — which is the wrong grain, per above.
2. There is nothing to attach it to. A `kind='venue'` crawl target whose
   handle maps to exactly one venue has every event FORCE-assigned by
   `EventExtractionService._extract_one`'s default closure and never runs
   `resolve_event_venue`, so `replace_event_venue_link_candidates` never
   fires and no candidate row is ever written. Measured across the nine
   flagged handles: **8 of the 9 have ZERO rows** on
   `events.event_venue_link_candidate` (only the double-mapped
   `entreamigosobode` has any, because being double-mapped is what makes it
   run the ladder at all). A design writing to 0047's column would do
   literally nothing for 8 of 9 handles.

## Deliberately no foreign keys

Neither `handle` nor `venue_id` gets an FK, by choice:

- `handle` is not a foreign key anywhere in this schema. It is a plain
  `text` join value on `events.crawl_target`, `events.promoter_account` and
  `events.post_item_source.source_handle`; there is no single owning table
  to point at, and `events.event_merge_suggestion` (0039) set the precedent
  of declaring no FK for exactly this kind of cross-table text reference.
- `venue_id` is deliberately left unconstrained because this repo NEVER
  hard-deletes a venue (soft-delete via `venues.venue.deleted_at` /
  `lifecycle_status`), and a review stays meaningful history after a venue
  is deprecated: "the machine confirmed this mapping on 2026-09-14" is
  exactly the record an operator wants when a venue later goes away. An FK
  would buy nothing (there is no delete to cascade) and would risk a future
  cleanup path failing on a stale advisory row it was never designed to
  know about.

## No index

12 flagged pairs today, and the table is read either by its primary key
(the suppression check, one pair at a time) or in full by the operator list
endpoint, which sorts the whole small set by `updated_at`. The PK index is
the only access path worth having; a `verdict` or `operator_decision` index
would index a handful of rows. Add one if this ever becomes a catalog-wide
sweep — it is not one.

## Downgrade

Safe. Every row is DERIVED, advisory review history: re-running the
reviewer recomputes a verdict for any pair still flagged, and nothing
outside this feature reads the table — no other table, key, or view depends
on it. Dropping it loses only that history and any un-actioned operator
decision, after which every previously-suppressed pair simply shows up
flagged again, which is the safe direction to fail.

Revision ID: 0048_venue_link_audit_review
Revises: 0047_event_venue_link_candidate_llm_recommendation
"""
from alembic import op

revision = "0048_venue_link_audit_review"
down_revision = "0047_event_venue_link_candidate_llm_recommendation"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS events.venue_link_audit_review (
  handle                            text NOT NULL,
  venue_id                          text NOT NULL,
  outcome                           text NOT NULL,
  verdict                           text,
  evidence_quote                    text,
  matched_field                     text,
  reason                            text,
  model                             text,
  consensus_k                       integer,
  tally                             jsonb,
  checkable_count_at_review         integer,
  non_corroborating_count_at_review integer,
  operator_decision                 text,
  operator_decided_at               timestamptz,
  operator_note                     text,
  created_at                        timestamptz NOT NULL DEFAULT now(),
  updated_at                        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (handle, venue_id)
);
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS events.venue_link_audit_review;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
