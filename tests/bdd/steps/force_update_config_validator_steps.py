"""Behave steps for tests/bdd/api/force-update-config-validator.feature.

Exercises AdminConfigService directly (wired in environment.py as
context.admin_config_service against the fake RDS store + fakeredis), the same
way admin_config_rds_steps.py does. The HTTP router maps ValueError->400 already
and is out of scope here; what we assert is that the registered force_update
validator rejects malformed policies before anything is persisted to the RDS
system of record or the Redis serving mirror.
"""
from __future__ import annotations

import json

from behave import given, when, then  # type: ignore[import-untyped]

from app.services.admin_config_service import ADMIN_CONFIG_PREFIX

FORCE_UPDATE_KEY = "force_update"
FORCE_UPDATE_REDIS_KEY = f"{ADMIN_CONFIG_PREFIX}{FORCE_UPDATE_KEY}"


def _valid_block(supported: str, recommended: str) -> dict:
    return {
        "min_supported_version": supported,
        "min_recommended_version": recommended,
        "store_url": "https://apps.example.com/app",
    }


def _valid_policy() -> dict:
    return {
        "ios": _valid_block("1.2.0", "1.4.0"),
        "android": _valid_block("1.2.0", "1.4.0"),
    }


# ── given: build the policy body into context.cfg_value ──────────────────────
@given("a force_update policy with valid ios and android version floors")
def step_valid_policy(context):
    context.cfg_value = _valid_policy()


@given('a force_update policy whose ios min_supported_version is "{version}"')
def step_invalid_version(context, version):
    policy = _valid_policy()
    policy["ios"]["min_supported_version"] = version
    context.cfg_value = policy


@given(
    "a force_update policy where ios min_supported_version is above its "
    "min_recommended_version"
)
def step_inverted_floor(context):
    policy = _valid_policy()
    policy["ios"]["min_supported_version"] = "2.0.0"
    policy["ios"]["min_recommended_version"] = "1.5.0"
    context.cfg_value = policy


@given("a force_update policy whose android block has no store_url")
def step_missing_store_url(context):
    policy = _valid_policy()
    policy["android"].pop("store_url")
    context.cfg_value = policy


@given("a force_update policy whose ios store_url is not https")
def step_non_https_store_url(context):
    policy = _valid_policy()
    policy["ios"]["store_url"] = "http://apps.example.com/app"
    context.cfg_value = policy


@given('a force_update policy that includes a "{platform}" platform block')
def step_unknown_platform(context, platform):
    policy = _valid_policy()
    policy[platform] = _valid_block("1.0.0", "1.0.0")
    context.cfg_value = policy


@given("a stored force_update policy")
def step_stored_policy(context):
    context.cfg_value = _valid_policy()
    context.admin_config_service.set(
        FORCE_UPDATE_KEY, context.cfg_value, updated_by="seed"
    )


# ── when ─────────────────────────────────────────────────────────────────────
@when("an admin writes it to the force_update config key")
def step_write_policy(context):
    context.set_error = None
    try:
        context.admin_config_service.set(
            FORCE_UPDATE_KEY, context.cfg_value, updated_by="admin"
        )
    except Exception as e:  # validator raises ValueError/TypeError on invalid input
        context.set_error = e


@when("an admin deletes the force_update config key")
def step_delete_policy(context):
    context.admin_config_service.delete(FORCE_UPDATE_KEY)


# ── then: accept path ────────────────────────────────────────────────────────
@then("the write succeeds")
def step_write_succeeds(context):
    assert context.set_error is None, f"unexpected error: {context.set_error!r}"


@then("the policy is stored in the admin config system of record")
def step_stored_in_record(context):
    row = context.rds_store.get_admin_config(FORCE_UPDATE_KEY)
    assert row is not None, "RDS missing force_update row"
    assert row["value"] == context.cfg_value


@then("the policy is mirrored to the force_update Redis serving key")
def step_mirrored(context):
    raw = context.fake_redis.get(FORCE_UPDATE_REDIS_KEY)
    assert raw is not None, "Redis mirror missing force_update key"
    assert json.loads(raw) == context.cfg_value


# ── then: reject path ────────────────────────────────────────────────────────
@then("the write is rejected as invalid")
def step_write_rejected(context):
    assert context.set_error is not None, "expected the write to be rejected"
    assert isinstance(
        context.set_error, (ValueError, TypeError)
    ), f"expected ValueError/TypeError, got {context.set_error!r}"


@then("nothing is persisted for the force_update key")
def step_nothing_persisted(context):
    assert (
        context.rds_store.get_admin_config(FORCE_UPDATE_KEY) is None
    ), "RDS should hold no force_update row"
    assert (
        context.fake_redis.get(FORCE_UPDATE_REDIS_KEY) is None
    ), "Redis mirror should not be written"


# ── then: delete path ────────────────────────────────────────────────────────
@then("the record is removed from the system of record")
def step_record_removed(context):
    assert context.rds_store.get_admin_config(FORCE_UPDATE_KEY) is None


@then("the force_update Redis serving key is removed")
def step_mirror_removed(context):
    assert context.fake_redis.get(FORCE_UPDATE_REDIS_KEY) is None
