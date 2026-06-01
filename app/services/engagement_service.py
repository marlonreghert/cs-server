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

logger = logging.getLogger(__name__)

HOT_LIKE_TTL_SECONDS = 6 * 3600  # trending window for the Redis counter


class EngagementService:
    def __init__(self, redis_client, rds_store=None, pseudonymization_key: str = ""):
        # redis_client is the raw redis (supports sadd/srem/sismember/expire)
        self.redis = redis_client
        self.rds_store = rds_store
        self._key = (pseudonymization_key or "").encode()

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
        if self.rds_store is not None:
            self.rds_store.upsert_favorite(self.pseudonymize(user_id), venue_id)
        self.redis.sadd(self._fav_key(user_id), venue_id)  # projection

    def remove_favorite(self, user_id: str, venue_id: str) -> None:
        if self.rds_store is not None:
            self.rds_store.soft_delete_favorite(self.pseudonymize(user_id), venue_id)
        self.redis.srem(self._fav_key(user_id), venue_id)

    def add_hot_like(self, user_id: str, venue_id: str, ttl_seconds: int = None) -> None:
        # Append-only event in RDS; trending set + TTL in Redis. The client
        # (vibes_bot) controls the TTL via the ttl_seconds wire field.
        if self.rds_store is not None:
            self.rds_store.add_hot_like_event(self.pseudonymize(user_id), venue_id)
        key = self._hot_key(venue_id)
        self.redis.sadd(key, user_id)
        self.redis.expire(key, ttl_seconds if ttl_seconds and ttl_seconds > 0 else HOT_LIKE_TTL_SECONDS)

    def remove_hot_like(self, user_id: str, venue_id: str) -> None:
        # Removes the user from the trending set (Redis read path). The RDS
        # hot_like_event log is immutable history and is intentionally kept.
        self.redis.srem(self._hot_key(venue_id), user_id)
