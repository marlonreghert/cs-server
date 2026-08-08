"""Engagement write-through: favorites / hot_likes / blocked venues -> RDS
(truth) + Redis (read).

vibes_bot calls this via the cs-server API (it no longer writes Redis directly).
The raw user_id is pseudonymized (HMAC) before it touches RDS; the Redis
projection keeps the existing key formats vibes_bot reads
(`user_favorites:{user_id}`, `hot_likes:{venue_id}`, `user_blocked_venues:
{user_id}`). RDS-first, then Redis: if the Redis projection fails after the
RDS commit, the caller is told to retry.

Blocking and favoriting the same venue are mutually exclusive: `block_venue`
clears any active favorite for that (user, venue) pair in the SAME RDS
transaction (see `RdsVenueStore.block_venue`), and mirrors that into the
favorites Redis projection too. Unblocking never restores a favorite —
locked product decision, not a bug.
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
    #   blocked_venues (new): user_blocked_venues:{user_id} — parallel to
    #   favorites, deliberately named blocked_venue/block_venue (not block/
    #   block_list) to stay unambiguous against the pre-existing, unrelated
    #   admin eligibility "block-list" concept (venue_eligibility.py).
    def _fav_key(self, user_id: str) -> str:
        return f"user_favorites:{user_id}"

    def _hot_key(self, venue_id: str) -> str:
        return f"hot_likes:v1:{venue_id}"

    def _blocked_key(self, user_id: str) -> str:
        return f"user_blocked_venues:{user_id}"

    def add_favorite(self, user_id: str, venue_id: str) -> None:
        self.rds_store.upsert_favorite(self.pseudonymize(user_id), venue_id)  # truth
        self.redis.sadd(self._fav_key(user_id), venue_id)  # projection

    def remove_favorite(self, user_id: str, venue_id: str) -> None:
        self.rds_store.soft_delete_favorite(self.pseudonymize(user_id), venue_id)
        self.redis.srem(self._fav_key(user_id), venue_id)

    def block_venue(self, user_id: str, venue_id: str) -> bool:
        """Block a venue: one RDS transaction upserts the block row and
        atomically clears any active favorite for the same pair (see
        RdsVenueStore.block_venue) — RDS-first, then the Redis projections.

        Returns whether an existing favorite was actually cleared, so the
        caller (the router response) can report `favorite_removed` without a
        second round trip.
        """
        favorite_removed = self.rds_store.block_venue(self.pseudonymize(user_id), venue_id)  # truth
        self.redis.sadd(self._blocked_key(user_id), venue_id)  # projection
        if favorite_removed:
            self.redis.srem(self._fav_key(user_id), venue_id)  # keep favorites projection in sync
        return favorite_removed

    def unblock_venue(self, user_id: str, venue_id: str) -> None:
        # Unblocking never restores a favorite — locked product decision.
        self.rds_store.soft_delete_block(self.pseudonymize(user_id), venue_id)
        self.redis.srem(self._blocked_key(user_id), venue_id)

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

    # ── erasure (account deletion) ────────────────────────────────────────────
    def delete_user_data(self, user_id: str) -> dict:
        """Erase every trace of one user, given their RAW id.

        This is deliberately NOT the ordinary remove path. `remove_favorite`
        soft-deletes (the row, and so the pseudonym, survives) and
        `remove_hot_like` keeps the `hot_like_event` history on purpose. Both are
        right for un-favoriting and wrong for an erasure request: a surviving
        pseudonymized row is a deactivation, which Apple's Guideline 5.1.1(v)
        explicitly rejects. Erasure therefore hard-deletes.

        Step order is load-bearing, and it deliberately INVERTS the
        projection-after-truth ordering the write path uses:

        1. Read the user's hot-liked venue ids from RDS. `hot_likes:v1:{venue_id}`
           is keyed by VENUE with user ids as members, so Redis cannot answer
           "which venues did this user like?" — only these rows can.
        2. Strip the projections (every hot-likes membership, then the favorites
           key, then the blocked-venues key — the last two are single per-user
           sets, like favorites, so no enumeration is needed). Idempotent:
           re-running `srem`/`delete` on an already-clean key is a no-op.
        3. Hard-delete the RDS rows.

        Steps 2 and 3 are in this order **so a retry can converge**. The write
        path commits RDS then projects, but erasure cannot: if the rows were
        deleted first and the projection write then failed, the retry would
        re-enumerate an empty venue list and could never reach the hot-likes
        sets — the user would stay a member of them forever. Enumerating from
        rows that still exist is the only thing that makes the operation
        recoverable, so the rows must outlive the projection cleanup.

        Raises on a blank id (a silent no-op would report success while deleting
        nothing) and propagates a projection failure so the caller retries.
        """
        if not user_id or not str(user_id).strip():
            raise ValueError("user_id is required for erasure")

        pseudo = self.pseudonymize(user_id)

        # 1. Enumerate while the rows still exist — they are the only index from
        #    this user to the venue-keyed hot-likes sets.
        venue_ids = list(self.rds_store.list_user_hot_like_venue_ids(pseudo))

        # 2. Projections this service owns. Before the purge, so a failure here
        #    leaves the rows intact and the retry can re-enumerate.
        for venue_id in venue_ids:
            self.redis.srem(self._hot_key(venue_id), user_id)
        self.redis.delete(self._fav_key(user_id))
        self.redis.delete(self._blocked_key(user_id))

        # 3. System of record.
        removed = dict(self.rds_store.purge_user_engagement(pseudo))
        removed["hot_like_sets"] = len(venue_ids)
        # Log the pseudonym and the counts, never the raw id — keeping the raw id
        # out of storage is the whole point of pseudonymizing it.
        logger.info(
            "[Engagement] erased user pseudo=%s removed=%s", pseudo, removed
        )
        return removed

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
