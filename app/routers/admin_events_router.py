"""Admin API for reviewing and correcting extracted Instagram events.

See plans/260804_instagram-event-extraction.md §E ("so an operator can see
and correct what the pipeline believes before anything downstream trusts
it"). This is the ONLY consumer of events.event outside the extraction job
itself — no public/app-facing route reads this table, and nothing here
touches the Redis serving projection or changes any venue response (the
plan's non-goal: "No serving impact").
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.metrics import EVENT_COVER_PRESIGN_TOTAL, EVENT_VENUE_LINK_TOTAL
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


def _media_store():
    if _container is None:
        raise HTTPException(status_code=503, detail="Container not initialized")
    store = getattr(_container, "media_archive_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Media archive store not configured")
    return store


def _require_admin(
    x_admin_api_key: Optional[str] = Header(default=None, alias="X-Admin-Api-Key"),
) -> None:
    """Opt-in shared-secret gate for admin routes (plans/260806_event-cover-
    presign.md, item 5: "Require admin auth, like every other admin route").

    `settings.admin_api_key` defaults to empty, which makes this a no-op —
    identical to every other route in this router, which rely solely on the
    network-layer gating documented in app/routers/internal_router.py (Caddy
    never exposes /admin publicly; no admin_token/internal_api_key exists in
    this repo by design). This route is the first to accept an OPTIONAL
    app-level gate on top of that, because unlike its siblings its response
    IS a bearer credential: once issued, it keeps working after it leaves
    cs-server's network perimeter, so an operator who wants defense-in-depth
    here can set admin_api_key without changing behavior anywhere else.
    """
    expected = settings.admin_api_key
    if not expected:
        return
    if not x_admin_api_key or not secrets.compare_digest(x_admin_api_key, expected):
        raise HTTPException(status_code=401, detail="Admin authentication required")


class EventOut(BaseModel):
    event_id: str
    venue_id: Optional[str] = None
    # plans/260807_review-queue-completeness-and-venue-names.md: the linked
    # venue's name, resolved in SQL via a LEFT JOIN (app/dao/rds_venue_store.py
    # _EVENT_SELECT) so the console never has to decode an opaque venue_id.
    # NULL whenever venue_id is NULL, and also when venue_id points at a
    # venue row that no longer exists (a dangling reference must not fail
    # the listing).
    venue_name: Optional[str] = None
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
    # plans/260808_event-ticket-info-and-attractions.md: a non-URL ticket
    # reference, captured verbatim alongside — not instead of — ticket_url.
    # An ordinary operator-protectable scalar (EventPatch below).
    ticket_info: Optional[str] = None
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
    # plans/260806_multi-event-posts.md: the content-derived identity that
    # lets several events share one post, and the display-only ordinal that
    # never participates in it. NULL for a pre-migration row never
    # re-extracted since, and for an extraction_failed placeholder (no
    # content to key by).
    source_event_key: Optional[str] = None
    source_event_index: Optional[int] = None
    # plans/260807_one-event-many-posts.md: every post that announced this
    # event, oldest first-seen first. ADDITIVE — `source_permalink`,
    # `source_handle`, `source_shortcode` and `cover_photo_key` above are
    # KEPT, now derived (by the DAO) from the most recently seen source
    # rather than read from a column, so the deployed console's flyer viewer
    # keeps working unchanged across a merge. `sources` is the new,
    # optional-to-adopt way to see every announcing post at once.
    sources: list["EventSourceOut"] = Field(default_factory=list)
    # plans/260807_auto-accept-and-field-level-protection.md: the union of
    # every field name an operator has PATCHed on this event (written by
    # `patch_event`, below). NULL/None is a MEANINGFUL, distinct value from
    # an empty list: it means "unknown which fields, if any, were edited" —
    # every row that predates this column, or that was confirmed without
    # ever being PATCHed — and `reconcile_post_events`'s confirmed branch
    # reads exactly that distinction to keep today's whole-row protection
    # for those rows rather than guessing. Additive.
    operator_edited_fields: Optional[list[str]] = None
    # plans/260808_event-ticket-info-and-attractions.md: every DJ/live act
    # the pipeline found, each `{name, type, stage, styles}` — additive
    # across the posts announcing one event (see app.services.
    # event_reconciliation.union_attractions), never operator-editable
    # (EventPatch below omits it, matching `lineup`).
    attractions: list = Field(default_factory=list)
    # plans/260811_post-items-and-categories.md: what a post yielded — an
    # event, a promotion, a menu item, or an unrecognised value stored
    # VERBATIM (never coerced). Operator-editable (EventPatch below) and
    # protected via `operator_edited_fields` like every other scalar here.
    # Response FIELD NAME is unaffected by the table rename (events.event ->
    # events.post_item) — the plan keeps this PR's API surface stable; the
    # console is updated separately.
    post_type: str = "event"
    # Free text, steered by an admin-configurable vocabulary
    # (app.models.post_category) and case-insensitively canonicalised on
    # write. NULL when the model gave no basis for one — never invented.
    category: Optional[str] = None


class EventSourceOut(BaseModel):
    source_kind: str = "venue_post"
    source_handle: str
    source_shortcode: str
    source_permalink: Optional[str] = None
    cover_photo_key: Optional[str] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None


EventOut.model_rebuild()


def _to_out(dao, row: dict) -> EventOut:
    sources = [EventSourceOut(**s) for s in dao.list_event_sources(row["event_id"])]
    return EventOut(**{
        **row, "lineup": row.get("lineup") or [],
        "attractions": row.get("attractions") or [], "sources": sources,
    })


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
    ticket_info: Optional[str] = None
    price_text: Optional[str] = None
    location_text: Optional[str] = None
    # plans/260811_post-items-and-categories.md: lets an operator correct a
    # misclassified item's type. `category` is deliberately NOT here — the
    # plan tests operator correction of `post_type` only.
    post_type: Optional[str] = None


@router.get("", response_model=list[EventOut])
def list_events(
    venue_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
):
    dao = _dao()
    rows = dao.list_events(venue_id=venue_id, status=status, since=since, until=until)
    return [_to_out(dao, r) for r in rows]


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
    """Every event awaiting a human decision, with ranked venue candidates
    where they exist — the queue's whole value: an operator chooses between
    named venues with scores and reasons instead of being handed a location
    string and a search box. See VenueRepository.list_events_awaiting_decision
    for the population (wider than "promoter events awaiting a venue" — see
    plans/260807_review-queue-completeness-and-venue-names.md)."""
    dao = _dao()
    out = []
    for row in dao.list_events_awaiting_decision():
        candidates = dao.list_event_venue_link_candidates(row["event_id"])
        sources = [EventSourceOut(**s) for s in dao.list_event_sources(row["event_id"])]
        out.append(ReviewQueueItemOut(
            **{
                **row, "lineup": row.get("lineup") or [],
                "attractions": row.get("attractions") or [],
            },
            candidates=candidates, sources=sources,
        ))
    return out


class EventCoverOut(BaseModel):
    url: str
    expires_at: datetime
    expires_in: int


# ── event cover presign (plans/260806_event-cover-presign.md) — also
# registered before "/{event_id}" for the same reason as /promoters and
# /review above. Not actually reachable by "/{event_id}" today (that pattern
# is one path segment; "/{event_id}/cover" is two, so the default converter
# never matches it regardless of order), but the ordering is pinned by BDD so
# a future switch to a greedy "{event_id:path}" converter fails loudly
# instead of silently swallowing this route the way the router's history
# warns about. ────────────────────────────────────────────────────────────
@router.get("/{event_id}/cover", response_model=EventCoverOut)
async def get_event_cover(event_id: str, _admin: None = Depends(_require_admin)):
    """A short-lived, viewable url for an event's archived cover photo.

    The object is resolved from the event's OWN row, never from anything in
    the request — the single non-negotiable security decision in the plan:
    a route that presigns a client-supplied key is an arbitrary-object-read
    primitive against the whole data lake. The signed url is a bearer
    credential for that object until it expires; it is returned to the
    caller here and must never be logged.
    """
    dao = _dao()
    row = dao.get_event(event_id)
    if row is None:
        EVENT_COVER_PRESIGN_TOTAL.labels(result="not_found").inc()
        raise HTTPException(status_code=404, detail="Event not found")

    key = row.get("cover_photo_key")
    if not key:
        EVENT_COVER_PRESIGN_TOTAL.labels(result="no_key").inc()
        raise HTTPException(status_code=404, detail="Event has no archived cover photo")

    store = _media_store()
    expires_in = settings.event_cover_presign_expires_seconds
    url = await store.presign(key, expires_in=expires_in)
    if not url:
        # MediaArchiveStore.presign() returns None rather than raising on
        # failure — that must map to a server error here, never a 200
        # carrying a null/empty url.
        EVENT_COVER_PRESIGN_TOTAL.labels(result="failed").inc()
        raise HTTPException(status_code=502, detail="Failed to sign cover photo url")

    EVENT_COVER_PRESIGN_TOTAL.labels(result="signed").inc()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return EventCoverOut(url=url, expires_at=expires_at, expires_in=expires_in)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: str):
    dao = _dao()
    row = dao.get_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _to_out(dao, row)


@router.patch("/{event_id}", response_model=EventOut)
def patch_event(event_id: str, patch: EventPatch):
    dao = _dao()
    existing = dao.get_event(event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found")
    fields = patch.model_dump(exclude_unset=True)
    if fields:
        # `exclude_unset` yields exactly the keys the console sent as a
        # genuine change (vibes_bot #169's partial-patch fix) — the ONLY
        # signal that lets `reconcile_post_events` protect what an operator
        # actually touched instead of the whole row. Accumulates across
        # successive patches; never records a field this request did not
        # send. Sorted for a deterministic, order-independent value.
        already_edited = set(existing.get("operator_edited_fields") or [])
        fields["operator_edited_fields"] = sorted(already_edited | set(fields.keys()))
    updated = dao.update_event(event_id, fields) if fields else existing
    return _to_out(dao, updated)


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
    return _to_out(dao, updated)


@router.post("/{event_id}/reject", response_model=EventOut)
def reject_event(event_id: str):
    dao = _dao()
    existing = dao.get_event(event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found")
    updated = dao.update_event(event_id, {"status": "rejected"})
    return _to_out(dao, updated)


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
    return _to_out(dao, updated)


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
    return _to_out(dao, updated)


__all__ = [
    "router", "set_container", "EventOut", "EventSourceOut", "EventPatch",
    "PromoterAccountOut", "PromoterAccountCreate", "PromoterAccountPatch",
    "LinkCandidateOut", "ReviewQueueItemOut", "LinkRequest", "EventCoverOut",
]
