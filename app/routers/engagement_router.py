"""Engagement API: vibes_bot writes favorites/hot_likes here (reads stay Redis).

Write-through: the service commits RDS then projects Redis. If the projection
fails after the RDS commit, the endpoint returns 5xx so vibes_bot retries
(idempotent), keeping the user's read path consistent within seconds.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["engagement"])

_engagement_service = None


def set_engagement_service(service) -> None:
    global _engagement_service
    _engagement_service = service


class EngagementRequest(BaseModel):
    user_id: str
    venue_id: str


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
        _svc().add_hot_like(req.user_id, req.venue_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Engagement] add_hot_like failed: {e}")
        raise HTTPException(status_code=502, detail="hot-like write failed; retry")
    return {"status": "ok"}
