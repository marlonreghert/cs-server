"""Behave steps for tests/bdd/api/admin-config-boolean-body.feature.

Drives the real admin config write path over HTTP: context.client hits the
generic PUT/GET /admin/config/{key} routes, which read the real
AdminConfigService wired onto the shared container in environment.py — the
SAME validator functions app/container.py registers in production (imported
directly from app.services.event_dedup / app.models.venue_category /
app.services.vibe_modes_config, not reimplemented here). This exercises the
router's Body-type gate and the validate-before-write ordering end to end, so
a bare boolean body reaches a boolean-typed key's validator as a native
Python bool, and object/array bodies for dict/list-typed keys are unaffected.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

CONFIG_URL = "/admin/config/{key}"
_BOOL_WORDS = {"true": True, "false": False}


def _url(key: str) -> str:
    return CONFIG_URL.format(key=key)


# ── background / preconditions ────────────────────────────────────────────────
@given("the admin config service is wired with its production validators")
def step_service_wired(context):
    svc = context.admin_config_service
    assert svc is not None, "context.admin_config_service is not wired"
    for key in (
        "event_dedup_handle_time_match_enabled",
        "venue_category_map",
        "vibe_modes",
    ):
        assert key in svc.validators, f"{key!r} has no validator wired for this scenario"


@given('the "{key}" config key is currently true')
def step_seed_true(context, key):
    # Seeded through the service directly (not the HTTP route under test) so
    # this precondition never depends on the bug this feature exists to fix.
    context.admin_config_service.set(key, True, updated_by="test-setup")


# ── bare boolean body ───────────────────────────────────────────────────────────
@when('the admin PUTs a bare boolean {value} to the "{key}" config key')
def step_put_bare_bool(context, value, key):
    context.response = context.client.put(_url(key), json=_BOOL_WORDS[value])


@then("the response value is the boolean {value}")
def step_response_value_is_bool(context, value):
    body = context.response.json()
    assert body["value"] is _BOOL_WORDS[value], body


@then("GET /admin/config/{key} returns the boolean {value}")
def step_get_returns_bool(context, key, value):
    resp = context.client.get(_url(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] is _BOOL_WORDS[value], resp.json()


# ── dict-typed key regression guard (venue_category_map) ───────────────────────
def _valid_category_map() -> dict:
    # Submitted verbatim so the validator's normalization (lowercase google
    # keys, uppercase besttime keys) is a no-op and the response value can be
    # compared to the submitted object with plain equality.
    return {"google": {"bakery": "FOOD_DRINK"}, "besttime": {}}


@when('the admin PUTs a well-formed category map object to the "{key}" config key')
def step_put_category_map(context, key):
    context.submitted_value = _valid_category_map()
    context.response = context.client.put(_url(key), json=context.submitted_value)


@then("the stored venue_category_map value equals the submitted object")
def step_category_map_equals_submitted(context):
    assert context.response.json()["value"] == context.submitted_value, context.response.text


@then("the stored venue_category_map value is unchanged")
def step_category_map_unchanged(context):
    # Fresh in-memory RDS + Redis per scenario (environment.py's
    # before_scenario), so "unchanged" from a never-stored key means still
    # absent after the rejected PUT.
    resp = context.client.get(_url("venue_category_map"))
    assert resp.status_code == 404, resp.text


# ── list-typed key regression guard (vibe_modes) ────────────────────────────────
def _mode(mode_id: str, *, is_default: bool = False, enabled: bool = True) -> dict:
    """A minimal well-formed vibe mode matching validate_vibe_modes_config's
    required shape. Duplicated (not imported) from
    tests/bdd/steps/vibe_modes_config_validator_steps.py's own _mode() so this
    feature's step module stays self-contained, matching this repo's existing
    per-feature steps-file convention."""
    return {
        "id": mode_id,
        "label": mode_id.replace("_", " ").title(),
        "emoji": "🔥",
        "description": f"{mode_id} vibe",
        "is_default": is_default,
        "enabled": enabled,
        "busyness_range": [0, 4],
        "sort_strategy": "combined_score_desc",
        "affinity": {"bar": 1.0},
        "filter": {
            "allowed_types": ["BAR"],
            "always_pass_types": [],
            "excluded_granular_types": [],
            "quality_gates": [
                {"types": ["BAR"], "min_rating": 4.0, "min_reviews": 5},
            ],
            "requires_open_late": False,
            "vibe_label_matchers": [
                {"category": "estilo_do_lugar", "labels": ["Lounge"]},
            ],
        },
    }


def _valid_vibe_modes() -> list[dict]:
    return [_mode("explorar", is_default=True), _mode("role_calmo")]


@when('the admin PUTs a well-formed vibe_modes array to the "{key}" config key')
def step_put_vibe_modes(context, key):
    context.submitted_value = _valid_vibe_modes()
    context.response = context.client.put(_url(key), json=context.submitted_value)


@then("the round-tripped vibe_modes value equals the submitted array")
def step_vibe_modes_equals_submitted(context):
    assert context.response.json()["value"] == context.submitted_value, context.response.text
