"""Admin API for reviewing and correcting extracted Instagram events.

See plans/260804_instagram-event-extraction.md §E ("so an operator can see
and correct what the pipeline believes before anything downstream trusts
it"). This is the ONLY consumer of events.event outside the extraction job
itself — no public/app-facing route reads this table, and nothing here
touches the Redis serving projection or changes any venue response (the
plan's non-goal: "No serving impact").
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

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


__all__ = ["router", "set_container", "EventOut", "EventPatch"]
