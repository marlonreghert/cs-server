"""The audit-sourced, address-aware reviewer —
plans/260914_agentic-venue-resolution-fallback.md.

Takes its input from `app.services.venue_link_audit.collect_venue_link_audit`'s
own flagged `(handle, venue)` pairs, gives a model the venue's real STREET
alongside its name and neighbourhood, and lets a sufficiently-agreed verdict
act: a `confirm` suppresses the flag, a `contradict` keeps it flagged and
records why.

## Why the input is the audit, not an extraction run

`EventVenueAdvisorService` can only ever see an event that is
`RESOLUTION_QUEUED`, was touched by the extraction run that just finished,
AND carries at least one `events.event_venue_link_candidate` row. Measured
live on 2026-09-14 (plan §B): **8 of the 9 originally-named handles have ZERO
candidate rows**, because a single-mapped `kind='venue'` crawl target
force-assigns every event in `EventExtractionService._extract_one`'s default
closure and never calls `resolve_event_venue` at all — so `_on_persisted`'s
`replace_event_venue_link_candidates` never runs. A pass built on that table
would do literally nothing for those handles no matter how rich its prompt
got. This pass therefore reads the audit's output instead.

The candidate set here is the handle's own CURRENTLY-MAPPED venue(s) — the
identical closed set `compute_venue_link_audit` already compares each event
against, and strictly narrower than the ladder's top-20. The catalog is never
searched.

## What this module will and will never do

- It NEVER writes `venue_id`, `location_resolution`, `linked_by`,
  `linked_at`, `status` or `review_reason` on any `events.post_item` row. Not
  a limitation — a finding: across all 12 live flagged pairs, **zero** call
  for a reattribution (plan §E). Seven are "the mapping is right and the
  audit's textual check cannot see the street", one is a correct decline, one
  is a correct refusal that needs a new catalogue venue added by a human.
- Its ONLY write is one `events.venue_link_audit_review` row per pair it
  looked at (migration `0048`).
- It never acts on a handle mapped to more than one venue. Plan §F item 3:
  both `entreamigosobode` siblings returned `confirm` **10/10** on a shared,
  mutually-exclusive pool of 12 events, one citing house number 30 for a
  venue catalogued at 50. Sampling cannot detect that; only this structural
  rule can.
- Only `confirm` closes a flag. The risks are asymmetric — a wrong `confirm`
  HIDES a real defect, a wrong `contradict` merely adds noise to a queue an
  operator already reads — so the flag-closing verdict carries the strict
  gate and `contradict` never suppresses anything.
- It NEVER raises into its caller, matching
  `EventVenueAdvisorService.run_for_events`'s own contract.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.metrics import VENUE_LINK_AUDIT_REVIEW_OUTCOME_TOTAL
from app.services.event_dedup import _load_validated_config  # noqa: F401 — the SAME validated-read path every sibling admin-config key in this pipeline uses; never coerced (`bool("false")` -> True is the trap it exists to prevent).
from app.services.event_link_skip import skip_reason  # the ONE expression of the skip table (plan §1) — `superseded` is one of its own six reasons, so this module must not restate it.
from app.services.venue_link_audit import venue_corroborates_location_text
from app.services.venue_link_audit_reviewer_validator import (
    VERDICT_CONFIRM,
    VERDICT_CONTRADICT,
    VERDICT_INSUFFICIENT,
    ReviewAnswer,
    validate_venue_link_review,
)

logger = logging.getLogger(__name__)

ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY = (
    "admin_config:venue_link_audit_reviewer_enabled"
)
ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY = (
    "admin_config:venue_link_audit_reviewer_auto_apply_enabled"
)
# BOTH off by default, like every sibling flag in this pipeline. Two flags
# rather than one deliberately (plan, Data/Config/API Impact): the risk here
# is a write that HIDES a real defect, so the pass must be runnable in shadow
# mode — producing a full, inspectable verdict log against live data — before
# it is ever allowed to close anything.
DEFAULT_VENUE_LINK_AUDIT_REVIEWER_ENABLED = False
DEFAULT_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_ENABLED = False

# Ten independent calls, all ten agreeing. Decided over the K=5 the original
# measurement recommended: at this document volume (12 flagged pairs,
# operator-triggered, not a continuous sweep) the extra spend is negligible,
# and plan §F measured `casabacurau` at 9/10 — meaning K=5 has a real chance
# of reading a mixed-evidence pair as unanimous and missing a hold-back it
# should have caught. K=10 does not miss it. Lower this only with a fresh
# measurement, never to save time.
DEFAULT_REVIEW_CONSENSUS_K = 10

# The floor below which a verdict may be RECORDED but must never SUPPRESS a
# flag. The plan's central instruction is that this feature must not ship a
# single-call auto-apply, and `consensus_k` is a constructor argument the
# runner exposes as `--consensus-k` — so without this floor, one CLI flag
# (`--consensus-k 1`) would quietly reduce the whole consensus design to the
# single unstable call it exists to prevent.
#
# Set at 5, the smallest K the production measurement (plan §F) actually
# exercised: at K=5 all three mixed-evidence pairs were still held back. A
# smaller K is deliberately still ALLOWED for tests and for cheap shadow
# runs — it simply cannot close a flag. `auto_apply` is ANDed with this in
# `run()`, so the effect is "record, never act", not a hard failure.
MIN_REVIEW_CONSENSUS_K = 5

OUTCOME_CONFIRMED = "confirmed"
OUTCOME_CONTRADICTED = "contradicted"
OUTCOME_INSUFFICIENT = "insufficient"
OUTCOME_NO_CONSENSUS = "no_consensus"
OUTCOME_SKIPPED_MULTI_MAPPED = "skipped_multi_mapped"
OUTCOME_SKIPPED_OPERATOR_DECIDED = "skipped_operator_decided"
OUTCOME_SKIPPED_ALL_EVENTS_PROTECTED = "skipped_all_events_protected"
OUTCOME_VALIDATOR_REJECTED = "validator_rejected"
# Its OWN outcome, never folded into `validator_rejected`. Plan §F: an empty
# response from a reasoning model whose `max_completion_tokens` is too small
# parses to `None`, which the existing advisor's validator records as
# `malformed` and its service counts as `rejected` — indistinguishable, on a
# dashboard, from the gate correctly refusing a bad answer. That confound hid
# a real configuration bug for a full cycle. It cannot recur here.
OUTCOME_EMPTY_RESPONSE = "empty_response"
OUTCOME_ERROR = "error"

# Operator decisions on a recorded review.
DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
DECISIONS = (DECISION_ACCEPTED, DECISION_REJECTED)


def validate_venue_link_audit_reviewer_enabled_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("venue link audit reviewer flag must be a boolean")
    return value


def validate_venue_link_audit_reviewer_auto_apply_config(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("venue link audit reviewer auto-apply flag must be a boolean")
    return value


def load_venue_link_audit_reviewer_enabled(redis_like) -> bool:
    return _load_validated_config(
        redis_like, ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY,
        DEFAULT_VENUE_LINK_AUDIT_REVIEWER_ENABLED,
        validator=validate_venue_link_audit_reviewer_enabled_config,
        module_tag="venue_link_audit_reviewer",
    )


def load_venue_link_audit_reviewer_auto_apply_enabled(redis_like) -> bool:
    return _load_validated_config(
        redis_like, ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY,
        DEFAULT_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_ENABLED,
        validator=validate_venue_link_audit_reviewer_auto_apply_config,
        module_tag="venue_link_audit_reviewer",
    )


def parse_venue_link_review_response(raw_text) -> Optional[dict]:
    """`{"verdict", "evidence_quote", "matched_field", "reason"}` -> the dict,
    or `None` for anything unparseable. Same forgiving-of-SHAPE /
    never-of-content posture as `event_venue_advisor.parse_event_venue_
    advisor_response`: a markdown-fenced block is unwrapped, a malformed reply
    is a rejection at the validator, never a partial write.

    An EMPTY reply returns `None` here too — the caller distinguishes the two
    cases by checking the raw text itself, so it can count
    `OUTCOME_EMPTY_RESPONSE` separately (see that constant's comment)."""
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


# ── the pure core (every rule lives here; no DAO, no Redis, no client) ──────

@dataclass(frozen=True)
class ReviewEvidence:
    """Everything the model is shown about one flagged pair, and nothing
    else. `location_texts` is the ONLY valid haystack for the verbatim gate —
    it is exactly `corroborating + non_corroborating`."""

    handle: str
    venue_id: str
    venue_name: Optional[str]
    street: Optional[str]
    neighborhood: Optional[str]
    city: Optional[str]
    siblings: tuple = ()
    corroborating: tuple = ()
    non_corroborating: tuple = ()
    protected_event_count: int = 0

    @property
    def location_texts(self) -> list:
        return list(self.corroborating) + list(self.non_corroborating)

    def to_dict(self) -> dict:
        return {
            "handle": self.handle, "venue_id": self.venue_id,
            "venue_name": self.venue_name, "street": self.street,
            "neighborhood": self.neighborhood, "city": self.city,
            "siblings": [dict(s) for s in self.siblings],
            "corroborating": list(self.corroborating),
            "non_corroborating": list(self.non_corroborating),
            "protected_event_count": self.protected_event_count,
        }


@dataclass
class PairOutcome:
    """One pair's result. `verdict` is set only when `outcome` is one of the
    three verdict outcomes; a skipped or failed pair carries `None`."""

    handle: str
    venue_id: str
    outcome: str
    verdict: Optional[str] = None
    evidence_quote: Optional[str] = None
    matched_field: Optional[str] = None
    reason: Optional[str] = None
    tally: dict = field(default_factory=dict)
    checkable_count: int = 0
    non_corroborating_count: int = 0
    suppressed: bool = False
    # False for a pair this run only OBSERVED rather than decided — one
    # already confirmed and still in force, or one an operator has decided.
    # Such a pair must not be re-written: re-persisting it while
    # `auto_apply` happened to be off would store a NULL
    # `non_corroborating_count_at_review` over a live one and silently
    # DESTROY an existing suppression, so merely turning the auto-apply flag
    # off for one run would re-open every previously-confirmed pair. Nothing
    # changed, so nothing is written.
    persist: bool = True

    def to_dict(self) -> dict:
        return {
            "handle": self.handle, "venue_id": self.venue_id,
            "outcome": self.outcome, "verdict": self.verdict,
            "evidence_quote": self.evidence_quote,
            "matched_field": self.matched_field, "reason": self.reason,
            "tally": dict(self.tally),
            "checkable_count": self.checkable_count,
            "non_corroborating_count": self.non_corroborating_count,
            "suppressed": self.suppressed,
            "persist": self.persist,
        }


def build_review_evidence(
    *, handle: str, venue: dict, siblings: list, events: list,
) -> ReviewEvidence:
    """PURE. Split one pair's checkable events into the texts that already
    corroborate the venue and the texts that do not, after removing every row
    the skip table protects.

    `venue`: `{"venue_id", "venue_name", "street", "neighborhood", "city"}`.
    `siblings`: the handle's OTHER mapped venues, same shape, shown to the
    model as labeled context only.
    `events`: the handle's live event rows.

    The checkable filter is `compute_venue_link_audit`'s own — an event
    already resolved to a DIFFERENT venue is a closed case and is not
    evidence about this mapping either way. The skip-table filter is on top
    of it: an event an operator confirmed, hand-linked, or whose venue they
    edited is never shown to the model and never counted."""
    corroborating: list = []
    non_corroborating: list = []
    protected = 0
    for row in events or []:
        if row.get("venue_id") not in (None, venue["venue_id"]):
            continue
        # `superseded` is NOT checked separately ahead of this: it is one of
        # the skip table's own six reasons (`SKIP_SUPERSEDED`), and plan
        # Desired Behavior §3 requires "reusing the repository's single
        # existing expression of those rules rather than restating them".
        # A standalone `status == STATUS_SUPERSEDED` branch above this one
        # restated the rule AND bypassed the counter, so a superseded row
        # silently vanished from `protected_event_count` while all five of
        # its siblings were counted.
        if skip_reason(row) is not None:
            protected += 1
            continue
        verdict = venue_corroborates_location_text(
            venue.get("venue_name"), venue.get("neighborhood"),
            row.get("location_text"),
        )
        if verdict is None:
            continue
        text = (row.get("location_text") or "").strip()
        (corroborating if verdict else non_corroborating).append(text)

    return ReviewEvidence(
        handle=handle, venue_id=venue["venue_id"],
        venue_name=venue.get("venue_name"), street=venue.get("street"),
        neighborhood=venue.get("neighborhood"), city=venue.get("city"),
        siblings=tuple(
            {
                "venue_name": s.get("venue_name"), "street": s.get("street"),
                "neighborhood": s.get("neighborhood"),
            } for s in (siblings or [])
        ),
        # De-duplicated: the model gains nothing from the same spelling twelve
        # times, and the prompt stays bounded regardless of a handle's volume.
        corroborating=tuple(dict.fromkeys(corroborating)),
        non_corroborating=tuple(dict.fromkeys(non_corroborating)),
        protected_event_count=protected,
    )


def tally_consensus(verdicts: list, *, k: int) -> tuple:
    """PURE. `(outcome, verdict)` for one pair's K validated answers.

    Unanimity is required over EXACTLY `k` accepted answers: a run that
    produced fewer (a call failed, or the validator rejected one) can never
    reach consensus, which is what makes "all ten agreeing, all ten
    validating" a single check rather than two that could drift apart."""
    if not verdicts or len(verdicts) != k:
        return OUTCOME_NO_CONSENSUS, None
    distinct = set(verdicts)
    if len(distinct) != 1:
        return OUTCOME_NO_CONSENSUS, None
    verdict = distinct.pop()
    if verdict == VERDICT_CONFIRM:
        return OUTCOME_CONFIRMED, VERDICT_CONFIRM
    if verdict == VERDICT_CONTRADICT:
        return OUTCOME_CONTRADICTED, VERDICT_CONTRADICT
    return OUTCOME_INSUFFICIENT, VERDICT_INSUFFICIENT


def review_suppresses_pair(review: Optional[dict], *, non_corroborating_count: int) -> bool:
    """PURE. Does a stored review currently close this pair's flag?

    Three conditions, all required:
    - the verdict is `confirm` (only the flag-closing verdict suppresses);
    - an operator has not REJECTED it (a rejection is permanent — the pair is
      never suppressed and never re-reviewed again);
    - the pair's evidence has not grown since the review. New
      non-corroborating texts re-open the pair on their own, with no operator
      action and no job — the staleness rule."""
    if not review:
        return False
    if review.get("operator_decision") == DECISION_REJECTED:
        return False
    if review.get("verdict") != VERDICT_CONFIRM:
        return False
    at_review = review.get("non_corroborating_count_at_review")
    if at_review is None:
        return False
    return non_corroborating_count <= at_review


def apply_reviews_to_audit(candidates: list, reviews: dict) -> list:
    """PURE. Fold stored reviews into `collect_venue_link_audit`'s output.

    `candidates`: `VenueLinkAuditCandidate` objects (or their `.to_dict()`
    form). `reviews`: `{(handle, venue_id): review_row}`.

    Returns plain dicts — a suppressed venue is dropped from `mapped_venues`,
    a handle left with no flagged venue is dropped entirely, and every
    surviving venue carries an additive `review` sub-object so a still-flagged
    pair shows its machine verdict inline. Never mutates its input: the
    audit's own dataclasses are frozen by design."""
    out: list = []
    for candidate in candidates or []:
        data = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate)
        kept: list = []
        for venue in data.get("mapped_venues") or []:
            review = reviews.get((data["handle"], venue["venue_id"]))
            if venue.get("flagged") and review_suppresses_pair(
                review, non_corroborating_count=venue.get("non_corroborating_count", 0),
            ):
                continue
            venue = dict(venue)
            venue["review"] = _review_out(review)
            kept.append(venue)
        if not any(v.get("flagged") for v in kept):
            continue
        data["mapped_venues"] = kept
        out.append(data)
    return out


def _review_out(review: Optional[dict]) -> Optional[dict]:
    """The additive `review` sub-object. `None` when the pair has never been
    reviewed — an absent review and a review that decided nothing are
    different states and must stay distinguishable."""
    if not review:
        return None
    return {
        "verdict": review.get("verdict"),
        "outcome": review.get("outcome"),
        "evidence_quote": review.get("evidence_quote"),
        "matched_field": review.get("matched_field"),
        "reason": review.get("reason"),
        "operator_decision": review.get("operator_decision"),
        "reviewed_at": review.get("updated_at") or review.get("created_at"),
    }


# ── the adapter (fetch, call, tally, persist — holds no rules of its own) ───

class VenueLinkAuditReviewerService:
    """The audit-sourced pass. One `(handle, venue)` pair at a time, K model
    calls each, at most one `events.venue_link_audit_review` row written each.

    Never raises: an OpenAI failure, a validator rejection or a DB error on
    one pair is caught, counted, logged, and leaves that pair exactly as the
    audit produced it — the identical degrade-gracefully contract
    `EventVenueAdvisorService.run_for_events` already honours."""

    def __init__(
        self, venue_dao, review_client, *, redis_client=None,
        consensus_k: int = DEFAULT_REVIEW_CONSENSUS_K, concurrency: int = 5,
    ):
        self.venue_dao = venue_dao
        self.review_client = review_client
        self.redis_client = redis_client
        self.consensus_k = consensus_k
        self.concurrency = concurrency

    async def run(self, *, handles=None, dry_run: bool = False) -> dict:
        """Returns `{"outcomes": [PairOutcome.to_dict(), ...], "counts": {...}}`.

        `dry_run=True` makes every model call and reaches every verdict but
        writes nothing — the shape the runner script uses by default, so the
        first production run is a measurement an operator reads before
        anything is ever suppressed.

        The enabled flag is checked ONCE here, not per pair, and decides
        whether the work runs AT ALL — no model call and no row read when it
        is off."""
        import asyncio

        from app.services.venue_link_audit import collect_venue_link_audit

        if not load_venue_link_audit_reviewer_enabled(self.redis_client):
            return {"outcomes": [], "counts": {}, "enabled": False}

        auto_apply = load_venue_link_audit_reviewer_auto_apply_enabled(self.redis_client)
        if auto_apply and self.consensus_k < MIN_REVIEW_CONSENSUS_K:
            # Record, never act — see MIN_REVIEW_CONSENSUS_K. A K this small
            # cannot be allowed to close a flag no matter what the flag says,
            # because "unanimous" over one or two calls is not consensus.
            logger.warning(
                "[VenueLinkAuditReviewer] consensus_k=%s is below the "
                "MIN_REVIEW_CONSENSUS_K floor of %s; verdicts will be "
                "recorded but no flag will be suppressed",
                self.consensus_k, MIN_REVIEW_CONSENSUS_K,
            )
            auto_apply = False

        candidates = collect_venue_link_audit(self.venue_dao) or []
        if handles:
            wanted = set(handles)
            candidates = [c for c in candidates if c.handle in wanted]

        reviews = self._existing_reviews()
        counts: dict = {}
        outcomes: list = []

        def _bump(outcome: str) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1
            VENUE_LINK_AUDIT_REVIEW_OUTCOME_TOTAL.labels(outcome=outcome).inc()

        semaphore = asyncio.Semaphore(self.concurrency)
        for candidate in candidates:
            mapped = list(candidate.mapped_venues)
            # The handle's FULL mapped set decides multi-mapped-ness, not just
            # its flagged subset: two siblings compete over the same shared
            # event pool whether or not both happen to be flagged.
            multi_mapped = len(mapped) > 1
            addresses = self._addresses([v.venue_id for v in mapped])
            for audited in mapped:
                if not audited.flagged:
                    continue
                outcome = await self._review_one(
                    candidate=candidate, audited=audited, mapped=mapped,
                    addresses=addresses, reviews=reviews,
                    multi_mapped=multi_mapped, semaphore=semaphore,
                    auto_apply=auto_apply,
                )
                _bump(outcome.outcome)
                outcomes.append(outcome.to_dict())
                if not dry_run and outcome.persist:
                    self._persist(outcome, auto_apply=auto_apply)
        return {
            "outcomes": outcomes, "counts": counts, "enabled": True,
            "auto_apply": auto_apply, "dry_run": dry_run,
            "consensus_k": self.consensus_k,
        }

    # ── helpers ────────────────────────────────────────────────────────────

    def _existing_reviews(self) -> dict:
        try:
            rows = self.venue_dao.list_venue_link_audit_reviews() or []
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[VenueLinkAuditReviewer] could not read stored reviews: {e}")
            return {}
        return {(r["handle"], r["venue_id"]): r for r in rows}

    def _addresses(self, venue_ids: list) -> dict:
        """`get_address` per venue, not `get_address_bulk` — the bulk method
        is a NARROW projection (lat/lng/neighborhood only) whose own docstring
        forbids widening it for a caller outside its feature, and this pass
        needs `street` and `city`. Bounded by the flagged-pair count (12
        live), so the per-venue read is not a hot path."""
        out: dict = {}
        for venue_id in dict.fromkeys(venue_ids):
            try:
                out[venue_id] = self.venue_dao.get_address(venue_id) or {}
            except Exception:  # pragma: no cover - defensive
                out[venue_id] = {}
        return out

    async def _review_one(
        self, *, candidate, audited, mapped, addresses, reviews, multi_mapped,
        semaphore, auto_apply: bool,
    ) -> PairOutcome:
        import asyncio

        handle = candidate.handle
        venue_id = audited.venue_id
        stored = reviews.get((handle, venue_id))

        base = PairOutcome(
            handle=handle, venue_id=venue_id, outcome=OUTCOME_ERROR,
            checkable_count=audited.checkable_count,
            non_corroborating_count=audited.non_corroborating_count,
        )

        # An operator's own decision outranks everything and is permanent —
        # never re-review, never re-spend, never overwrite it.
        if stored and stored.get("operator_decision"):
            base.outcome = OUTCOME_SKIPPED_OPERATOR_DECIDED
            base.persist = False
            return base

        # Already confirmed and still within its recorded evidence → nothing
        # to do. This is what makes a second run over unchanged data free.
        if review_suppresses_pair(
            stored, non_corroborating_count=audited.non_corroborating_count,
        ):
            base.outcome = OUTCOME_CONFIRMED
            base.verdict = VERDICT_CONFIRM
            base.suppressed = True
            base.persist = False
            base.evidence_quote = stored.get("evidence_quote")
            base.matched_field = stored.get("matched_field")
            base.reason = stored.get("reason")
            return base

        if multi_mapped:
            # Plan §F item 3 — the structural gate. No model call is made at
            # all: this is not a judgement the model gets to attempt.
            base.outcome = OUTCOME_SKIPPED_MULTI_MAPPED
            return base

        venue = dict(addresses.get(venue_id) or {})
        venue["venue_id"] = venue_id
        venue["venue_name"] = audited.venue_name
        siblings = [
            {
                "venue_name": s.venue_name,
                "street": (addresses.get(s.venue_id) or {}).get("street"),
                "neighborhood": (addresses.get(s.venue_id) or {}).get("neighborhood"),
            }
            for s in mapped if s.venue_id != venue_id
        ]
        try:
            events = self.venue_dao.list_events_by_handle(handle) or []
        except Exception as e:
            logger.warning(f"[VenueLinkAuditReviewer] event read failed for {handle}: {e}")
            base.outcome = OUTCOME_ERROR
            return base

        evidence = build_review_evidence(
            handle=handle, venue=venue, siblings=siblings, events=events,
        )
        if not evidence.non_corroborating:
            # Every row that made this pair flagged is protected by the skip
            # table, so there is nothing left the model may look at.
            base.outcome = OUTCOME_SKIPPED_ALL_EVENTS_PROTECTED
            return base

        async def _one():
            async with semaphore:
                return await self.review_client.review_venue_link(evidence=evidence)

        try:
            raws = await asyncio.gather(
                *[_one() for _ in range(self.consensus_k)], return_exceptions=True,
            )
        except Exception as e:  # pragma: no cover - gather itself
            logger.warning(f"[VenueLinkAuditReviewer] review calls failed for {handle}: {e}")
            base.outcome = OUTCOME_ERROR
            return base

        verdicts: list = []
        accepted: list = []
        empty = 0
        rejected = 0
        errored = 0
        for raw in raws:
            if isinstance(raw, Exception):
                errored += 1
                continue
            if not (raw or "").strip():
                empty += 1
                continue
            parsed = parse_venue_link_review_response(raw)
            result = validate_venue_link_review(
                ReviewAnswer(**{
                    k: v for k, v in (parsed or {}).items()
                    if k in ("verdict", "evidence_quote", "matched_field", "reason")
                }) if parsed is not None else None,
                location_texts=evidence.location_texts,
            )
            if not result.accepted:
                rejected += 1
                continue
            verdicts.append(result.verdict)
            accepted.append(result)

        base.tally = _tally_dict(verdicts, empty=empty, rejected=rejected, errored=errored)

        # An empty response is its OWN outcome and must outrank a generic
        # rejection — see OUTCOME_EMPTY_RESPONSE's comment. Reported whenever
        # it prevented consensus, so a token-budget regression is visible on
        # the first run rather than a cycle later.
        if empty and len(verdicts) != self.consensus_k:
            base.outcome = OUTCOME_EMPTY_RESPONSE
            return base
        if errored and len(verdicts) != self.consensus_k:
            base.outcome = OUTCOME_ERROR
            return base
        if rejected and len(verdicts) != self.consensus_k:
            base.outcome = OUTCOME_VALIDATOR_REJECTED
            return base

        outcome, verdict = tally_consensus(verdicts, k=self.consensus_k)
        base.outcome = outcome
        base.verdict = verdict
        if accepted:
            base.evidence_quote = accepted[0].evidence_quote
            base.matched_field = accepted[0].matched_field
            base.reason = accepted[0].reason
        # Gated on `auto_apply`, NOT on the verdict alone. In shadow mode a
        # consensus confirm is recorded but suppresses nothing (`_persist`
        # stores no `non_corroborating_count_at_review`, so
        # `review_suppresses_pair` refuses it) — and this outcome list is
        # precisely the report an operator reads BEFORE deciding to enable
        # auto-apply. Claiming `suppressed: true` there would misstate the
        # one thing that report exists to convey.
        #
        # Note this gates only a FRESH consensus. The early-return above, for
        # a pair already confirmed and still in force, sets `suppressed=True`
        # unconditionally and correctly: that pair really is suppressed right
        # now, whatever the flag currently says.
        base.suppressed = auto_apply and outcome == OUTCOME_CONFIRMED
        logger.info(
            "[VenueLinkAuditReviewer] handle=%s venue=%s outcome=%s verdict=%s tally=%s",
            handle, venue_id, base.outcome, base.verdict, base.tally,
        )
        return base

    def _persist(self, outcome: PairOutcome, *, auto_apply: bool) -> None:
        """One row per pair the pass looked at — including skipped and
        no-consensus ones, so an operator sees what the machine considered and
        declined, not only what it acted on.

        `auto_apply=False` records the verdict but stores no
        `non_corroborating_count_at_review`, which is what
        `review_suppresses_pair` requires to suppress — so shadow mode is
        genuinely inert rather than merely hidden."""
        fields = {
            "outcome": outcome.outcome,
            "verdict": outcome.verdict,
            "evidence_quote": outcome.evidence_quote,
            "matched_field": outcome.matched_field,
            "reason": outcome.reason,
            "consensus_k": self.consensus_k,
            "tally": outcome.tally,
            "model": getattr(self.review_client, "model", None),
            "checkable_count_at_review": outcome.checkable_count,
            "non_corroborating_count_at_review": (
                outcome.non_corroborating_count if auto_apply else None
            ),
        }
        try:
            self.venue_dao.upsert_venue_link_audit_review(
                outcome.handle, outcome.venue_id, fields,
            )
        except Exception as e:
            logger.warning(
                f"[VenueLinkAuditReviewer] could not persist review for "
                f"{outcome.handle}/{outcome.venue_id}: {e}"
            )


def _tally_dict(verdicts: list, *, empty: int, rejected: int, errored: int) -> dict:
    """The per-verdict breakdown stored on the review row. Non-verdict
    outcomes are kept as their own keys rather than dropped, so a stored
    tally always sums to K and a reader can tell "six confirms and four
    empties" from "six confirms and four insufficients"."""
    tally: dict = {}
    for verdict in verdicts:
        tally[verdict] = tally.get(verdict, 0) + 1
    if empty:
        tally[OUTCOME_EMPTY_RESPONSE] = empty
    if rejected:
        tally[OUTCOME_VALIDATOR_REJECTED] = rejected
    if errored:
        tally[OUTCOME_ERROR] = errored
    return tally


__all__ = [
    "ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_ENABLED_KEY",
    "ADMIN_CONFIG_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_KEY",
    "DEFAULT_VENUE_LINK_AUDIT_REVIEWER_ENABLED",
    "DEFAULT_VENUE_LINK_AUDIT_REVIEWER_AUTO_APPLY_ENABLED",
    "DEFAULT_REVIEW_CONSENSUS_K", "MIN_REVIEW_CONSENSUS_K",
    "OUTCOME_CONFIRMED", "OUTCOME_CONTRADICTED", "OUTCOME_INSUFFICIENT",
    "OUTCOME_NO_CONSENSUS", "OUTCOME_SKIPPED_MULTI_MAPPED",
    "OUTCOME_SKIPPED_OPERATOR_DECIDED", "OUTCOME_SKIPPED_ALL_EVENTS_PROTECTED",
    "OUTCOME_VALIDATOR_REJECTED", "OUTCOME_EMPTY_RESPONSE", "OUTCOME_ERROR",
    "DECISION_ACCEPTED", "DECISION_REJECTED", "DECISIONS",
    "validate_venue_link_audit_reviewer_enabled_config",
    "validate_venue_link_audit_reviewer_auto_apply_config",
    "load_venue_link_audit_reviewer_enabled",
    "load_venue_link_audit_reviewer_auto_apply_enabled",
    "parse_venue_link_review_response",
    "ReviewEvidence", "PairOutcome",
    "build_review_evidence", "tally_consensus", "review_suppresses_pair",
    "apply_reviews_to_audit", "VenueLinkAuditReviewerService",
]
