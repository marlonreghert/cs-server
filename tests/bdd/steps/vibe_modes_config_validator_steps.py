"""Behave steps for tests/bdd/api/vibe-modes-config-validator.feature.

Drives the real admin config write path over HTTP: context.client hits the
generic PUT/GET /admin/config/{key} routes, which read the real
AdminConfigService wired onto the shared container in environment.py. This
exercises the router's ValueError->400 mapping and the validate-before-write
ordering end to end, so a rejected payload leaves neither the RDS system of
record nor the Redis mirror touched. The vibe_modes validator itself is
registered in app/container.py (production) and mirrored in environment.py.
"""
from __future__ import annotations

import copy
import json

from behave import given, when, then  # type: ignore[import-untyped]

from app.services.admin_config_service import ADMIN_CONFIG_PREFIX, AdminConfigService

VIBE_MODES_KEY = "vibe_modes"
VIBE_MODES_URL = "/admin/config/vibe_modes"
VIBE_MODES_REDIS_KEY = f"{ADMIN_CONFIG_PREFIX}{VIBE_MODES_KEY}"


def _rds_value(context):
    row = context.rds_store.get_admin_config(VIBE_MODES_KEY)
    return row["value"] if row is not None else None


def _redis_mirror(context):
    raw = context.fake_redis.get(VIBE_MODES_REDIS_KEY)
    return json.loads(raw) if raw is not None else None


def _mode(mode_id: str, *, is_default: bool = False, enabled: bool = True, **overrides) -> dict:
    """A minimal well-formed mode object matching the vibes_bot reader shape."""
    mode = {
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
    mode.update(overrides)
    return mode


def _valid_modes() -> list[dict]:
    return [
        _mode("explorar", is_default=True),
        _mode("role_calmo"),
        _mode("jantar"),
    ]


def _seed_modes() -> list[dict]:
    """A valid array that DIFFERS from _valid_modes() so the accept scenario's
    round-trip assertions detect a genuine write, not a 200-returning no-op that
    leaves the seeded value in place."""
    return [_mode("seed_default", is_default=True), _mode("seed_other")]


def _find(modes: list[dict], mode_id: str) -> dict:
    for mode in modes:
        if mode["id"] == mode_id:
            return mode
    raise KeyError(f"mode {mode_id!r} not in fixture")


def _put(context, payload) -> None:
    context.payload = payload
    context.response = context.client.put(VIBE_MODES_URL, json=payload)


# ── background ───────────────────────────────────────────────────────────────
@given("the admin config service is wired with the vibe_modes validator")
def step_service_wired(context):
    # The service exists and the HTTP routes read the very same instance.
    assert isinstance(context.admin_config_service, AdminConfigService)
    assert (
        getattr(context.container, "admin_config_service", None)
        is context.admin_config_service
    ), "container.admin_config_service must be the real service the routes read"


@given("a well-formed vibe_modes array is currently stored")
def step_seed_modes(context):
    # Seed a valid array distinct from the one the accept scenario PUTs, so a
    # successful write is observable (stored != seed) and a rejected write leaves
    # exactly this value in place.
    context.seed = _seed_modes()
    context.admin_config_service.set(
        VIBE_MODES_KEY, copy.deepcopy(context.seed), updated_by="seed"
    )


# ── when ─────────────────────────────────────────────────────────────────────
@when(
    "the admin PUTs a vibe_modes array where every mode has a unique id, label, "
    "emoji, description, is_default, enabled, busyness_range, sort_strategy, "
    "affinity, and a complete filter"
)
def step_put_valid(context):
    _put(context, _valid_modes())


@when(
    'the admin PUTs a vibe_modes array where mode "{mode_id}" has a filter '
    'without "{field}"'
)
def step_put_filter_missing_field(context, mode_id, field):
    modes = _valid_modes()
    _find(modes, mode_id)["filter"].pop(field, None)
    _put(context, modes)


@when('the admin PUTs a vibe_modes array where mode "{mode_id}" has no "{field}"')
def step_put_mode_missing_field(context, mode_id, field):
    modes = _valid_modes()
    _find(modes, mode_id).pop(field, None)
    _put(context, modes)


@when(
    'the admin PUTs a valid vibe_modes array where one mode carries '
    '"requires_family_signal" inside its filter and "trajectory_weight" at the '
    'top level'
)
def step_put_extra_keys(context):
    modes = _valid_modes()
    context.extra_mode_id = modes[0]["id"]
    modes[0]["filter"]["requires_family_signal"] = True
    modes[0]["trajectory_weight"] = 0.5
    _put(context, modes)


@when("the admin PUTs a JSON object instead of an array to the vibe_modes key")
def step_put_object(context):
    _put(context, {"modes": _valid_modes()})


@when("the admin PUTs an empty vibe_modes array")
def step_put_empty(context):
    _put(context, [])


@when('the admin PUTs a vibe_modes array containing two modes with id "{mode_id}"')
def step_put_duplicate_ids(context, mode_id):
    modes = _valid_modes()
    modes[0]["id"] = mode_id
    modes[1]["id"] = mode_id
    modes[1]["is_default"] = False  # keep duplicate-id the sole violation
    _put(context, modes)


@when("the admin PUTs a vibe_modes array where every mode has enabled set to false")
def step_put_all_disabled(context):
    modes = _valid_modes()
    for mode in modes:
        mode["enabled"] = False
    _put(context, modes)


@when("the admin PUTs a vibe_modes array where two modes have is_default set to true")
def step_put_two_defaults(context):
    modes = _valid_modes()
    modes[0]["is_default"] = True
    modes[1]["is_default"] = True
    _put(context, modes)


@when("the admin PUTs a vibe_modes array where one mode has busyness_range [3, 1]")
def step_put_bad_busyness_range(context):
    modes = _valid_modes()
    modes[0]["busyness_range"] = [3, 1]
    _put(context, modes)


@when('the admin PUTs a vibe_modes array where one mode has sort_strategy "{strategy}"')
def step_put_bad_sort_strategy(context, strategy):
    modes = _valid_modes()
    modes[0]["sort_strategy"] = strategy
    _put(context, modes)


@when('the admin PUTs a vibe_modes array where one quality gate has no "{field}"')
def step_put_bad_quality_gate(context, field):
    modes = _valid_modes()
    _find(modes, "explorar")["filter"]["quality_gates"][0].pop(field, None)
    _put(context, modes)


@when('the admin PUTs a vibe_modes array where one mode has a non-numeric "{field}"')
def step_put_bad_trajectory_weight(context, field):
    modes = _valid_modes()
    modes[0][field] = "high"
    _put(context, modes)


@when(
    'the admin PUTs a vibe_modes array where one mode filter has a non-boolean "{field}"'
)
def step_put_bad_family_signal(context, field):
    modes = _valid_modes()
    modes[0]["filter"][field] = "false"
    _put(context, modes)


# ── then ─────────────────────────────────────────────────────────────────────
# NOTE: `the response status is {status:d}` is shared (defined in
# user_activity_tracking_steps.py); it asserts context.response.status_code,
# which _put() sets. Do not redefine it here — behave loads steps globally.


@then("the stored vibe_modes value equals the submitted array")
def step_stored_equals_submitted(context):
    # Assert both the RDS system of record and the Redis serving mirror.
    assert _rds_value(context) == context.payload, "RDS row must hold the new array"
    assert _redis_mirror(context) == context.payload, "Redis mirror must be updated"


@then("GET /admin/config/vibe_modes returns the submitted array")
def step_get_returns_submitted(context):
    resp = context.client.get(VIBE_MODES_URL)
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == context.payload


@then('the error detail names mode "{mode_id}" and field "{field}"')
def step_detail_names_mode_and_field(context, mode_id, field):
    detail = context.response.json().get("detail", "")
    assert mode_id in detail, f"{mode_id!r} not named in detail: {detail!r}"
    assert field in detail, f"{field!r} not named in detail: {detail!r}"


@then('the error detail names the duplicated id "{mode_id}"')
def step_detail_names_duplicate(context, mode_id):
    detail = context.response.json().get("detail", "")
    assert mode_id in detail, f"{mode_id!r} not named in detail: {detail!r}"


@then('the error detail names field "{field}"')
def step_detail_names_field(context, field):
    detail = context.response.json().get("detail", "")
    assert field in detail, f"{field!r} not named in detail: {detail!r}"


@then("the stored vibe_modes value is unchanged")
def step_stored_unchanged(context):
    # Neither the RDS system of record nor the Redis mirror may change on reject.
    assert _rds_value(context) == context.seed, "RDS row must be untouched"
    assert _redis_mirror(context) == context.seed, "Redis mirror must be untouched"


@then('the stored mode still contains "{filter_key}" and "{top_key}" verbatim')
def step_extras_preserved(context, filter_key, top_key):
    # Assert the extras survived in BOTH the RDS system of record and the mirror.
    for label, stored in (("RDS", _rds_value(context)), ("mirror", _redis_mirror(context))):
        mode = _find(stored, context.extra_mode_id)
        assert mode.get(top_key) == 0.5, f"{label}: top-level {top_key!r} lost: {mode}"
        assert (
            mode["filter"].get(filter_key) is True
        ), f"{label}: filter {filter_key!r} lost: {mode['filter']}"
