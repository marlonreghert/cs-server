"""Unit tests for the force_update admin config validator.

Table-driven coverage of each accept/reject rule in
app/services/force_update.py: semver parsing, floor ordering, store_url scheme,
unknown platform, required fields, and optional-message typing. The validator is
registered in app/container.py and the BDD harness; the end-to-end persist/mirror
contract is covered by tests/bdd/api/force-update-config-validator.feature.
"""
from __future__ import annotations

import pytest

from app.services.force_update import (
    _parse_version,
    validate_force_update_config,
)


def _block(supported="1.2.0", recommended="1.4.0", store_url="https://x.example/app", **extra):
    block = {
        "min_supported_version": supported,
        "min_recommended_version": recommended,
        "store_url": store_url,
    }
    block.update(extra)
    return block


# ── accept ───────────────────────────────────────────────────────────────────
ACCEPT_CASES = {
    "two platforms": {"ios": _block(), "android": _block()},
    "ios only": {"ios": _block()},
    "android only": {"android": _block()},
    "equal floors": {"ios": _block(supported="1.4.0", recommended="1.4.0")},
    "zero version": {"ios": _block(supported="0.0.0", recommended="0.0.1")},
    "with messages": {
        "ios": _block(hard_message="Please update", soft_message="Update available")
    },
}


@pytest.mark.parametrize("policy", ACCEPT_CASES.values(), ids=list(ACCEPT_CASES))
def test_valid_policies_pass_and_return_unchanged(policy):
    # Returns the exact same object so persisted bytes stay reader-compatible.
    assert validate_force_update_config(policy) is policy


# ── reject: ValueError ─────────────────────────────────────────────────────────
VALUE_ERROR_CASES = {
    "empty body": {},
    "unknown platform": {"ios": _block(), "web": _block()},
    "invalid version 2.x": {"ios": _block(supported="2.x")},
    "too few version parts": {"ios": _block(supported="1.2")},
    "too many version parts": {"ios": _block(supported="1.2.3.4")},
    "negative version part": {"ios": _block(supported="-1.2.3")},
    "non-digit version part": {"ios": _block(supported="1.beta.0")},
    "empty version part": {"ios": _block(supported="1..0")},
    "supported above recommended": {
        "ios": _block(supported="2.0.0", recommended="1.5.0")
    },
    "missing min_supported_version": {
        "ios": {"min_recommended_version": "1.4.0", "store_url": "https://x.example/app"}
    },
    "missing min_recommended_version": {
        "ios": {"min_supported_version": "1.2.0", "store_url": "https://x.example/app"}
    },
    "missing store_url": {
        "ios": {"min_supported_version": "1.2.0", "min_recommended_version": "1.4.0"}
    },
    "empty store_url": {"ios": _block(store_url="")},
    "non-https store_url": {"ios": _block(store_url="http://x.example/app")},
}


@pytest.mark.parametrize("policy", VALUE_ERROR_CASES.values(), ids=list(VALUE_ERROR_CASES))
def test_invalid_policies_raise_value_error(policy):
    with pytest.raises(ValueError):
        validate_force_update_config(policy)


# ── reject: TypeError ──────────────────────────────────────────────────────────
TYPE_ERROR_CASES = {
    "body not a dict": ["ios"],
    "platform block not a dict": {"ios": "1.2.0"},
    "version not a string": {"ios": _block(supported=120)},
    "hard_message not a string": {"ios": _block(hard_message=123)},
    "soft_message not a string": {"ios": _block(soft_message=["x"])},
}


@pytest.mark.parametrize("policy", TYPE_ERROR_CASES.values(), ids=list(TYPE_ERROR_CASES))
def test_invalid_policies_raise_type_error(policy):
    with pytest.raises(TypeError):
        validate_force_update_config(policy)


# ── nothing-persisted invariant: a rejected body is never mutated ──────────────
def test_validator_does_not_mutate_rejected_body():
    policy = {"ios": _block(supported="2.x")}
    snapshot = {"ios": dict(policy["ios"])}
    with pytest.raises(ValueError):
        validate_force_update_config(policy)
    assert policy == snapshot


# ── _parse_version helper ──────────────────────────────────────────────────────
def test_parse_version_returns_tuple_for_ordering():
    assert _parse_version("ios", "min_supported_version", "1.2.10") == (1, 2, 10)
    assert _parse_version("ios", "f", "1.2.10") > _parse_version("ios", "f", "1.2.9")


@pytest.mark.parametrize("raw", ["1.2", "1.2.3.4", "1.x.0", "", "v1.2.3", " 1.2.3"])
def test_parse_version_rejects_malformed_strings(raw):
    with pytest.raises(ValueError):
        _parse_version("ios", "min_supported_version", raw)


def test_parse_version_rejects_non_string():
    with pytest.raises(TypeError):
        _parse_version("ios", "min_supported_version", 120)
