"""Unit tests for block/unblock write-through and erasure's blocked-venue
projection cleanup.

Mirrors the fake-call-order-recording pattern tests/test_engagement_user_deletion.py
established (_Redis/_Store minimal doubles): BDD covers the observable outcome
(RDS truth + Redis projection immediacy); these protect the internals a
behavioural assertion would not distinguish -- RDS-before-Redis ordering, the
CONDITIONAL favorites-projection srem (only when the DAO actually cleared a
favorite), and where the blocked-venues projection cleanup falls in the
erasure step order.
"""
from __future__ import annotations

from app.services.engagement_service import EngagementService

KEY = "test-hmac-key"


class _Redis:
    """Minimal stand-in recording the calls in the order they happen."""

    def __init__(self):
        self.calls = []

    def sadd(self, key, member):
        self.calls.append(("sadd", key, member))

    def srem(self, key, member):
        self.calls.append(("srem", key, member))

    def delete(self, key):
        self.calls.append(("delete", key))

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))


class _Store:
    def __init__(self, block_result=False):
        self.calls = []
        self.block_result = block_result
        self.unblocked = []

    def block_venue(self, user_pseudo, venue_id):
        self.calls.append(("block_venue", user_pseudo, venue_id))
        return self.block_result

    def soft_delete_block(self, user_pseudo, venue_id):
        self.calls.append(("soft_delete_block", user_pseudo, venue_id))
        self.unblocked.append((user_pseudo, venue_id))

    def list_user_hot_like_venue_ids(self, user_pseudo):
        self.calls.append(("enumerate", user_pseudo))
        return []

    def purge_user_engagement(self, user_pseudo):
        self.calls.append(("purge", user_pseudo))
        return {"favorites": 0, "hot_like_events": 0, "app_sessions": 0, "blocked_venues": 0}


def _svc(redis=None, store=None):
    return EngagementService(
        redis_client=redis or _Redis(),
        rds_store=store or _Store(),
        pseudonymization_key=KEY,
    )


# ── block_venue: ordering + dispatch ───────────────────────────────────────────
def test_block_venue_calls_rds_before_redis():
    redis, store = _Redis(), _Store(block_result=True)
    _svc(redis=redis, store=store).block_venue("user-a", "v1")
    assert store.calls == [("block_venue", _svc().pseudonymize("user-a"), "v1")]
    assert redis.calls[0] == ("sadd", "user_blocked_venues:user-a", "v1")


def test_block_venue_returns_the_dao_result():
    assert _svc(store=_Store(block_result=True)).block_venue("user-a", "v1") is True
    assert _svc(store=_Store(block_result=False)).block_venue("user-a", "v1") is False


def test_block_venue_always_sadds_the_blocked_projection():
    redis, store = _Redis(), _Store(block_result=False)
    _svc(redis=redis, store=store).block_venue("user-a", "v1")
    assert ("sadd", "user_blocked_venues:user-a", "v1") in redis.calls


def test_block_venue_srems_favorites_projection_only_when_favorite_removed():
    redis_true, store_true = _Redis(), _Store(block_result=True)
    _svc(redis=redis_true, store=store_true).block_venue("user-a", "v1")
    assert ("srem", "user_favorites:user-a", "v1") in redis_true.calls

    redis_false, store_false = _Redis(), _Store(block_result=False)
    _svc(redis=redis_false, store=store_false).block_venue("user-a", "v1")
    assert ("srem", "user_favorites:user-a", "v1") not in redis_false.calls


# ── unblock_venue ──────────────────────────────────────────────────────────────
def test_unblock_venue_srems_the_blocked_projection_and_never_touches_favorites():
    redis, store = _Redis(), _Store()
    service = _svc(redis=redis, store=store)
    service.unblock_venue("user-a", "v1")
    assert ("srem", "user_blocked_venues:user-a", "v1") in redis.calls
    assert store.unblocked == [(service.pseudonymize("user-a"), "v1")]
    assert not any(c[0] == "srem" and c[1] == "user_favorites:user-a" for c in redis.calls)


def test_blocked_key_matches_the_projection_format():
    """This key format is the contract with vibes_bot's DAO (once wired) --
    parallel to _fav_key's `user_favorites:{user_id}`."""
    assert _svc()._blocked_key("u") == "user_blocked_venues:u"


# ── erasure ──────────────────────────────────────────────────────────────────
def test_delete_user_data_clears_the_blocked_venues_projection_key():
    redis, store = _Redis(), _Store()
    _svc(redis=redis, store=store).delete_user_data("user-a")
    assert ("delete", "user_blocked_venues:user-a") in redis.calls


def test_delete_user_data_clears_blocked_projection_before_the_purge():
    """Same convergence guarantee the favorites/hot-likes cleanup relies on:
    the projection cleanup must precede the RDS purge, or a retry after a
    failed purge could never re-clean it."""
    redis, store = _Redis(), _Store()
    order = []
    original_delete = redis.delete
    original_purge = store.purge_user_engagement

    def _delete(key):
        order.append(("delete", key))
        original_delete(key)

    def _purge(pseudo):
        order.append(("purge", pseudo))
        return original_purge(pseudo)

    redis.delete, store.purge_user_engagement = _delete, _purge
    _svc(redis=redis, store=store).delete_user_data("user-a")

    delete_indices = [i for i, (tag, _) in enumerate(order) if tag == "delete"]
    purge_index = next(i for i, (tag, _) in enumerate(order) if tag == "purge")
    assert delete_indices, "expected at least one projection delete"
    assert all(i < purge_index for i in delete_indices)
