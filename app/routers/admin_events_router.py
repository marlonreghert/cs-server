"""Admin API for reviewing and correcting extracted Instagram events.

See plans/260804_instagram-event-extraction.md §E ("so an operator can see
and correct what the pipeline believes before anything downstream trusts
it"). This is the ONLY consumer of events.event outside the extraction job
itself — no public/app-facing route reads this table, and nothing here
touches the Redis serving projection or changes any venue response (the
plan's non-goal: "No serving impact").
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.metrics import EVENT_VENUE_LINK_TOTAL
from app.services.promoter_registry_service import InvalidPromoterAccount, PromoterRegistryService

router = APIRouter(prefix="/admin/events", tags=["admin", "events"])

# Global container reference, set during startup — same pattern as
# admin_trigger_router / internal_router.
_container = None


def set_container(container) -> None:
    global _container
    _container = container


def _dao():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    dao = getattr(_container, "pipeline_repository", None)
    if dao is None:
        raise HTTPException(status_code=503, detail="Venue repository not configured")
    return dao


class EventOut(BaseModel):
    event_id: str
    venue_id: Optional[str] = None
    source_kind: str = "venue_post"
    source_handle: str
    source_shortcode: str
    source_permalink: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_recurring: bool = False
    recurrence_text: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    lineup: list = Field(default_factory=list)
    ticket_url: Optional[str] = None
    price_text: Optional[str] = None
    location_text: Optional[str] = None
    cover_photo_key: Optional[str] = None
    confidence: Optional[float] = None
    status: str
    review_reason: Optional[str] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # plans/260804_instagram-promoter-events.md: how, if at all, a promoter
    # event's venue was resolved. NULL for a venue-owned post's event, which
    # never touches the resolution ladder at all.
    location_resolution: Optional[str] = None
    location_confidence: Optional[float] = None
    linked_by: Optional[str] = None
    linked_at: Optional[datetime] = None


def _to_out(row: dict) -> EventOut:
    return EventOut(**{**row, "lineup": row.get("lineup") or []})


class EventPatch(BaseModel):
    """Every field an operator can correct by hand. `exclude_unset` on the
    update means an omitted field is left alone, not cleared to null — a
    partial correction must never blank out fields the operator did not
    touch."""
    venue_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_recurring: Optional[bool] = None
    recurrence_text: Optional[str] = None
    lineup: Optional[list] = None
    ticket_url: Optional[str] = None
    price_text: Optional[str] = None
    location_text: Optional[str] = None


@router.get("", response_model=list[EventOut])
def list_events(
    venue_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
):
    dao = _dao()
    rows = dao.list_events(venue_id=venue_id, status=status, since=since, until=until)
    return [_to_out(r) for r in rows]


# ── promoter registry (plans/260804_instagram-promoter-events.md) ───────────
# Registered BEFORE the "/{event_id}" routes below: FastAPI/Starlette matches
# routes in REGISTRATION ORDER, and "/{event_id}" is a single path segment —
# exactly the shape of "/promoters" and "/review" too. Registered after it,
# a request for GET /admin/events/review would be swallowed by
# get_event(event_id="review") and returned as a 404 "Event not found"
# instead of ever reaching this code (caught by BDD, not by inspection).
def _registry() -> PromoterRegistryService:
    return PromoterRegistryService(_dao())


class PromoterAccountOut(BaseModel):
    handle: str
    display_name: Optional[str] = None
    status: str
    discovery_source: str = "manual"
    discovered_from_event_id: Optional[str] = None
    mention_count: int = 0
    notes: Optional[str] = None
    added_by: Optional[str] = None
    last_crawled_at: Optional[datetime] = None
    posts_crawled: int = 0
    events_extracted: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PromoterAccountCreate(BaseModel):
    """Registering a handle defaults it to `candidate` — even a deliberate
    manual add still has to be flipped `active` (PATCH) before anything
    crawls it, the same one-way gate a discovered candidate goes through."""
    handle: str
    display_name: Optional[str] = None
    status: str = "candidate"
    notes: Optional[str] = None
    added_by: Optional[str] = None


class PromoterAccountPatch(BaseModel):
    display_name: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    added_by: Optional[str] = None


@router.get("/promoters", response_model=list[PromoterAccountOut])
def list_promoters(status: Optional[str] = Query(None)):
    return _registry().list(status=status)


@router.post("/promoters", response_model=PromoterAccountOut)
def create_promoter(body: PromoterAccountCreate):
    try:
        return _registry().register(
            body.handle, display_name=body.display_name, status=body.status,
            notes=body.notes, added_by=body.added_by,
        )
    except InvalidPromoterAccount as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/promoters/{handle}", response_model=PromoterAccountOut)
def patch_promoter(handle: str, patch: PromoterAccountPatch):
    fields = patch.model_dump(exclude_unset=True)
    try:
        row = _registry().update(handle, fields)
    except InvalidPromoterAccount as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="Promoter account not found")
    return row


@router.delete("/promoters/{handle}", response_model=PromoterAccountOut)
def delete_promoter(handle: str):
    """A soft transition to `rejected`, never a hard delete — the same
    "keep the audit trail" posture every other enrichment table in this
    codebase takes (CLAUDE.md)."""
    row = _registry().reject(handle)
    if row is None:
        raise HTTPException(status_code=404, detail="Promoter account not found")
    return row


# ── review queue (also registered before "/{event_id}") ─────────────────────
class LinkCandidateOut(BaseModel):
    venue_id: str
    rank: int
    score: Optional[float] = None
    method: str
    evidence: dict = Field(default_factory=dict)


class ReviewQueueItemOut(EventOut):
    candidates: list[LinkCandidateOut] = Field(default_factory=list)


class LinkRequest(BaseModel):
    """Choose a candidate by rank, or name a venue directly — exactly one
    must resolve to a venue_id."""
    venue_id: Optional[str] = None
    candidate_rank: Optional[int] = None
    linked_by: str


@router.get("/review", response_model=list[ReviewQueueItemOut])
def review_queue():
    """Pending promoter events with their ranked candidates — the queue's
    whole value: an operator chooses between named venues with scores and
    reasons instead of being handed a location string and a search box."""
    dao = _dao()
    out = []
    for row in dao.list_events_pending_location():
        candidates = dao.list_event_venue_link_candidates(row["event_id"])
        out.append(ReviewQueueItemOut(
            **{**row, "lineup": row.get("lineup") or []}, candidates=candidates,
        ))
    return out


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: str):
    dao = _dao()
    row = dao.get_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _to_out(row)


@router.patch("/{event_id}", response_model=EventOut)
def patch_event(event_id: str, patch: EventPatch):
    dao = _dao()
    existing = dao.get_event(event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found")
    fields = patch.model_dump(exclude_unset=True)
    updated = dao.update_event(event_id, fields) if fields else existing
    return _to_out(updated)


@router.post("/{event_id}/confirm", response_model=EventOut)
def confirm_event(event_id: str):
    """An operator's confirmation — from here on, re-extraction of this post
    may only touch raw_extraction/last_seen_at (see EventExtractionService's
    confirmed-preserve rule). Clears any prior review_reason: it described why
    the record was queued, and it no longer needs review."""
    dao = _dao()
    existing = dao.get_event(event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found")
    updated = dao.update_event(event_id, {"status": "confirmed", "review_reason": None})
    return _to_out(updated)


@router.post("/{event_id}/reject", response_model=EventOut)
def reject_event(event_id: str):
    dao = _dao()
    existing = dao.get_event(event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found")
    updated = dao.update_event(event_id, {"status": "rejected"})
    return _to_out(updated)


# ── manual link / unlink (promoter registry + review queue are registered
# above, before "/{event_id}" — see the comment there) ──────────────────────
@router.post("/{event_id}/link", response_model=EventOut)
def link_event(event_id: str, body: LinkRequest):
    """A manual link is never overwritten by a later crawl (see
    PromoterCrawlService) — the operator's answer outranks the model, the
    same principle a confirmed event's protection is built on."""
    dao = _dao()
    existing = dao.get_event(event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found")

    venue_id = body.venue_id
    confidence = None
    if body.candidate_rank is not None:
        candidates = dao.list_event_venue_link_candidates(event_id)
        match = next((c for c in candidates if c["rank"] == body.candidate_rank), None)
        if match is None:
            raise HTTPException(status_code=400, detail="Unknown candidate rank")
        venue_id = match["venue_id"]
        confidence = match.get("score")
    if not venue_id:
        raise HTTPException(status_code=400, detail="venue_id or candidate_rank is required")

    updated = dao.update_event(event_id, {
        "venue_id": venue_id, "location_resolution": "manual",
        "location_confidence": confidence, "linked_by": body.linked_by,
        "linked_at": datetime.now(timezone.utc),
    })
    EVENT_VENUE_LINK_TOTAL.labels(method="manual", result="manual").inc()
    return _to_out(updated)


@router.post("/{event_id}/unlink", response_model=EventOut)
def unlink_event(event_id: str):
    dao = _dao()
    existing = dao.get_event(event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found")
    updated = dao.update_event(event_id, {
        "venue_id": None, "location_resolution": "unresolved",
        "location_confidence": None, "linked_by": None, "linked_at": None,
    })
    EVENT_VENUE_LINK_TOTAL.labels(method="operator_unlink", result="unresolved").inc()
    return _to_out(updated)


__all__ = [
    "router", "set_container", "EventOut", "EventPatch",
    "PromoterAccountOut", "PromoterAccountCreate", "PromoterAccountPatch",
    "LinkCandidateOut", "ReviewQueueItemOut", "LinkRequest",
]
