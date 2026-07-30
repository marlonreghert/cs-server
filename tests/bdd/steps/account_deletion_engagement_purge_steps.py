"""Behave steps for tests/bdd/persistence/account-deletion-engagement-purge.feature.

Erasure is not the same as un-favoriting. The ordinary remove paths soft-delete a
favorite and deliberately keep the immutable `hot_like_event` history; a deletion
request must leave nothing bearing the user's pseudonym in either store, and must
also strip the user from every `hot_likes:v1:{venue_id}` set — which is keyed by
VENUE, so those venues have to be enumerated from RDS before the rows go.

Drives the RDS layer built in environment.py: context.rds_store (fake truth),
context.engagement_service (the write-through + purge service),
context.fake_redis (the projection), context.repository / context.redis_only_dao
for the venue-untouched assertions.
"""
from __future__ import annotations

import logging
import re

from behave import given, then, when  # type: ignore[import-untyped]

from app.models import Venue

_LAT, _LNG = -8.05, -34.88


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "venue"


def _state(context):
    if getattr(context, "adp", None) is None:
        context.adp = {
            "venues": {},
            "result": None,
            "error": None,
            "before": {},
            "log_records": [],
            "fail_projection": False,
        }
    return context.adp


def _seed_venue(context, name: str) -> str:
    state = _state(context)
    venue_id = _slug(name)
    context.repository.upsert_venue(
        Venue(
            forecast=True, processed=True, venue_id=venue_id, venue_name=name,
            venue_address=f"{venue_id} address", venue_lat=_LAT, venue_lng=_LNG,
            venue_type="BAR",
        )
    )
    state["venues"][name] = venue_id
    return venue_id


def _venue_id(context, name: str) -> str:
    return _state(context)["venues"].get(name) or _seed_venue(context, name)


def _pseudo(context, user_id: str) -> str:
    return context.engagement_service.pseudonymize(user_id)


def _fav_rows(context, user_id: str) -> list:
    p = _pseudo(context, user_id)
    return [(k, v) for k, v in context.rds_store.favorites.items() if k[0] == p]


def _hot_rows(context, user_id: str) -> list:
    p = _pseudo(context, user_id)
    return [e for e in context.rds_store.hot_like_events if e.get("user_pseudo") == p]


def _session_rows(context, user_id: str) -> list:
    p = _pseudo(context, user_id)
    return [s for s in context.rds_store.app_sessions if s[0] == p]


def _hot_members(context, venue_name: str) -> set:
    key = f"hot_likes:v1:{_venue_id(context, venue_name)}"
    return set(context.fake_redis.smembers(key) or [])


def _fav_members(context, user_id: str) -> set:
    return set(context.fake_redis.smembers(f"user_favorites:{user_id}") or [])


def _fav_key_exists(context, user_id: str) -> bool:
    return bool(context.fake_redis.exists(f"user_favorites:{user_id}"))


# ── Background ────────────────────────────────────────────────────────────────
@given('the venues "{first}" and "{second}" exist and are servable')
def step_seed_venues(context, first, second):
    for name in (first, second):
        venue_id = _seed_venue(context, name)
        assert venue_id in set(context.repository.list_servable_venue_ids())


@given('the user "{user_id}" has favorited "{first}" and "{second}"')
def step_favorited_two(context, user_id, first, second):
    for name in (first, second):
        context.engagement_service.add_favorite(user_id, _venue_id(context, name))


@given('the user "{user_id}" has favorited "{name}"')
def step_favorited_one(context, user_id, name):
    context.engagement_service.add_favorite(user_id, _venue_id(context, name))


@given('the user "{user_id}" has hot-liked "{name}"')
def step_hot_liked(context, user_id, name):
    context.engagement_service.add_hot_like(user_id, _venue_id(context, name))


@given('the user "{user_id}" has recorded app sessions')
def step_sessions(context, user_id):
    context.engagement_service.record_session(user_id)


@given('the user "{user_id}" has hot-liked "{name}" on {days:d} different days')
def step_hot_liked_days(context, user_id, name, days):
    """Multiple event rows for one (user, venue) — the unique index is per
    business period, so a repeat visitor legitimately holds several rows."""
    from datetime import timedelta

    from app.utils.recife_time import recife_today

    venue_id = _venue_id(context, name)
    pseudo = _pseudo(context, user_id)
    today = recife_today()
    for offset in range(days):
        context.rds_store.add_hot_like_event(pseudo, venue_id, today - timedelta(days=offset))
    context.fake_redis.sadd(f"hot_likes:v1:{venue_id}", user_id)


@given("the engagement projection write will fail")
def step_projection_will_fail(context):
    _state(context)["fail_projection"] = True


@given("the engagement projection write recovers")
@when("the engagement projection write recovers")
def step_projection_recovers(context):
    _state(context)["fail_projection"] = False


# ── When ──────────────────────────────────────────────────────────────────────
def _snapshot(context):
    state = _state(context)
    state["before"] = {
        "hot_counts": {n: len(_hot_members(context, n)) for n in state["venues"]},
        "fav_b": len(_fav_rows(context, "user-b")),
        "hot_b": len(_hot_rows(context, "user-b")),
        "fav_proj_b": _fav_members(context, "user-b"),
        "servable": set(context.repository.list_servable_venue_ids()),
        "venue_rows": {v: dict(context.rds_store.venues[v]) for v in state["venues"].values()},
    }


def _do_delete(context, user_id):
    state = _state(context)
    if not state["before"]:
        _snapshot(context)

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            try:
                records.append(record.getMessage())
            except Exception:
                pass

    handler = _Capture()
    root = logging.getLogger()
    root.addHandler(handler)

    original_srem = context.fake_redis.srem
    if state["fail_projection"]:
        def _boom(*a, **k):
            raise RuntimeError("projection unavailable")
        context.fake_redis.srem = _boom

    state["error"] = None
    try:
        state["result"] = context.engagement_service.delete_user_data(user_id)
    except Exception as exc:
        state["error"] = exc
        state["result"] = None
    finally:
        context.fake_redis.srem = original_srem
        root.removeHandler(handler)
        state["log_records"].extend(records)


@when('the engagement data for "{user_id}" is deleted')
@when('the engagement data for "{user_id}" is deleted again')
def step_delete(context, user_id):
    _do_delete(context, user_id)


@when('the engagement data for {user_id} is deleted')
def step_delete_invalid(context, user_id):
    """Scenario Outline variant: the id arrives unquoted ("" or `missing`)."""
    raw = (user_id or "").strip()
    if raw == '""':
        _do_delete(context, "")
    elif raw == "missing":
        _do_delete(context, None)
    else:
        _do_delete(context, raw.strip('"'))


# ── Then: RDS erasure ─────────────────────────────────────────────────────────
@then('no favorites rows remain for "{user_id}"')
def step_no_fav_rows(context, user_id):
    rows = _fav_rows(context, user_id)
    assert rows == [], f"{len(rows)} favorites row(s) still bear the pseudonym: {rows}"


@then('no hot-like event rows remain for "{user_id}"')
def step_no_hot_rows(context, user_id):
    rows = _hot_rows(context, user_id)
    assert rows == [], f"{len(rows)} hot_like_event row(s) survive: {rows}"


@then('no app session rows remain for "{user_id}"')
def step_no_session_rows(context, user_id):
    rows = _session_rows(context, user_id)
    assert rows == [], f"{len(rows)} app_session row(s) survive: {rows}"


@then('no favorites row bearing the pseudonym for "{user_id}" survives in any state')
def step_hard_deleted(context, user_id):
    """A soft delete leaves the row (and the pseudonym) behind — that is a
    deactivation, which is exactly what Apple's guideline rejects."""
    rows = _fav_rows(context, user_id)
    assert rows == [], (
        f"favorites rows survived erasure (soft-deleted, not hard-deleted): {rows}"
    )


# ── Then: projection erasure ──────────────────────────────────────────────────
@then('the favorites projection for "{user_id}" is absent')
def step_fav_proj_absent(context, user_id):
    assert not _fav_key_exists(context, user_id), (
        f"user_favorites:{user_id} still exists with {_fav_members(context, user_id)}"
    )


@then('"{user_id}" is not a member of the hot-likes set for "{venue_name}"')
def step_not_hot_member(context, user_id, venue_name):
    members = _hot_members(context, venue_name)
    assert user_id not in members, f"{user_id} still in {venue_name}'s set: {members}"


@then('the hot-likes count for "{venue_name}" decreases by {n:d}')
def step_hot_count_drops(context, venue_name, n):
    before = _state(context)["before"]["hot_counts"][venue_name]
    after = len(_hot_members(context, venue_name))
    assert after == before - n, f"{venue_name}: {before} -> {after}, expected -{n}"


@then('"{user_id}" is still a member of the hot-likes set for "{venue_name}"')
def step_still_hot_member(context, user_id, venue_name):
    members = _hot_members(context, venue_name)
    assert user_id in members, f"{user_id} was wrongly removed from {venue_name}: {members}"


# ── Then: isolation ───────────────────────────────────────────────────────────
@then('the favorites rows for "{user_id}" are unchanged')
def step_other_fav_unchanged(context, user_id):
    assert len(_fav_rows(context, user_id)) == _state(context)["before"]["fav_b"]


@then('the hot-like event rows for "{user_id}" are unchanged')
def step_other_hot_unchanged(context, user_id):
    assert len(_hot_rows(context, user_id)) == _state(context)["before"]["hot_b"]


@then('the favorites projection for "{user_id}" is unchanged')
def step_other_fav_proj_unchanged(context, user_id):
    assert _fav_members(context, user_id) == _state(context)["before"]["fav_proj_b"]


@then('the venues "{first}" and "{second}" are still servable')
def step_venues_servable(context, first, second):
    servable = set(context.repository.list_servable_venue_ids())
    for name in (first, second):
        assert _venue_id(context, name) in servable, f"{name} left the serving view"


@then("the stored venue rows and enrichment records are unchanged")
def step_venue_rows_unchanged(context):
    for venue_id, before in _state(context)["before"]["venue_rows"].items():
        after = context.rds_store.venues.get(venue_id)
        assert after is not None, f"{venue_id} row disappeared"
        assert after.get("deleted_at") is None, f"{venue_id} was soft-deleted"
        assert after.get("lifecycle_status", "active") == before.get(
            "lifecycle_status", "active"
        ), f"{venue_id} lifecycle changed"


# ── Then: outcome / idempotency ───────────────────────────────────────────────
@then("the deletion succeeds")
@then("the second deletion succeeds")
def step_deletion_ok(context):
    state = _state(context)
    assert state["error"] is None, f"deletion raised {state['error']!r}"


@then("the deletion reports failure")
def step_deletion_failed(context):
    assert _state(context)["error"] is not None, "expected the deletion to fail"


@then("the deletion reports zero removals")
@then("the second deletion reports zero removals")
def step_zero_removals(context):
    result = _state(context)["result"]
    assert result is not None, "no deletion result recorded"
    total = sum(v for v in result.values() if isinstance(v, int))
    assert total == 0, f"expected zero removals, got {result}"


@then("the request is rejected as invalid")
def step_rejected(context):
    assert _state(context)["error"] is not None, (
        "a blank/missing user id must be rejected, not silently purge nothing"
    )


@then('no emitted log record contains the raw id "{user_id}"')
def step_no_raw_id_logged(context, user_id):
    leaked = [m for m in _state(context)["log_records"] if user_id in m]
    assert not leaked, f"raw user id leaked into logs: {leaked[:3]}"
