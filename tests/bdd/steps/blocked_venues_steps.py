"""Behave steps for tests/bdd/persistence/blocked-venues.feature.

Drives POST/DELETE /v1/blocks through the real HTTP boundary (context.client,
the same TestClient wired by environment.py / used by
tests/bdd/steps/user_activity_tracking_steps.py for /v1/sessions) rather than
calling EngagementService directly — the feature's own acceptance criteria
require the missing-id case to be "rejected at the request boundary (422)",
which only a real request/response round trip can prove.

Two steps are deliberately REUSED, not redefined, from
tests/bdd/steps/account_deletion_engagement_purge_steps.py (same literal
Gherkin text — a duplicate @given/@when/@then registration for identical text
raises behave.step_registry.AmbiguousStep and would break the whole BDD suite,
not just this feature):
  - `the venues "{first}" and "{second}" exist and are servable` (Background)
  - `the user "{user_id}" has favorited "{name}"`
  - `the engagement data for "{user_id}" is deleted`
  - `the request is rejected as invalid` — checks `context.adp["error"] is not
    None`. This file writes into that same `context.adp["error"]` slot (via
    `_adp`, mirroring that module's own `_state` helper) so the reused
    assertion sees a real result for OUR requests too, without importing that
    module (behave exec's each steps/*.py file directly rather than through
    normal `import`, so a real Python `import` of an already behave-loaded
    steps module would re-run its decorators a second time and hit the same
    AmbiguousStep collision).

One line in blocked-venues.feature was reworded from the plan's original
"RDS no longer holds an active favorite for ... on ..." (identical, semantics
preserved) to "the favorite for ... on ... is no longer active", because that
exact literal text is already registered in rds_system_of_record_steps.py —
but bound to venue IDS ("v1"), whereas this feature refers to venues by NAME
("Bar Alfa"). Reusing it as-is would silently look up the favorite under the
raw string "Bar Alfa" instead of the resolved venue_id "bar_alfa" and always
fail. See the coordination note in plans/260808_blocked-venues.md's execution
report for the full reasoning.
"""
from __future__ import annotations

import re

import parse as _parse
from behave import given, when, then, register_type  # type: ignore[import-untyped]


@_parse.with_pattern(r'[^"]*')
def _parse_maybe_empty(text):
    """A quoted field that may be empty ("") — parse's default `{}` type
    requires 1+ characters, which never matches the invalid-request Scenario
    Outline's blank-id rows (Examples cells `""` / literally two adjacent
    quote characters). Same `with_pattern` pattern
    tests/bdd/steps/admin_breakdown_and_fold_steps.py's `Unquoted` type uses,
    just zero-or-more instead of one-or-more."""
    return text


register_type(MaybeEmpty=_parse_maybe_empty)


def _slug(name: str) -> str:
    """Same deterministic slug account_deletion_engagement_purge_steps.py's
    `_seed_venue` uses ("Bar Alfa" -> "bar_alfa"), duplicated locally rather
    than imported (see module docstring) — both files derive the identical
    venue_id independently because the Background step that actually seeds
    the venue runs in that other file."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or name


def _venue_id(name: str) -> str:
    # A deliberately blank name (the invalid-request Scenario Outline) must
    # stay blank — never slugged into a non-empty placeholder — or the 422
    # rejection scenario would silently stop testing a blank venue_id.
    if not name:
        return name
    return _slug(name)


def _pseudo(context, user_id: str) -> str:
    return context.engagement_service.pseudonymize(user_id)


def _adp(context) -> dict:
    # Shares the exact storage cell the reused "the request is rejected as
    # invalid" step (account_deletion_engagement_purge_steps.py) reads from.
    # Only ever touches the "error"/"result" keys here — never resets the
    # whole dict — so it never clobbers that module's own "venues" mapping
    # seeded by the shared Background step.
    if getattr(context, "adp", None) is None:
        context.adp = {"error": None}
    return context.adp


def _do_block(context, uid: str, vname: str) -> None:
    vid = _venue_id(vname)
    context.uid, context.vid = uid, vid
    context.response = context.client.post(
        "/v1/blocks", json={"user_id": uid, "venue_id": vid}
    )
    state = _adp(context)
    if context.response.status_code >= 400:
        state["error"] = RuntimeError(
            f"POST /v1/blocks rejected: {context.response.status_code} {context.response.text}"
        )
    else:
        state["error"] = None


def _do_unblock(context, uid: str, vname: str) -> None:
    vid = _venue_id(vname)
    context.uid, context.vid = uid, vid
    context.response = context.client.request(
        "DELETE", "/v1/blocks", json={"user_id": uid, "venue_id": vid}
    )
    state = _adp(context)
    state["error"] = None if context.response.status_code < 400 else RuntimeError(
        f"DELETE /v1/blocks rejected: {context.response.status_code} {context.response.text}"
    )


# ── blocking ─────────────────────────────────────────────────────────────────
# {uid:MaybeEmpty}/{vname:MaybeEmpty}, not the default type: the invalid-request
# Scenario Outline substitutes a literal "" (empty) for one of these two ids,
# and parse's default field type requires 1+ characters.
@given('user "{uid:MaybeEmpty}" blocks venue "{vname:MaybeEmpty}" through the engagement API')
@when('user "{uid:MaybeEmpty}" blocks venue "{vname:MaybeEmpty}" through the engagement API')
def step_block(context, uid, vname):
    _do_block(context, uid, vname)


@when('user "{uid}" blocks venue "{vname}" through the engagement API again')
def step_block_again(context, uid, vname):
    _do_block(context, uid, vname)


@then('RDS holds an active block for user "{uid}" on "{vname}"')
def step_rds_block_active(context, uid, vname):
    row = context.rds_store.get_block(_pseudo(context, uid), _venue_id(vname))
    assert row is not None and row.get("deleted_at") is None, row


@then("Redis holds the block so vibes_bot can read it")
def step_redis_block(context):
    assert context.fake_redis.sismember(f"user_blocked_venues:{context.uid}", context.vid)


@then("the block response reports that no favorite was removed")
def step_block_no_favorite_removed(context):
    assert context.response.status_code < 400, context.response.text
    assert context.response.json()["favorite_removed"] is False


@then("the block response reports that a favorite was removed")
def step_block_favorite_removed(context):
    assert context.response.status_code < 400, context.response.text
    assert context.response.json()["favorite_removed"] is True


@then('RDS holds exactly one block row for user "{uid}" on "{vname}"')
def step_rds_exactly_one_block(context, uid, vname):
    pseudo, vid = _pseudo(context, uid), _venue_id(vname)
    # Dict-keyed by (pseudo, venue_id) — a repeat block can never produce a
    # second row; this asserts the (sole) row exists.
    assert (pseudo, vid) in context.rds_store.blocked_venues
    assert context.rds_store.get_block(pseudo, vid) is not None


@then("the block is still active")
def step_block_still_active_last(context):
    row = context.rds_store.get_block(_pseudo(context, context.uid), context.vid)
    assert row is not None and row.get("deleted_at") is None, row


# ── unblocking ───────────────────────────────────────────────────────────────
@when('user "{uid}" unblocks venue "{vname}" through the engagement API')
def step_unblock(context, uid, vname):
    _do_unblock(context, uid, vname)


@then('RDS no longer holds an active block for user "{uid}" on "{vname}"')
def step_rds_block_no_longer_active(context, uid, vname):
    row = context.rds_store.get_block(_pseudo(context, uid), _venue_id(vname))
    assert row is not None and row.get("deleted_at") is not None, row


@then('RDS does not hold an active block for user "{uid}" on "{vname}"')
def step_rds_block_absent_or_inactive(context, uid, vname):
    # Covers both "never blocked" (no row at all) and "blocked then
    # unblocked" (row exists, soft-deleted) — both read as "not an active
    # block" to a caller.
    row = context.rds_store.get_block(_pseudo(context, uid), _venue_id(vname))
    assert row is None or row.get("deleted_at") is not None, row


@then('RDS still holds an active block for user "{uid}" on "{vname}"')
def step_rds_block_still_active(context, uid, vname):
    row = context.rds_store.get_block(_pseudo(context, uid), _venue_id(vname))
    assert row is not None and row.get("deleted_at") is None, row


# ── favorites interaction (mutual exclusion) ───────────────────────────────────
@then('the favorite for "{uid}" on "{vname}" is no longer active')
def step_favorite_no_longer_active(context, uid, vname):
    row = context.rds_store.get_favorite(_pseudo(context, uid), _venue_id(vname))
    assert row is not None and row.get("deleted_at") is not None, row


@then('the favorites projection for "{uid}" no longer includes "{vname}"')
def step_favorites_projection_excludes(context, uid, vname):
    assert not context.fake_redis.sismember(f"user_favorites:{uid}", _venue_id(vname))


@then('RDS still does not hold an active favorite for "{uid}" on "{vname}"')
def step_favorite_still_inactive(context, uid, vname):
    row = context.rds_store.get_favorite(_pseudo(context, uid), _venue_id(vname))
    assert row is None or row.get("deleted_at") is not None, row


@then('RDS still holds an active favorite for "{uid}" on "{vname}"')
def step_favorite_still_active(context, uid, vname):
    row = context.rds_store.get_favorite(_pseudo(context, uid), _venue_id(vname))
    assert row is not None and row.get("deleted_at") is None, row


# ── erasure ──────────────────────────────────────────────────────────────────
@then('no blocked-venue rows remain for "{uid}"')
def step_no_blocked_rows(context, uid):
    pseudo = _pseudo(context, uid)
    rows = [k for k in context.rds_store.blocked_venues if k[0] == pseudo]
    assert rows == [], f"{len(rows)} blocked_venue row(s) still bear the pseudonym: {rows}"


@then('the blocked-venues projection for "{uid}" is absent')
def step_blocked_projection_absent(context, uid):
    key = f"user_blocked_venues:{uid}"
    assert not context.fake_redis.exists(key), (
        f"{key} still exists with {context.fake_redis.smembers(key)}"
    )


@then("the deletion reports the number of blocked venues removed")
def step_deletion_reports_blocked_count(context):
    result = _adp(context).get("result")
    assert result is not None, "no deletion result recorded"
    # This scenario's user-a blocked exactly one venue ("Bar Alfa").
    assert result.get("blocked_venues") == 1, result
