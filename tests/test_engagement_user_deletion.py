"""Unit tests for account-deletion erasure.

BDD covers the observable outcome (nothing bearing the pseudonym survives). These
protect the internals a behavioural assertion would not distinguish: the step
ORDER that makes a retry converge, the DISTINCT enumeration, blank-id rejection,
and the guarantee that the raw user id never reaches a log record.
"""
from __future__ import annotations

import logging

import pytest

from app.services.engagement_service import EngagementService

KEY = "test-hmac-key"


class _Redis:
    """Minimal stand-in recording the calls in the order they happen."""

    def __init__(self, fail_srem=False):
        self.calls = []
        self.fail_srem = fail_srem

    def sadd(self, key, member):
        self.calls.append(("sadd", key, member))

    def srem(self, key, member):
        if self.fail_srem:
            raise RuntimeError("projection unavailable")
        self.calls.append(("srem", key, member))

    def delete(self, key):
        self.calls.append(("delete", key))

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))


class _Store:
    def __init__(self, venues=(), counts=None):
        self._venues = list(venues)
        self._counts = counts or {"favorites": 0, "hot_like_events": 0, "app_sessions": 0}
        self.calls = []
        self.purged = False

    def list_user_hot_like_venue_ids(self, user_pseudo):
        self.calls.append(("enumerate", user_pseudo))
        # After a purge the rows are gone — model that faithfully, because it is
        # exactly what breaks a retry if the ordering is wrong.
        return [] if self.purged else list(self._venues)

    def purge_user_engagement(self, user_pseudo):
        self.calls.append(("purge", user_pseudo))
        self.purged = True
        return dict(self._counts)


def _svc(redis=None, store=None):
    return EngagementService(
        redis_client=redis or _Redis(),
        rds_store=store or _Store(),
        pseudonymization_key=KEY,
    )


# ── validation ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "   ", None])
def test_blank_user_id_is_rejected(bad):
    """A silent no-op would report success while deleting nothing."""
    store = _Store()
    with pytest.raises(ValueError):
        _svc(store=store).delete_user_data(bad)
    assert store.calls == [], "nothing may be touched for an invalid id"


# ── ordering (the convergence guarantee) ──────────────────────────────────────
def test_enumeration_happens_before_the_purge():
    store = _Store(venues=["v1"])
    _svc(store=store).delete_user_data("user-a")
    assert [c[0] for c in store.calls] == ["enumerate", "purge"]


def test_projection_cleanup_happens_before_the_purge():
    """The write path commits RDS then projects; erasure MUST invert that. If the
    rows went first, a failed projection write could never be retried — the
    retry would enumerate nothing and leave the user in every hot-likes set."""
    redis, store = _Redis(), _Store(venues=["v1"])
    order = []
    original_srem = redis.srem
    original_purge = store.purge_user_engagement

    def srem(key, member):
        order.append("projection")
        return original_srem(key, member)

    def purge(pseudo):
        order.append("rows")
        return original_purge(pseudo)

    redis.srem, store.purge_user_engagement = srem, purge
    _svc(redis=redis, store=store).delete_user_data("user-a")
    assert order == ["projection", "rows"]


def test_retry_converges_after_a_projection_failure():
    """First attempt fails mid-projection; the rows must still be there so the
    retry can re-enumerate and finish the job."""
    store = _Store(venues=["v1", "v2"])
    failing = _Redis(fail_srem=True)
    with pytest.raises(RuntimeError):
        _svc(redis=failing, store=store).delete_user_data("user-a")
    assert not store.purged, "rows must survive a failed projection write"

    healthy = _Redis()
    _svc(redis=healthy, store=store).delete_user_data("user-a")
    assert ("srem", "hot_likes:v1:v1", "user-a") in healthy.calls
    assert ("srem", "hot_likes:v1:v2", "user-a") in healthy.calls
    assert store.purged


# ── projection targets ────────────────────────────────────────────────────────
def test_every_hot_like_set_and_the_favorites_key_are_cleaned():
    redis, store = _Redis(), _Store(venues=["v1", "v2", "v3"])
    _svc(redis=redis, store=store).delete_user_data("user-a")
    for venue in ("v1", "v2", "v3"):
        assert ("srem", f"hot_likes:v1:{venue}", "user-a") in redis.calls
    assert ("delete", "user_favorites:user-a") in redis.calls


def test_projection_keys_match_the_write_path_formats():
    """These key formats are the contract with vibes_bot's DAOs; a drift here
    would silently leave the real keys behind."""
    service = _svc()
    assert service._fav_key("u") == "user_favorites:u"
    assert service._hot_key("v") == "hot_likes:v1:v"


# ── reporting / idempotency ───────────────────────────────────────────────────
def test_counts_are_reported_including_the_set_count():
    store = _Store(
        venues=["v1", "v2"],
        counts={"favorites": 3, "hot_like_events": 5, "app_sessions": 2},
    )
    removed = _svc(store=store).delete_user_data("user-a")
    assert removed == {
        "favorites": 3, "hot_like_events": 5, "app_sessions": 2, "hot_like_sets": 2,
    }


def test_unknown_user_reports_zero_removals():
    removed = _svc(store=_Store()).delete_user_data("ghost")
    assert sum(v for v in removed.values() if isinstance(v, int)) == 0


def test_pseudonym_matches_the_write_path():
    """Erasure must derive the SAME pseudonym the writes used, or it deletes
    nothing while reporting success."""
    service = _svc()
    store = _Store()
    service.rds_store = store
    service.delete_user_data("user-a")
    assert store.calls[0][1] == service.pseudonymize("user-a")


# ── privacy ───────────────────────────────────────────────────────────────────
def test_the_raw_user_id_is_never_logged(caplog):
    with caplog.at_level(logging.INFO):
        _svc(store=_Store(venues=["v1"])).delete_user_data("user-a-raw-id")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "user-a-raw-id" not in joined
    # The pseudonym IS expected — it is what makes the log auditable.
    assert _svc().pseudonymize("user-a-raw-id") in joined
