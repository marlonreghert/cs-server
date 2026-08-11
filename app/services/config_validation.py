"""Shared type predicates for admin-config write-time validators.

One source of truth for "what counts as a JSON number / integer / string list"
across the per-key validators (``vibe_modes_config``, ``validate_geo_fence``,
future keys). The subtlety these encode: ``bool`` subclasses ``int`` in Python,
so a naive ``isinstance(value, (int, float))`` accepts ``true``/``false`` where
a number is required — every validator must share the bool-aware version or
the admin keys drift into enforcing different type contracts.

These are STRICT predicates for write-time rejection. Lenient runtime readers
that coerce-or-default (e.g. the eligibility config reader) intentionally do
not use them.
"""
from __future__ import annotations

from typing import Any


def is_number(value: Any) -> bool:
    """A JSON number (int or float) but not a bool (``bool`` subclasses ``int``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value: Any) -> bool:
    """A JSON integer, excluding bools."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_string_list(value: Any) -> bool:
    """A list whose every element is a string (an empty list qualifies)."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# ── instagram_discovery ──────────────────────────────────────────────────────
# plans/260811_instagram-discovery-admin-flags.md: the two add-time Instagram
# discovery spend switches (the Google-search tier, the LLM judge), settable
# from admin config so an operator can start/stop that spend without a
# redeploy. AddVenueHandler resolves this key fresh on every add (see
# AddVenueHandler._resolve_instagram_discovery_flags); the Settings values
# (app/config.py `instagram_google_search_enabled` / `instagram_judge_enabled`)
# are the fallback only when this key is absent or a field is unset.
INSTAGRAM_DISCOVERY_FIELDS = ("google_search_enabled", "judge_enabled")


def validate_instagram_discovery_config(value: Any) -> dict:
    """Validate an admin write to ``admin_config:instagram_discovery`` before
    persistence (``AdminConfigService.set`` dispatches here).

    Body shape: a JSON object with zero or more of the boolean fields
    ``google_search_enabled`` / ``judge_enabled``. Unknown fields and
    non-boolean values are rejected with ``ValueError``/``TypeError`` so a
    malformed write never reaches RDS or the Redis mirror. Returns the value
    unchanged (byte-compatible with what ``AddVenueHandler`` reads back) —
    an absent field simply means "no override for that field", not "disable
    it", which is exactly what the resolution helper's fallback-to-Settings
    behavior expects.
    """
    if not isinstance(value, dict):
        raise TypeError(
            f"instagram_discovery config must be an object, got {type(value).__name__}"
        )
    unknown = sorted(set(value) - set(INSTAGRAM_DISCOVERY_FIELDS))
    if unknown:
        raise ValueError(
            f"instagram_discovery config has unknown field(s): {unknown}; "
            f"allowed: {list(INSTAGRAM_DISCOVERY_FIELDS)}"
        )
    for field in INSTAGRAM_DISCOVERY_FIELDS:
        if field in value and not isinstance(value[field], bool):
            raise TypeError(
                f"instagram_discovery.{field} must be a boolean, got "
                f"{type(value[field]).__name__}"
            )
    return value
