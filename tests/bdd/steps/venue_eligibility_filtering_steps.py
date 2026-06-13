"""Behave steps for tests/bdd/refresh/venue_eligibility_filtering.feature.

Scoped to the admin-tunable eligibility CONFIGURATION surface (read + validate).
The destructive eligibility sweep, write-time born-deprecate, and serve-time
eligibility filter were retired in favour of the non-destructive serving view;
their scenarios moved to tests/bdd/persistence/eligibility-serving-view.feature.
"""
from __future__ import annotations

import asyncio
import importlib

from behave import given, when, then  # type: ignore[import-untyped]


def _admin_module():
    return importlib.import_module("app.routers.admin_trigger_router")


# ── Background ───────────────────────────────────────────────────────────────
@given(
    "the eligibility filter uses the default blocked types, blocked Google "
    "types, and blocked name keywords"
)
def step_default_filter(context):
    # Defaults are in effect whenever admin_config:venue_eligibility is absent.
    pass


# ── When ─────────────────────────────────────────────────────────────────────
@when("an operator requests the eligibility configuration")
def step_get_config(context):
    context.config_response = asyncio.run(_admin_module().get_eligibility_config())


@when("an operator submits an eligibility configuration with a non-list blocked-types value")
def step_submit_invalid_config(context):
    from fastapi import HTTPException

    context.invalid_config_error = None
    try:
        asyncio.run(
            _admin_module().update_eligibility_config(
                config={"blocked_venue_types": "not-a-list"}
            )
        )
    except HTTPException as e:
        context.invalid_config_error = e


# ── Then ─────────────────────────────────────────────────────────────────────
@then(
    "the response returns the active blocked types, blocked Google types, and "
    "blocked name keywords"
)
def step_config_returns_lists(context):
    body = context.config_response
    assert body["blocked_venue_types"], body
    assert body["blocked_google_types"], body
    assert body["hard_blocked_name_keywords"], body
    assert "ambiguous_name_keywords" in body, body


@then("the update is rejected with a validation error")
def step_update_rejected(context):
    assert context.invalid_config_error is not None
    assert context.invalid_config_error.status_code == 400


@then("the active eligibility configuration is unchanged")
def step_config_unchanged(context):
    body = asyncio.run(_admin_module().get_eligibility_config())
    assert body["source"] == "defaults", body
