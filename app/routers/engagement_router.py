"""Engagement API: vibes_bot writes favorites/hot_likes here (reads stay Redis).

Write-through: the service commits RDS then projects Redis. If the projection
fails after the RDS commit, the endpoint returns 5xx so vibes_bot retries
(idempotent), keeping the user's read path consistent within seconds.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.metrics import ENGAGEMENT_SESSION_TOTAL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["engagement"])

_engagement_service = None


def set_engagement_service(service) -> None:
    global _engagement_service
    _engagement_service = service


class EngagementRequest(BaseModel):
    user_id: str
    venue_id: str
    # Only used by POST /v1/hot-likes (the trending TTL the client controls);
    # ignored by favorites and by DELETE. Wire field name is `ttl_seconds`.
    ttl_seconds: Optional[int] = None


class SessionRequest(BaseModel):
    # App-activity ping carries only the user; sessions have no venue (so the
    # favorites/hot_likes EngagementRequest, which requires venue_id, won't do).
    user_id: str


def _svc():
    if _engagement_service is None:
        raise HTTPException(status_code=503, detail="engagement service not configured")
    return _engagement_service


@router.post("/favorites")
async def add_favorite(req: EngagementRequest):
    try:
        _svc().add_favorite(req.user_id, req.venue_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Engagement] add_favorite failed: {e}")
        raise HTTPException(status_code=502, detail="favorite write failed; retry")
    return {"status": "ok"}


@router.delete("/favorites")
async def remove_favorite(req: EngagementRequest):
    try:
        _svc().remove_favorite(req.user_id, req.venue_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Engagement] remove_favorite failed: {e}")
        raise HTTPException(status_code=502, detail="unfavorite write failed; retry")
    return {"status": "ok"}


@router.post("/hot-likes")
async def add_hot_like(req: EngagementRequest):
    try:
        _svc().add_hot_like(req.user_id, req.venue_id, ttl_seconds=req.ttl_seconds)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Engagement] add_hot_like failed: {e}")
        raise HTTPException(status_code=502, detail="hot-like write failed; retry")
    return {"status": "ok"}


@router.post("/sessions")
async def record_session(req: SessionRequest):
    try:
        _svc().record_session(req.user_id)
    except HTTPException:
        raise
    except Exception as e:
        # Best-effort for the client (vibes_bot does not surface it to users), but
        # 502 so the ping can retry. Never log the raw user_id.
        ENGAGEMENT_SESSION_TOTAL.labels(result="error").inc()
        logger.error(f"[Engagement] record_session failed: {e}")
        raise HTTPException(status_code=502, detail="session write failed; retry")
    ENGAGEMENT_SESSION_TOTAL.labels(result="success").inc()
    return {"status": "ok"}


@router.delete("/hot-likes")
async def remove_hot_like(req: EngagementRequest):
    try:
        _svc().remove_hot_like(req.user_id, req.venue_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Engagement] remove_hot_like failed: {e}")
        raise HTTPException(status_code=502, detail="hot-like remove failed; retry")
    return {"status": "ok"}
