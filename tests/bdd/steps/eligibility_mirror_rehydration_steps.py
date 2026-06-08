"""Behave steps for tests/bdd/persistence/eligibility-mirror-rehydration.feature.

Drives the startup/projector rehydration of the Redis
admin_config:venue_eligibility mirror from the admin.eligibility_rule rows,
through the harness wired by environment.py (context.eligibility_rule_service,
context.rds_store, context.fake_redis, context.redis_projection_service). The
Background ("RDS system-of-record is enabled", "an empty RDS and an empty Redis")
and "RDS is unavailable" are shared from rds_system_of_record_steps.py.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.metrics import ELIGIBILITY_MIRROR_REHYDRATION_TOTAL
from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    eligibility_config_from_rules,
    evaluate,
    load_eligibility_config,
)


def _live_config(context):
    """The eligibility config serving would read right now (from the mirror)."""
    return load_eligibility_config(context.fake_redis)


def _failure_count() -> float:
    return ELIGIBILITY_MIRROR_REHYDRATION_TOTAL.labels(result="failure")._value.get()


# ── given ────────────────────────────────────────────────────────────────────
@given('an eligibility rule blocks the name keyword "{kw}"')
def step_rule_blocks_keyword(context, kw):
    context.eligibility_rule_service.add_rule("hard_blocked_name_keyword", kw, updated_by="test")


@given('there are no eligibility rules')
def step_no_rules(context):
    assert context.rds_store.list_eligibility_rules() == []


@given('the Redis eligibility mirror is then cleared')
def step_clear_mirror(context):
    context.fake_redis.delete(ADMIN_CONFIG_ELIGIBILITY_KEY)
    assert context.fake_redis.get(ADMIN_CONFIG_ELIGIBILITY_KEY) is None


# ── when ─────────────────────────────────────────────────────────────────────
@when('cs-server rehydrates the eligibility mirror on startup')
def step_rehydrate_startup(context):
    context.rehydrate_fail_before = _failure_count()
    context.rehydrate_error = None
    try:
        context.eligibility_rule_service.rehydrate_mirror()
    except Exception as e:  # rehydration must be degrade-safe; capture for assertion
        context.rehydrate_error = e


@when('the periodic projector runs a rebuild cycle')
def step_projector_cycle(context):
    context.rehydrate_fail_before = _failure_count()
    context.rehydrate_error = None
    context.redis_projection_service.rebuild_redis_from_rds()


@when('an eligibility rule blocking the name keyword "{kw}" is added')
def step_add_rule_when(context, kw):
    context.eligibility_rule_service.add_rule("hard_blocked_name_keyword", kw, updated_by="test")


# ── then ─────────────────────────────────────────────────────────────────────
@then('a venue named "{name}" is excluded by the eligibility filter')
def step_excluded(context, name):
    cfg = _live_config(context)
    result = evaluate(name, besttime_type=None, google_type=None, config=cfg)
    assert not result.eligible, (
        f"{name} should be excluded; hard_keywords="
        f"{cfg.to_public_dict().get('hard_blocked_name_keywords')}"
    )


@then('a venue named "{name}" is allowed by the eligibility filter')
def step_allowed(context, name):
    cfg = _live_config(context)
    assert evaluate(name, besttime_type=None, google_type=None, config=cfg).eligible, \
        f"{name} should be allowed under defaults"


@then('the live eligibility config came from the rows, not the hardcoded defaults')
def step_from_rows_not_defaults(context):
    cfg = _live_config(context)
    assert cfg.from_admin_override is True, (
        "live config should be an admin override rebuilt from the rows, not the "
        f"hardcoded defaults; source={cfg.to_public_dict().get('source')}"
    )


@then('the Redis eligibility mirror exists')
def step_mirror_exists(context):
    assert context.fake_redis.get(ADMIN_CONFIG_ELIGIBILITY_KEY) is not None, "mirror not written"


@then("its effective config equals the effective config of the rows")
def step_mirror_equals_rows(context):
    from_mirror = _live_config(context)
    from_rows = eligibility_config_from_rules(context.rds_store.list_eligibility_rules())
    assert from_mirror.to_public_dict() == from_rows.to_public_dict(), (
        from_mirror.to_public_dict(), from_rows.to_public_dict())


@then('no eligibility override mirror is present')
def step_no_mirror(context):
    assert context.fake_redis.get(ADMIN_CONFIG_ELIGIBILITY_KEY) is None


@then('startup completes without raising')
def step_no_raise(context):
    assert context.rehydrate_error is None, f"rehydration raised: {context.rehydrate_error!r}"


@then('a rehydration failure is recorded in the metrics')
def step_failure_metric(context):
    assert _failure_count() > context.rehydrate_fail_before, (
        "expected the rehydration failure counter to increment")


@then('no RDS admin_config row for "{key}" is persisted')
def step_no_rds_row(context, key):
    assert context.rds_store.get_admin_config(key) is None, (
        f"RDS admin_config row {key!r} should not be persisted (rows are the sole truth)")


@then('the live eligibility config blocks the name keyword "{kw}"')
def step_config_blocks_keyword(context, kw):
    cfg = _live_config(context)
    assert kw.lower() in {k.lower() for k in cfg.hard_blocked_name_keywords}, \
        cfg.hard_blocked_name_keywords
