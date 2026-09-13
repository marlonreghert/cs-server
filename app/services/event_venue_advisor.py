"""The conditional, fully-inert live hook —
plans/260913_dedup-agentic-mitigation-discovery.md Phase 4.

Gated `False` by default (`event_venue_advisor_enabled`, see
`ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY` below). Ships this way
regardless of whether this cycle's measurement (Phase 2) recommends turning
it on — see the plan's own "ships fully inert" instruction and this
repository's standing rule that a new behaviour must provably change nothing
until an operator deliberately flips it.

## Where this sits, and why

`app.services.event_display_title.EventDisplayTitleService` is the one
existing precedent for an LLM anywhere in this pipeline, and its own
docstring states the structural reason this module copies exactly: the
resolution ladder (`app.services.event_venue_resolution.resolve_event_venue`)
is SYNCHRONOUS and `OpenAIEventExtractionClient` is ASYNC, so a model call
cannot live inside `resolve_event_venue`/`evaluate_pair`/the reconciliation
path without a large refactor of code three migrations already replay. That
constraint is a gift, not a limitation: it forces the model out of the
decision path entirely. This module runs as a SEPARATE pass, after
reconciliation has already decided (and persisted) an event's resolution
state, over events a run touched — the exact same shape
`EventDisplayTitleService.run_for_events` already takes.

## What it will and will never do

- Fires ONLY for an event sitting at `RESOLUTION_QUEUED` — `location_
  resolution IS NULL` and `venue_id IS NULL` (see
  `event_venue_resolution.py`'s own docstring: "'Awaiting a decision' is the
  ABSENCE of one"), with a non-empty closed candidate set the deterministic
  ladder already ranked (`event_venue_link_candidate` rows). An event the
  ladder already resolved automatically (`RESOLUTION_AUTO`, `venue_id` set)
  is never even considered — this module never re-opens a decision the
  deterministic code already made.
- Never sets `venue_id`. Never sets `location_resolution`. Never calls
  `merge_touched_events` or anything in `app.services.event_merge`. The
  ONLY write this module ever performs is
  `venue_dao.set_event_venue_link_candidate_recommendation(...)` — one
  nullable `jsonb` column (migration `0047`) on an EXISTING candidate row.
- Every recommendation is put through `app.services.
  event_venue_advisor_validator.validate_event_venue_recommendation` before
  anything is written — the closed-candidate-set + verbatim-evidence gate.
  A rejected or malformed answer writes nothing, exactly like
  `event_display_title.validate_display_title`'s own "on ANY failure ...
  nothing is written" posture.
- NEVER raises. An OpenAI failure, a validator rejection, or a missing
  client is caught, counted (`EVENT_VENUE_ADVISOR_OUTCOME_TOTAL`), logged,
  and the event is left exactly as the deterministic ladder produced it —
  the identical degrade-gracefully contract `EventDisplayTitleService.
  run_for_events` already honours for its own caller.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from app.metrics import EVENT_VENUE_ADVISOR_OUTCOME_TOTAL
from app.services.event_dedup import _load_validated_config  # noqa: F401 — the SAME validated-read path every other key in this plan uses; never coerced.
from app.services.event_venue_advisor_validator import (
    VenueCandidate,
    validate_event_venue_recommendation,
)

logger = logging.getLogger(__name__)

ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY = "admin_config:event_venue_advisor_enabled"
# OFF by default, like every other flag in this plan and its 260912
# predecessor: the deploy of this branch must provably change no stored row,
# and this pass WRITES one (the advisory column alone — see module
# docstring for the full list of things it never touches).
DEFAULT_EVENT_VENUE_ADVISOR_ENABLED = False

OUTCOME_SUGGESTED = "suggested"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"


def validate_event_venue_advisor_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("event venue advisor flag must be a boolean")
    return value


def load_event_venue_advisor_enabled(redis_like) -> bool:
    return _load_validated_config(
        redis_like, ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY,
        DEFAULT_EVENT_VENUE_ADVISOR_ENABLED,
        validator=validate_event_venue_advisor_enabled_config,
        module_tag="event_venue_advisor",
    )


def parse_event_venue_advisor_response(raw_text) -> Optional[dict]:
    """`{"venue_id": str|None, "evidence_quote": str|None}` -> the dict, or
    `None` for anything unparseable. Mirrors
    `event_display_title.parse_display_title_response`'s posture: forgiving
    of SHAPE (a markdown-fenced JSON block, say) but never of content — a
    malformed reply is a rejection at the validator, never a partial
    write."""
    if not raw_text:
        return None
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _is_resolution_queued(row: dict) -> bool:
    """`event_venue_resolution.py`'s own definition, restated here because
    `RESOLUTION_QUEUED` is a caller-side SENTINEL, never a stored column
    value (see that module's own comment: "'Awaiting a decision' is the
    ABSENCE of one (location_resolution IS NULL) ... never written to the
    column"). A row this module may consider is therefore exactly one with
    both link columns still unset."""
    return row.get("location_resolution") is None and row.get("venue_id") is None


class EventVenueAdvisorService:
    """The post-reconciliation pass. Walks the event ids a run touched and
    attaches a validated recommendation to any that are still
    `RESOLUTION_QUEUED` with a non-empty candidate set.

    At most ONE model call per qualifying EVENT — never per candidate, never
    per post — and zero calls for an event the ladder already resolved
    automatically, an event with no candidates at all, or any event once the
    admin-config flag is off (checked ONCE per `run_for_events` call, the
    same short-circuit `EventDisplayTitleService.run_for_events` takes)."""

    def __init__(self, venue_dao, openai_client, *, redis_client=None):
        self.venue_dao = venue_dao
        self.openai_client = openai_client
        self.redis_client = redis_client
        # One `get_venue` per VENUE, not per candidate row — the same
        # `_venue_name_cache` shape `EventDisplayTitleService` and
        # `scripts/measure_event_dedup.measure` both already use.
        self._venue_name_cache: dict = {}

    def _venue_name(self, venue_id: Optional[str]) -> Optional[str]:
        if not venue_id:
            return None
        if venue_id in self._venue_name_cache:
            return self._venue_name_cache[venue_id]
        venue = self.venue_dao.get_venue(venue_id)
        if venue is None:
            name = None
        elif isinstance(venue, dict):
            name = venue.get("venue_name")
        else:
            name = getattr(venue, "venue_name", None)
        self._venue_name_cache[venue_id] = name
        return name

    async def run_for_events(self, event_ids) -> dict:
        """Returns a small outcome-count dict for the caller's own report.
        NEVER raises: an OpenAI failure or a validator rejection in this
        pass must not fail the extraction run that triggered it."""
        if not load_event_venue_advisor_enabled(self.redis_client):
            return {}

        counts: dict = {}

        def _bump(outcome: str) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1
            EVENT_VENUE_ADVISOR_OUTCOME_TOTAL.labels(outcome=outcome).inc()

        for event_id in dict.fromkeys(event_ids):
            row = self.venue_dao.get_event(event_id)
            if row is None or row.get("status") == "superseded":
                continue
            if not _is_resolution_queued(row):
                # Covers RESOLUTION_AUTO (venue_id set) and RESOLUTION_
                # UNRESOLVED (no candidates worth ranking) alike — this
                # module never re-opens either.
                continue

            candidate_rows = self.venue_dao.list_event_venue_link_candidates(event_id)
            if not candidate_rows:
                continue

            await self._advise_one(row, candidate_rows, bump=_bump)
        return counts

    async def _advise_one(self, row: dict, candidate_rows: list, *, bump) -> None:
        if self.openai_client is None:
            bump(OUTCOME_ERROR)
            return

        candidates = [
            VenueCandidate(
                venue_id=c["venue_id"], venue_name=self._venue_name(c["venue_id"]),
            )
            for c in candidate_rows
        ]
        location_text = row.get("location_text")
        # The post CAPTION is never persisted anywhere on `events.post_item`
        # or `events.post_item_source` (confirmed: neither table carries a
        # `caption` column) — it is ephemeral, used only synchronously
        # during extraction/reconciliation. This pass runs strictly AFTER
        # reconciliation has already persisted and moved on, so it has no
        # caption to read, exactly the same constraint `app.services.
        # event_attribution_dispute.evaluate_attribution_dispute` documents
        # for passing `caption=None` into the same ladder. The validator and
        # the prompt both still accept a caption parameter (`None` here) so
        # a future caller with caption in hand — e.g. a live, synchronous
        # call site — can supply it without a signature change.
        caption = None

        try:
            raw = await self.openai_client.recommend_event_venue(
                location_text=location_text, caption=caption,
                candidates=[
                    {"venue_id": c.venue_id, "venue_name": c.venue_name} for c in candidates
                ],
            )
        except Exception as e:
            # An availability problem, never a data problem: nothing is
            # written and the event stays exactly as the deterministic
            # ladder left it.
            logger.warning(
                f"[EventVenueAdvisor] recommendation call failed for {row['event_id']}: {e}"
            )
            bump(OUTCOME_ERROR)
            return

        recommendation = parse_event_venue_advisor_response(raw)
        verdict = validate_event_venue_recommendation(
            candidates, recommendation, location_text=location_text, caption=caption,
        )
        if not verdict.accepted:
            # Log the rejection REASON (never the raw model payload) — a
            # climbing `rejected` means the gate is doing its job and the
            # prompt needs work, matching event_display_title's own
            # discipline.
            logger.info(
                "[EventVenueAdvisor] rejected recommendation for %s: %s",
                row["event_id"], verdict.rejection_reason,
            )
            bump(OUTCOME_REJECTED)
            return

        self.venue_dao.set_event_venue_link_candidate_recommendation(
            row["event_id"], verdict.venue_id,
            {"venue_id": verdict.venue_id, "evidence_quote": verdict.evidence_quote},
        )
        bump(OUTCOME_SUGGESTED)


__all__ = [
    "ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY", "DEFAULT_EVENT_VENUE_ADVISOR_ENABLED",
    "OUTCOME_SUGGESTED", "OUTCOME_REJECTED", "OUTCOME_ERROR",
    "validate_event_venue_advisor_enabled_config", "load_event_venue_advisor_enabled",
    "parse_event_venue_advisor_response", "EventVenueAdvisorService",
]
