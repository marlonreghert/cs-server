"""Promoter account registry: lifecycle CRUD, and discovery from
already-extracted venue posts. See
plans/260804_instagram-promoter-events.md §B: "Discovery proposes, an
operator disposes."

Discovery costs nothing: every caption it reads was already scraped by the
venue's own Instagram crawl, and every event it walks was already extracted
by a separately-billed run. It can never run away, either — a proposed
handle is inserted with `status='candidate'`, and `PromoterCrawlService`
only ever crawls `active` accounts (see that module), so an over-eager
discovery pass wastes a database row, not money.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.metrics import PROMOTER_ACCOUNTS_TOTAL
from app.services.event_venue_resolution import build_handle_index, extract_mentions
from app.services.instagram_handle_sources import normalize_handle

logger = logging.getLogger(__name__)

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_REJECTED = "rejected"
ALL_STATUSES = (STATUS_CANDIDATE, STATUS_ACTIVE, STATUS_PAUSED, STATUS_REJECTED)

DISCOVERY_MANUAL = "manual"
DISCOVERY_MENTION = "mention"
# Defined for parity with the plan's enum. Nothing in this codebase currently
# models Instagram's tagged-collaborator data (distinct from a caption
# @-mention), so no path produces this value yet — see the PR description.
DISCOVERY_TAG = "tag"

DEFAULT_MENTION_THRESHOLD = 3


class InvalidPromoterAccount(ValueError):
    pass


def validate_status(status: str) -> str:
    if status not in ALL_STATUSES:
        raise InvalidPromoterAccount(
            f"unknown status {status!r}, expected one of {ALL_STATUSES}"
        )
    return status


class PromoterRegistryService:
    """Registry CRUD + discovery. The admin router is a thin HTTP shell over
    this — every rule that must hold regardless of transport lives here."""

    def __init__(self, venue_dao, post_source=None, now_provider=None):
        self.venue_dao = venue_dao
        # EventPostSource, reused unchanged — discovery walks the SAME
        # archived manifests the venue-post extractor already reads, keyed by
        # (venue_id, shortcode) off each already-extracted event.
        self.post_source = post_source
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    # ── lifecycle CRUD ───────────────────────────────────────────────────────
    def register(
        self, handle: str, *, display_name: Optional[str] = None,
        status: str = STATUS_CANDIDATE, notes: Optional[str] = None,
        added_by: Optional[str] = None,
    ) -> dict:
        """A manual add. Defaults to `candidate` — an operator naming a
        handle still has to flip it `active` before anything crawls it,
        the same one-way gate discovery's proposals go through."""
        normalized = normalize_handle(handle)
        if not normalized:
            raise InvalidPromoterAccount("handle is required")
        validate_status(status)
        row = self.venue_dao.upsert_promoter_account(normalized, {
            "display_name": display_name, "status": status,
            "discovery_source": DISCOVERY_MANUAL, "notes": notes, "added_by": added_by,
        })
        self._update_gauge()
        return row

    def update(self, handle: str, fields: dict) -> Optional[dict]:
        normalized = normalize_handle(handle)
        existing = self.venue_dao.get_promoter_account(normalized)
        if existing is None:
            return None
        if "status" in fields:
            validate_status(fields["status"])
        row = self.venue_dao.upsert_promoter_account(normalized, fields)
        self._update_gauge()
        return row

    def reject(self, handle: str) -> Optional[dict]:
        """The registry's DELETE — a soft transition to `rejected`, never a
        hard delete. Same "keep the audit trail" posture as every other
        enrichment table in this codebase (CLAUDE.md: never hard-delete)."""
        return self.update(handle, {"status": STATUS_REJECTED})

    def get(self, handle: str) -> Optional[dict]:
        return self.venue_dao.get_promoter_account(normalize_handle(handle))

    def list(self, status: Optional[str] = None) -> list[dict]:
        return self.venue_dao.list_promoter_accounts(status=status)

    def _update_gauge(self) -> None:
        counts = {status: 0 for status in ALL_STATUSES}
        for row in self.venue_dao.list_promoter_accounts():
            status = row.get("status", STATUS_CANDIDATE)
            counts[status] = counts.get(status, 0) + 1
        for status, count in counts.items():
            PROMOTER_ACCOUNTS_TOTAL.labels(status=status).set(count)

    # ── discovery: propose, never crawl ─────────────────────────────────────
    async def run_discovery(self, config: Optional[dict] = None) -> dict:
        """Count every distinct @-mention in the caption of an
        already-extracted VENUE post, and propose a handle as `candidate`
        once it clears the mention threshold — provided the handle names
        neither a known venue (it would be a rung-1 identity, not a
        promoter) nor an already-registered account (idempotent across
        repeated runs: a re-run counts the same captions again but never
        re-inserts).
        """
        cfg = dict(config or {})
        threshold = int(cfg.get("mention_threshold") or DEFAULT_MENTION_THRESHOLD)

        known_handles = set(build_handle_index(self.venue_dao).keys())
        registered = {row["handle"] for row in self.venue_dao.list_promoter_accounts()}

        counts: dict[str, int] = {}
        first_event_for: dict[str, str] = {}
        considered = 0

        if self.post_source is not None:
            events = [
                e for e in (self.venue_dao.list_events() or [])
                if e.get("source_kind", "venue_post") == "venue_post"
            ]
            since = datetime(1970, 1, 1, tzinfo=timezone.utc)
            posts_by_venue: dict[str, list] = {}
            for event in events:
                venue_id = event.get("venue_id")
                shortcode = event.get("source_shortcode")
                if not venue_id or not shortcode:
                    continue
                considered += 1
                if venue_id not in posts_by_venue:
                    posts_by_venue[venue_id] = await self.post_source.posts_for_venue(
                        venue_id, since,
                    )
                post = next(
                    (p for p in posts_by_venue[venue_id] if p.shortcode == shortcode), None,
                )
                if post is None or not post.caption:
                    continue
                own_handle = normalize_handle(event.get("source_handle"))
                # A distinct-per-post count: a caption that repeats the same
                # handle several times must not let one post alone clear the
                # threshold.
                for mention in set(extract_mentions(post.caption)):
                    if mention == own_handle or mention in known_handles:
                        continue
                    counts[mention] = counts.get(mention, 0) + 1
                    first_event_for.setdefault(mention, event["event_id"])

        proposed = []
        for handle, count in counts.items():
            if handle in registered or count < threshold:
                continue
            self.venue_dao.upsert_promoter_account(handle, {
                "status": STATUS_CANDIDATE,
                "discovery_source": DISCOVERY_MENTION,
                "discovered_from_event_id": first_event_for.get(handle),
                "mention_count": count,
            })
            proposed.append(handle)

        if proposed:
            self._update_gauge()
        return {"considered_events": considered, "candidates_proposed": proposed}


__all__ = [
    "PromoterRegistryService", "InvalidPromoterAccount", "validate_status",
    "STATUS_CANDIDATE", "STATUS_ACTIVE", "STATUS_PAUSED", "STATUS_REJECTED",
    "ALL_STATUSES", "DISCOVERY_MANUAL", "DISCOVERY_MENTION", "DISCOVERY_TAG",
    "DEFAULT_MENTION_THRESHOLD",
]
