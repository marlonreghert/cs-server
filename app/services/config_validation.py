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
