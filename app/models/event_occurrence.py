"""The events serving projection's cross-repo contract payload.

plans/260905_events-serving-projection.md — the exact field list under
"Occurrence payload (the cross-repo contract — vibes_bot reads exactly
this)". Field names and presence are PINNED: vibes_bot's `GET /events` and
`GET /events/{occurrence_id}` are built against this shape. Do not rename,
drop, or add a field without updating that plan and the sibling one in the
wrapper — see CLAUDE.md's "Do not change the cross-repo contract" guardrail.

`price_text` and `ticket_info` both travel and are NOT interchangeable even
though they can render the same sentence in different places on the card —
see the plan's Data section for why collapsing them would destroy a
distinction this repo already stores.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class EventOccurrence(BaseModel):
    occurrence_id: str
    event_id: str
    occurrence_date: str  # local (Recife) YYYY-MM-DD
    starts_at: Optional[datetime] = None  # UTC
    ends_at: Optional[datetime] = None
    time_known: bool = False
    is_recurring: bool = False
    recurrence_text: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    price_text: Optional[str] = None
    ticket_info: Optional[str] = None
    ticket_url: Optional[str] = None
    lineup: Optional[list] = None
    attractions: Optional[list[dict]] = None  # [{name, type, stage, styles}]
    flyer_url: Optional[str] = None
    venue_id: Optional[str] = None
    venue_name: Optional[str] = None
    venue_neighborhood: Optional[str] = None
    venue_lat: Optional[float] = None
    venue_lng: Optional[float] = None
    source_permalink: Optional[str] = None
    source_handle: Optional[str] = None
    city_slug: str
    status: Optional[str] = None
    updated_at: Optional[datetime] = None


__all__ = ["EventOccurrence"]
