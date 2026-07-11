"""Engagement write-through: favorites / hot_likes -> RDS (truth) + Redis (read).

vibes_bot calls this via the cs-server API (it no longer writes Redis directly).
The raw user_id is pseudonymized (HMAC) before it touches RDS; the Redis
projection keeps the existing key formats vibes_bot reads
(`user_favorites:{user_id}`, `hot_likes:{venue_id}`). RDS-first, then Redis: if
the Redis projection fails after the RDS commit, the caller is told to retry.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta

from app.metrics import ENGAGEMENT_HOT_LIKE_DEDUP_TOTAL
from app.utils.recife_time import recife_today

logger = logging.getLogger(__name__)

HOT_LIKE_TTL_SECONDS = 6 * 3600  # trending window for the Redis counter


class EngagementService:
    def __init__(self, redis_client, rds_store, pseudonymization_key: str = ""):
        # redis_client is the raw redis (supports sadd/srem/sismember/expire)
        if not pseudonymization_key:
            # An empty key would silently HMAC every user id with b"" -- every
            # existing RDS engagement row becomes unrecoverable the moment a
            # real key is later set/rotated. Fail loudly at construction time
            # (Container init -> FastAPI startup) instead of persisting under
            # a degenerate key.
            raise RuntimeError(
                "ENGAGEMENT_PSEUDONYMIZATION_KEY must be set (non-empty) before "
                "engagement writes can be pseudonymized; refusing to start with "
                "an empty key."
            )
        self.redis = redis_client
        self.rds_store = rds_store
        self._key = pseudonymization_key.encode()

    def pseudonymize(self, user_id: str) -> str:
        return hmac.new(self._key, user_id.encode(), hashlib.sha256).hexdigest()

    # Key formats MUST match what vibes_bot reads (vibes_bot/app/daos):
    #   favorites_dao.KEY_PREFIX = "user_favorites:"  -> user_favorites:{user_id}
    #   hot_likes_dao.KEY_PREFIX = "hot_likes:v1:"    -> hot_likes:v1:{venue_id}
    def _fav_key(self, user_id: str) -> str:
        return f"user_favorites:{user_id}"

    def _hot_key(self, venue_id: str) -> str:
        return f"hot_likes:v1:{venue_id}"

    def add_favorite(self, user_id: str, venue_id: str) -> None:
        self.rds_store.upsert_favorite(self.pseudonymize(user_id), venue_id)  # truth
        self.redis.sadd(self._fav_key(user_id), venue_id)  # projection

    def remove_favorite(self, user_id: str, venue_id: str) -> None:
        self.rds_store.soft_delete_favorite(self.pseudonymize(user_id), venue_id)
        self.redis.srem(self._fav_key(user_id), venue_id)

    def add_hot_like(self, user_id: str, venue_id: str, ttl_seconds: int = None) -> None:
        # Append-only event in RDS; trending set + TTL in Redis. The client
        # (vibes_bot) controls the TTL via the ttl_seconds wire field.
        # Idempotent per (user, venue, Recife calendar day): engagement_router
        # mandates the client retry on a 5xx, and a retried write must not
        # persist a second event row (unique index + ON CONFLICT DO NOTHING,
        # same pattern as record_session/app_session_day below).
        inserted = self.rds_store.add_hot_like_event(
            self.pseudonymize(user_id), venue_id, recife_today()
        )
        if not inserted:
            ENGAGEMENT_HOT_LIKE_DEDUP_TOTAL.inc()
        key = self._hot_key(venue_id)
        self.redis.sadd(key, user_id)
        self.redis.expire(key, ttl_seconds if ttl_seconds and ttl_seconds > 0 else HOT_LIKE_TTL_SECONDS)

    def remove_hot_like(self, user_id: str, venue_id: str) -> None:
        # Removes the user from the trending set (Redis read path). The RDS
        # hot_like_event log is immutable history and is intentionally kept.
        self.redis.srem(self._hot_key(venue_id), user_id)

    # App-activity system-of-record. Unlike favorites/hot_likes this is RDS-only
    # (no Redis projection): the counts are read by the admin from RDS, never on
    # the serve path. One pseudonymized row per user per Recife day.
    def record_session(self, user_id: str) -> None:
        self.rds_store.record_app_session(self.pseudonymize(user_id), recife_today())

    def activity_counts(self) -> dict:
        # "Active in the last N days" is inclusive of today, so the window starts
        # N-1 days back (1d == today only; 7d == today and the prior 6; etc).
        today = recife_today()
        return {
            "total_users": self.rds_store.count_users(None),
            "active_1d": self.rds_store.count_users(today),
            "active_7d": self.rds_store.count_users(today - timedelta(days=6)),
            "active_30d": self.rds_store.count_users(today - timedelta(days=29)),
        }
