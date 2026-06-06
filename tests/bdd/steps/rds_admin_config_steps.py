"""Behave steps for tests/bdd/persistence/rds-admin-config.feature (Ex2).

Exercises the normalized eligibility rules + reassembled mirror through
context.eligibility_rule_service (wired in environment.py against the in-memory
fake + AdminConfigService over fakeredis). Background steps are shared.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.services.venue_eligibility import (
    ADMIN_CONFIG_ELIGIBILITY_KEY,
    DEFAULT_BLOCKED_GOOGLE_TYPES,
    EligibilityConfig,
    eligibility_config_from_rules,
    evaluate,
    load_eligibility_config,
)


def _eligible(context, name, google_type=None) -> bool:
    cfg = context.eligibility_rule_service.effective_config()
    return evaluate(name, besttime_type=None, google_type=google_type, config=cfg).eligible


# ── single-rule add ──────────────────────────────────────────────────────────
@given("the eligibility rules are stored as normalized rows")
def step_rules_mode(context):
    context.rule_count_before = len(context.rds_store.list_eligibility_rules())


@given('a venue named "{name}" with no Google type that is currently eligible')
def step_venue_eligible(context, name):
    assert _eligible(context, name), f"{name} should start eligible"


@when('an operator adds the blocked name keyword "{kw}" as a single rule row')
def step_add_keyword(context, kw):
    context.rule_count_before = len(context.rds_store.list_eligibility_rules())
    context.eligibility_rule_service.add_rule("hard_blocked_name_keyword", kw, updated_by="admin")


@then('the venue named "{name}" becomes ineligible by name keyword')
def step_now_ineligible_keyword(context, name):
    cfg = context.eligibility_rule_service.effective_config()
    result = evaluate(name, besttime_type=None, google_type=None, config=cfg)
    assert not result.eligible, f"{name} should be ineligible"
    assert result.reason == "ineligible_name_keyword", result.reason


@then("exactly one eligibility rule row was added and no other rule changed")
def step_one_row_added(context):
    after = len(context.rds_store.list_eligibility_rules())
    assert after == context.rule_count_before + 1, (context.rule_count_before, after)


# ── single-rule remove ───────────────────────────────────────────────────────
@given('the blocked name keyword "{kw}" is stored as a single rule row')
def step_seed_keyword(context, kw):
    context.eligibility_rule_service.add_rule("hard_blocked_name_keyword", kw, updated_by="admin")


@given('a venue named "{name}" with no Google type that is currently ineligible')
def step_venue_ineligible(context, name):
    assert not _eligible(context, name), f"{name} should start ineligible"


@when('an operator removes the blocked name keyword "{kw}"')
def step_remove_keyword(context, kw):
    context.eligibility_rule_service.remove_rule("hard_blocked_name_keyword", kw, updated_by="admin")


@then('the venue named "{name}" becomes eligible again')
def step_eligible_again(context, name):
    assert _eligible(context, name), f"{name} should be eligible again"


# ── equivalence: rows vs old blob ────────────────────────────────────────────
@given('an existing "venue_eligibility" JSON override with a blocked Google type "{gtype}"')
def step_existing_blob(context, gtype):
    context.blob = {"blocked_google_types": [gtype]}


@when("that configuration is decomposed into normalized rule rows")
def step_decompose(context):
    context.eligibility_rule_service.set_full_config(context.blob, updated_by="admin")


@then("the effective eligibility config assembled from the rows equals the config the JSON blob produced")
def step_parity(context):
    from_rows = eligibility_config_from_rules(context.rds_store.list_eligibility_rules())
    from_blob = EligibilityConfig.from_dict(context.blob, from_admin_override=True)
    assert from_rows.to_public_dict() == from_blob.to_public_dict()


# ── fail-safe: empty rows -> defaults ────────────────────────────────────────
@given("the normalized eligibility rule table is empty")
def step_empty_rules(context):
    assert context.rds_store.list_eligibility_rules() == []


@when("the effective eligibility config is assembled")
def step_assemble_effective(context):
    context.effective = context.eligibility_rule_service.effective_config()


@then("the evaluation uses the hardcoded default block-lists")
def step_uses_defaults(context):
    assert context.effective.blocked_google_types == frozenset(DEFAULT_BLOCKED_GOOGLE_TYPES)
    assert context.effective.from_admin_override is False


@then("eligibility filtering does not break")
def step_filtering_works(context):
    result = evaluate("Some Bar", besttime_type=None, google_type=None, config=context.effective)
    assert result.eligible is True


# ── mirror reassembled byte-compatibly ───────────────────────────────────────
@when('an operator adds the blocked Google type "{gtype}" as a single rule row')
def step_add_google_type(context, gtype):
    context.eligibility_rule_service.add_rule("blocked_google_type", gtype, updated_by="admin")


@then('the Redis "admin_config:venue_eligibility" mirror is written as the reassembled JSON')
def step_mirror_written(context):
    raw = context.fake_redis.get(ADMIN_CONFIG_ELIGIBILITY_KEY)
    assert raw is not None, "mirror not written"
    import json
    blob = json.loads(raw)
    assert "casino" in blob.get("blocked_google_types", []), blob


@then('load_eligibility_config reading that mirror blocks the Google type "{gtype}"')
def step_mirror_blocks(context, gtype):
    cfg = load_eligibility_config(context.fake_redis)
    assert gtype in cfg.blocked_google_types
    assert not evaluate("X", besttime_type=None, google_type=gtype, config=cfg).eligible


# ── admin read from rows ─────────────────────────────────────────────────────
@given('the blocked Google type "{gtype}" is stored as a single rule row')
def step_seed_google_type(context, gtype):
    context.eligibility_rule_service.add_rule("blocked_google_type", gtype, updated_by="admin")


@when("the admin eligibility config is read from the rows")
def step_admin_read(context):
    context.public = context.eligibility_rule_service.effective_config().to_public_dict()


@then('the blocked Google types include "{gtype}"')
def step_public_includes(context, gtype):
    assert gtype in context.public["blocked_google_types"], context.public["blocked_google_types"]
