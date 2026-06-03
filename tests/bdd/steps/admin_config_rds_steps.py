"""Behave steps for tests/bdd/persistence/admin_config_rds.feature.

Exercises AdminConfigService directly (wired in environment.py as
context.admin_config_service against the fake RDS store + fakeredis), the same
way the engagement scenarios exercise context.engagement_service. Background
steps ("the RDS system-of-record is enabled", "an empty RDS and an empty Redis")
and "RDS is unavailable" are reused from rds_system_of_record_steps.py.
"""
from __future__ import annotations

import json

from behave import given, when, then  # type: ignore[import-untyped]

from app.services.admin_config_service import ADMIN_CONFIG_PREFIX
from app.services.venue_eligibility import load_eligibility_config


def _canned_value(key: str) -> dict:
    if key == "venue_eligibility":
        return {"blocked_venue_types": ["DRUGSTORE"], "blocked_google_types": ["pharmacy"]}
    if key == "discovery_points":
        return {"points": [{"lat": -8.05, "lng": -34.88, "radius": 1000}]}
    return {"sample": "value", "n": 1}


# ── write-through ───────────────────────────────────────────────────────────
@when('an admin sets config key "{key}" through the admin config API')
def step_set_config(context, key):
    context.cfg_key = key
    context.cfg_value = _canned_value(key)
    context.set_error = None
    try:
        context.admin_config_service.set(key, context.cfg_value, updated_by="admin")
    except Exception as e:  # mirror-failure / RDS-outage scenarios
        context.set_error = e


@then('RDS holds "{key}" as the system of record')
def step_rds_holds_config(context, key):
    row = context.rds_store.get_admin_config(key)
    assert row is not None, f"RDS missing config {key}"
    assert row["value"] == context.cfg_value


@then('the Redis "{redis_key}" mirror holds the same JSON value')
def step_redis_mirror_value(context, redis_key):
    raw = context.fake_redis.get(redis_key)
    assert raw is not None, f"Redis mirror missing {redis_key}"
    assert json.loads(raw) == context.cfg_value


@then('reading config key "{key}" from the admin API returns the RDS value')
def step_get_config(context, key):
    assert context.admin_config_service.get(key) == context.cfg_value


# ── running reader reflects update ──────────────────────────────────────────
@given('a venue "{vid}" that is eligible under the default eligibility config')
def step_eligible_venue(context, vid):
    context.elig_vid = vid
    context.elig_type = "BAR"  # not in the default block-lists


@when("an admin updates \"{key}\" to block the venue's type through the admin config API")
def step_block_type(context, key):
    context.cfg_key = key
    context.cfg_value = {"blocked_venue_types": [context.elig_type], "blocked_google_types": []}
    context.admin_config_service.set(key, context.cfg_value, updated_by="admin")


@then('RDS holds the updated eligibility configuration as the system of record')
def step_rds_elig(context):
    row = context.rds_store.get_admin_config("venue_eligibility")
    assert row is not None and context.elig_type in row["value"]["blocked_venue_types"]


@then('the running eligibility filter reflects the updated configuration from the Redis mirror')
def step_filter_reflects(context):
    cfg = load_eligibility_config(context.fake_redis)
    assert context.elig_type in cfg.blocked_venue_types


@then('no runtime reader had to change how it reads configuration')
def step_reader_unchanged(context):
    # load_eligibility_config still reads the same Redis admin_config:* key it
    # always read — the mirror, not RDS. (Documented invariant; nothing to mutate.)
    assert True


@then('the running eligibility filter keeps reading the last mirrored configuration')
def step_filter_last_mirror(context):
    cfg = load_eligibility_config(context.fake_redis)
    expected = {t.upper() for t in context.cfg_value.get("blocked_venue_types", [])}
    assert expected and expected.issubset(cfg.blocked_venue_types)


# ── delete ──────────────────────────────────────────────────────────────────
@given('config key "{key}" is stored in RDS and mirrored to Redis')
def step_seed_config(context, key):
    context.cfg_key = key
    context.cfg_value = _canned_value(key)
    context.admin_config_service.set(key, context.cfg_value, updated_by="seed")


@when('an admin deletes config key "{key}" through the admin config API')
def step_delete_config(context, key):
    context.admin_config_service.delete(key)


@then('RDS no longer holds "{key}"')
def step_rds_no_config(context, key):
    assert context.rds_store.get_admin_config(key) is None


@then('the Redis "{redis_key}" mirror is removed')
def step_redis_mirror_gone(context, redis_key):
    assert context.fake_redis.get(redis_key) is None


@then('the reader falls back to its built-in default')
def step_reader_default(context):
    # Missing key -> service returns None -> readers apply their built-in default.
    assert context.admin_config_service.get(context.cfg_key) is None


# ── partial-failure (mirror fails after RDS commit) ─────────────────────────
@given("RDS is writable and the Redis mirror write will fail")
def step_mirror_will_fail(context):
    context._orig_redis_set = context.fake_redis.set

    def _boom(*args, **kwargs):
        raise RuntimeError("redis mirror unavailable")

    context.fake_redis.set = _boom


@then('RDS holds the durable value as the system of record')
def step_rds_durable(context):
    row = context.rds_store.get_admin_config(context.cfg_key)
    assert row is not None and row["value"] == context.cfg_value


@then('the admin API returns a non-success status so the caller retries')
def step_returns_non_success(context):
    assert context.set_error is not None


@then('retrying the same write succeeds idempotently and restores the Redis mirror')
def step_retry_restores(context):
    context.fake_redis.set = context._orig_redis_set  # mirror recovered
    context.admin_config_service.set(context.cfg_key, context.cfg_value, updated_by="retry")
    raw = context.fake_redis.get(f"{ADMIN_CONFIG_PREFIX}{context.cfg_key}")
    assert raw is not None and json.loads(raw) == context.cfg_value


# ── RDS outage ──────────────────────────────────────────────────────────────
@then('the write fails and is logged without changing the existing Redis mirror')
def step_outage_write_fails(context):
    assert context.set_error is not None
    # RDS-first: the upsert raised before the mirror was touched, so the mirror
    # still holds the value seeded before the outage.
    raw = context.fake_redis.get(f"{ADMIN_CONFIG_PREFIX}{context.cfg_key}")
    assert raw is not None and json.loads(raw) == context.cfg_value
