"""Shared per-post event reconciliation for the venue and promoter multi-event
extraction paths. See plans/260806_venue-post-multi-event.md.

Both `EventExtractionService` (venue posts) and `PromoterCrawlService`
(promoter posts) extract a LIST of events from one post — a single-event post
still yields a list of one (plans/260806_multi-event-posts.md) — and both must
reconcile that list against whatever was already persisted for the SAME post,
by content-derived `source_event_key` (app.services.event_identity), never by
list position: the model does not guarantee stable ordering between runs, so
an ordinal would silently migrate an operator's confirmation onto a DIFFERENT
event the moment the model's list order changed.

Before this module, the two callers each grew their OWN copy of this
mechanism, and the copies had already drifted: the venue path flagged a
divergence when a confirmed event's title/date stopped matching the model's
answer, the promoter path did not. This module is the ONE implementation, and
it unifies on the RICHER behaviour — the promoter path gains divergence
flagging by moving onto it.

Owns everything that must never differ between the two callers:
  - indexing the post's existing rows by `source_event_key`;
  - a `confirmed` row: only `raw_extraction`/`last_seen_at` move, and a
    divergence between the model's FRESH title/date and what the operator
    already has is FLAGGED via `review_reason`, WITHOUT moving status away
    from confirmed;
  - a manually-linked row (`location_resolution == "manual"`): every
    attribution column is left untouched (content still refreshes);
  - otherwise: a full upsert;
  - every existing key this run did not return: superseded, never deleted,
    never touched once confirmed or manually linked, and never touched (nor
    treated as a candidate for anything) when it has no `source_event_key`
    at all — an `extraction_failed` placeholder row, which predates content
    identity entirely;
  - a confirmed/manually-linked row that is orphaned (its stored key matched
    no fresh event) and could not be unambiguously paired with one either:
    left with its operator-owned fields untouched, but its own review_reason/
    last_seen_at ARE refreshed so an operator can see the post no longer
    yields it, rather than a silently stale row;
  - observing `EVENT_EXTRACTION_EVENTS_PER_POST`.

The ONE thing that genuinely differs between the two callers is per-event
VENUE ATTRIBUTION: a venue post already knows its venue (attribution is a
constant `venue_id`, and `location_resolution` is never touched); a promoter
post must run the resolution ladder on EACH event's own `location_text`,
never a sibling's. That is why `attribute` is the only caller-supplied hook —
see the plan's §B for why a second knob (e.g. "should confirmed events be
preserved?") would re-create the exact drift this refactor removes.
`attribute` is never invoked for a confirmed or manually-linked existing
row — attribution must never touch either.

`attribute` returns `(fields, on_persisted)`, not a bare dict: `fields` is
merged before the row is written, but any SIDE EFFECT that references the
event by id (the promoter path's `replace_event_venue_link_candidates`) must
run AFTER `insert_event`/`update_event`, never before — migration
0024_promoter_accounts declares `event_venue_link_candidate.event_id` as a
real, non-deferrable FK to `events.event`, so writing a candidate row for an
event that is not committed yet raises ForeignKeyViolation on real Postgres
for every first-time QUEUED or auto-linked event (the normal case). See
plans/260806_venue-post-multi-event.md's review.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from app.metrics import EVENT_EXTRACTION_EVENTS_PER_POST
from app.services.event_identity import compute_source_event_key, normalize_title
from app.services.event_venue_resolution import RESOLUTION_MANUAL
from app.services.pipeline_run_registry import new_run_id

logger = logging.getLogger(__name__)

STATUS_PENDING_REVIEW = "pending_review"
STATUS_CONFIRMED = "confirmed"
# Already defined (and unused) by 0023_event_table's status vocabulary —
# plans/260806_multi-event-posts.md was the first thing to write it: an event
# a later extraction no longer finds is superseded, never hard-deleted, and
# never touched at all once confirmed or manually linked.
STATUS_SUPERSEDED = "superseded"

# Flagged on a `confirmed` row whose title or resolved date no longer matches
# the model's fresh answer — the operator's record is never reverted, but the
# divergence must be visible. Originally the venue path's behaviour only
# (app.services.event_extraction_service._preserve_confirmed); the promoter
# path gains it by moving onto this shared module.
REVIEW_REASON_DIVERGES_FROM_CONFIRMED = "model_diverges_from_confirmed_record"

# Flagged on a confirmed/manually-linked row that is orphaned (no fresh event
# this run shares its key) and could not be unambiguously paired with one
# either (see `_plausibly_same_event`). NOT a divergence — there is no
# specific fresh answer to compare against, so nothing about the operator's
# record is contradicted — but silence is worse: an operator must be able to
# tell "this run's extraction no longer yields your confirmed/linked event"
# apart from "everything is still fine".
REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION = "confirmed_event_absent_from_latest_extraction"

# attribute(fields, event_id) -> (fields_to_merge, on_persisted). `fields`
# already carries every column this module and the caller have built for
# this event so far (identity/bookkeeping + the caller's prepared content),
# so the ladder can read e.g. `fields["location_text"]`. `event_id` is
# stable by the time this is called — the existing row's id, or a freshly
# minted one — so a callback that needs it never has to guess it.
# `fields_to_merge` is applied BEFORE the row is written. `on_persisted` (or
# None) is called with NO arguments AFTER the row is written — this is
# where a side effect that references the event by id belongs (the FK note
# above is why this is split at all).
AttributeFn = Callable[[dict, str], tuple[dict, Optional[Callable[[], None]]]]


def new_event_id() -> str:
    """A ULID: same time-ordered rationale as the archive run id — see
    app/services/pipeline_run_registry.new_run_id."""
    return f"evt_{new_run_id()}"


def _plausibly_same_event(existing: dict, prepared: dict) -> bool:
    """True when EXACTLY ONE of the two `source_event_key` components
    (normalized title, resolved calendar date) changed between `existing`
    and `prepared` — the signature of the SAME event drifting (the model
    re-phrased its title, or the event moved to a new date), never of an
    unrelated event replacing it.

    Uses `normalize_title` — the SAME normalization `compute_source_event_
    key` hashes — so this check and the key itself can never disagree about
    what counts as "the same title". `existing`/`prepared` reaching this
    function already have DIFFERENT keys (that is why one was orphaned and
    the other unmatched), so "neither changed" cannot occur here; the only
    real question is whether exactly one changed (pair) or both did (do
    not — see the caller for what happens then).
    """
    same_title = normalize_title(prepared.get("title")) == normalize_title(existing.get("title"))

    existing_starts_at = existing.get("starts_at")
    prepared_starts_at = prepared.get("starts_at")
    existing_date = existing_starts_at.date() if existing_starts_at is not None else None
    prepared_date = prepared_starts_at.date() if prepared_starts_at is not None else None
    # An unresolved date is not evidence of a MATCHING date — "unknown" must
    # never compare equal to "unknown". Two genuinely different dateless
    # events would otherwise look like "same date" and pair on title alone
    # differing, when really neither date says anything at all.
    same_date = existing_date is not None and prepared_date is not None and existing_date == prepared_date

    return same_title != same_date


def reconcile_post_events(
    *,
    venue_dao,
    source_kind: str,
    source_handle: str,
    source_shortcode: str,
    source_permalink: Optional[str],
    prepared_events: list[dict],
    now: datetime,
    attribute: AttributeFn,
) -> int:
    """Reconcile one post's freshly-extracted events against the rows already
    persisted for `(source_handle, source_shortcode)`. Returns the number of
    events persisted this call.

    `prepared_events` holds one dict per successfully-parsed event (a
    single-event post still passes a list of one). Each dict must already
    carry every persisted column EXCEPT:
      - the identity/bookkeeping fields this module injects itself
        (`source_event_key`, `source_event_index`, `source_kind`,
        `source_handle`, `source_shortcode`, `source_permalink`, `status`,
        `last_seen_at`, and `event_id`/`first_seen_at` on a fresh insert);
      - the attribution fields `attribute` supplies (`venue_id`,
        `location_resolution`, `location_confidence`, `linked_by`,
        `linked_at`).

    In particular each dict must include a resolved `starts_at` (or None)
    and `title` — the content-derived key and the confirmed-divergence check
    are computed from these two, never from list position or the model's raw
    date text — plus `raw_extraction`, built however the caller needs to
    (the venue path folds in `time_known`; the promoter path passes the
    model's parsed dict through unchanged). Date resolution itself
    (app.services.event_date_resolver.resolve_event_datetime) is intentionally
    NOT done here: both callers already call it identically, and several of
    its outputs (review_reason nuances, raw_extraction shape) legitimately
    stay caller-specific — see plans/260806_venue-post-multi-event.md.
    """
    existing_events = venue_dao.list_events_by_source(source_handle, source_shortcode)
    existing_by_key = {
        row["source_event_key"]: row for row in existing_events
        if row.get("source_event_key") is not None
    }
    handled_event_ids: set[str] = set()
    persisted = 0

    def _persist(existing: Optional[dict], prepared: dict, index: int, key: str) -> None:
        nonlocal persisted

        # A `confirmed` row is the operator's word: only raw_extraction and
        # last_seen_at move; every field the operator could have corrected —
        # including venue attribution — is left untouched. A divergence
        # between the model's FRESH answer and what the operator confirmed
        # is flagged via review_reason WITHOUT moving status away from
        # confirmed. Compares the freshly RESOLVED date (`prepared[
        # "starts_at"]`), never the model's raw date text. `source_event_key`
        # is kept in step with the model's fresh answer so a STABLE
        # divergence (the model keeps saying the same new thing) matches
        # directly next time, without needing the fallback pairing below.
        if existing is not None and existing.get("status") == STATUS_CONFIRMED:
            update_fields = {
                "raw_extraction": prepared.get("raw_extraction"),
                "last_seen_at": now,
                "source_event_key": key,
            }
            title_diverges = (prepared.get("title") or None) != (existing.get("title") or None)
            date_diverges = prepared.get("starts_at") != existing.get("starts_at")
            if title_diverges or date_diverges:
                update_fields["review_reason"] = REVIEW_REASON_DIVERGES_FROM_CONFIRMED
            venue_dao.update_event(existing["event_id"], update_fields)
            handled_event_ids.add(existing["event_id"])
            persisted += 1
            return

        # A manual link outranks the model too: a later run of the same post
        # must never move venue_id/location_resolution/location_confidence/
        # linked_by/linked_at once an operator has set them for THIS event.
        # `attribute` is simply never invoked, so none of those columns ever
        # appear in `fields` below — on an update that leaves them exactly
        # as they were (the DAO's partial-update contract). A fresh row can
        # never be pre-existing-manual in the first place.
        preserve_manual_link = (
            existing is not None and existing.get("location_resolution") == RESOLUTION_MANUAL
        )

        fields = {
            "source_kind": source_kind,
            "source_handle": source_handle,
            "source_shortcode": source_shortcode,
            "source_permalink": source_permalink,
            "source_event_key": key,
            "source_event_index": index,
            "status": STATUS_PENDING_REVIEW,
            "last_seen_at": now,
        }
        fields.update(prepared)

        if existing is None:
            event_id = new_event_id()
            fields["event_id"] = event_id
            fields["first_seen_at"] = now
        else:
            event_id = existing["event_id"]
            handled_event_ids.add(event_id)

        # `attribute` returns (fields_to_merge, on_persisted): the fields
        # are merged NOW (needed for the insert/update below), but
        # `on_persisted` — the promoter path's candidate-row write — must
        # not run until AFTER the event row itself is committed.
        # event_venue_link_candidate.event_id is a real, non-deferrable FK
        # to events.event (migration 0024_promoter_accounts); calling it
        # before insert_event raises ForeignKeyViolation on real Postgres
        # for every first-time QUEUED or auto-linked event — the normal
        # case, not an edge case. See plans/260806_venue-post-multi-
        # event.md's review.
        on_persisted: Optional[Callable[[], None]] = None
        if not preserve_manual_link:
            attribution_fields, on_persisted = attribute(fields, event_id)
            fields.update(attribution_fields)

        if existing is None:
            # A fresh row has no prior value for the four link columns to
            # fall back on — unlike an update, where omitting them from
            # `fields` correctly leaves whatever was already stored (NULL,
            # from that same row's own first insert) untouched. The real
            # store's column default covers this for an insert; the
            # in-memory fake needs it explicit.
            fields.setdefault("venue_id", None)
            fields.setdefault("location_resolution", None)
            fields.setdefault("location_confidence", None)
            fields.setdefault("linked_by", None)
            fields.setdefault("linked_at", None)
            venue_dao.insert_event(fields)
        else:
            venue_dao.update_event(event_id, fields)

        if on_persisted is not None:
            on_persisted()

        persisted += 1

    unmatched: list[tuple[int, dict, str]] = []
    seen_keys_this_run: set[str] = set()
    for index, prepared in enumerate(prepared_events, start=1):
        key = compute_source_event_key(prepared.get("title"), prepared.get("starts_at"))
        if key in seen_keys_this_run:
            # compute_source_event_key's own docstring already names this:
            # two events in the SAME post with the same normalized title and
            # the same resolved date collapse onto one key — there is no
            # stronger identity signal available. Persisting a second row
            # for it would violate the real (source_handle, source_shortcode,
            # source_event_key) UNIQUE constraint (uq_event_source_key), so
            # the duplicate is skipped rather than crashing the whole post;
            # the first occurrence already represents this key for this run.
            logger.warning(
                f"[EventReconciliation] duplicate source_event_key within "
                f"one post ({source_handle}/{source_shortcode}); skipping a "
                f"second row for title={prepared.get('title')!r}"
            )
            continue
        seen_keys_this_run.add(key)
        existing = existing_by_key.get(key)
        if existing is None:
            unmatched.append((index, prepared, key))
            continue
        _persist(existing, prepared, index, key)

    # A confirmed or manually-linked row whose stored key matched nothing
    # this run is not necessarily evidence a DIFFERENT event replaced it —
    # the model's OWN answer for that same event may simply have moved
    # enough (a new title, a new date) to change the content-derived key,
    # which is exactly what a divergence flag exists to catch. Only pair
    # when it is UNAMBIGUOUS (exactly one orphaned protected row and exactly
    # one unmatched fresh event) — with more than one on either side there is
    # no principled way to guess which fresh event corresponds to which
    # orphaned row, so they are left to the normal insert/supersede paths
    # below instead of risking a wrong pairing.
    #
    # Cardinality alone is not enough, though: a confirmed event genuinely
    # REPLACED by an unrelated one is ALSO "exactly one orphaned, exactly one
    # unmatched" — nothing about the counts distinguishes "the same event
    # moved" from "a different event arrived while the old one vanished".
    # `_plausibly_same_event` is the extra check: pair only when EXACTLY ONE
    # of the two key components changed (same title/different date, or same
    # date/different title) — the signature of the SAME event drifting.
    # When BOTH changed, they are treated as two unrelated events: the
    # protected row is left alone (already exempt from supersession below)
    # and the fresh event is inserted as its own row instead of being
    # silently absorbed into the confirmed/manual row and lost.
    orphaned_protected = [
        row for row in existing_events
        if row["event_id"] not in handled_event_ids
        and (row.get("status") == STATUS_CONFIRMED or row.get("location_resolution") == RESOLUTION_MANUAL)
    ]
    if len(orphaned_protected) == 1 and len(unmatched) == 1:
        candidate = orphaned_protected[0]
        index, prepared, key = unmatched[0]
        if _plausibly_same_event(candidate, prepared):
            _persist(candidate, prepared, index, key)
            unmatched = []

    for index, prepared, key in unmatched:
        _persist(None, prepared, index, key)

    # An event previously extracted from this post and absent from THIS run
    # is superseded — never hard-deleted, and never touched at all once
    # confirmed or manually linked (the operator outranks the model, exactly
    # as the confirmed branch above already behaves). A row with NO
    # source_event_key at all is an extraction_failed placeholder that
    # predates content identity entirely (see
    # EventExtractionService._record_failure / PromoterCrawlService.
    # _record_failure) — it was never a candidate for THIS run's events and
    # must never be flipped to superseded by their mere presence.
    for row in existing_events:
        if row["event_id"] in handled_event_ids:
            continue
        if row.get("source_event_key") is None:
            continue
        is_protected = (
            row.get("status") == STATUS_CONFIRMED
            or row.get("location_resolution") == RESOLUTION_MANUAL
        )
        if is_protected:
            # Orphaned AND not unambiguously pairable with a fresh event
            # (see above) — never claim a divergence from a specific answer
            # that does not exist, and never move status or any
            # operator-owned field. But silence would mean the operator
            # never learns "the post no longer yields your confirmed/linked
            # event" — so review_reason and last_seen_at are refreshed to
            # say exactly that, and nothing else.
            venue_dao.update_event(row["event_id"], {
                "review_reason": REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION,
                "last_seen_at": now,
            })
            continue
        venue_dao.update_event(row["event_id"], {"status": STATUS_SUPERSEDED})

    # A post collapsing back to fewer events than it used to is exactly the
    # regression this feature exists to prevent — visible only in this
    # distribution, never in a single scalar. Now observed for venue posts
    # too, not just promoter roundups.
    EVENT_EXTRACTION_EVENTS_PER_POST.observe(len(prepared_events))
    return persisted


__all__ = [
    "reconcile_post_events", "new_event_id",
    "STATUS_PENDING_REVIEW", "STATUS_CONFIRMED", "STATUS_SUPERSEDED",
    "REVIEW_REASON_DIVERGES_FROM_CONFIRMED", "REVIEW_REASON_ABSENT_FROM_LATEST_EXTRACTION",
]
