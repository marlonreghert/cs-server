"""Write-time validation for the ``vibe_modes`` admin config array.

The ``vibe_modes`` admin config key is an ordered JSON **array** of mode objects
that drives venue filtering, sorting, and affinity scoring for every client. The
serving stack reads it defensively but still assumes a well-formed shape:
vibes_bot (``app/services/vibe_modes_service.py`` defaults + ``vibe_modes_evaluator``)
and mobile (``VibeModeConfig`` — ``filter.quality_gates`` is a required array).
A mode missing a required key, an empty array, or an all-disabled list breaks
mode selection downstream.

cs-server owns write-time validation (the same split as ``force_update``): this
validator runs BEFORE any write in ``AdminConfigService.set``, raising
``ValueError`` (mapped to HTTP 400 by the admin router) on the first violation so
a malformed payload never reaches the RDS system of record or the Redis mirror.
The message names the offending mode (by ``id``, or index when the id is absent)
and the failing field. A valid payload is returned **unchanged**: genuinely
unknown keys are preserved verbatim (forward-compatible — the serving stack
ignores them) and the persisted bytes stay byte-compatible with what the reader
parses. Two optional keys are NOT ignored by the reader, so they are validated
when present (and still preserved verbatim when valid): ``trajectory_weight``
(vibes_bot coerces it via ``float()`` for every enabled mode, so a non-numeric
value crashes serving) must be a number, and ``filter.requires_family_signal``
(a reader truthiness gate) must be a boolean.

Stored shape (per mode; ``filter`` nested)::

    {
      "id": str, "label": str, "emoji": str, "description": str,
      "is_default": bool, "enabled": bool,
      "busyness_range": [min, max],           # ints, 0 <= min <= max <= 4
      "sort_strategy": str,                    # one of SORT_STRATEGIES
      "affinity": {str: number},
      "filter": {
        "allowed_types": [str], "always_pass_types": [str],
        "excluded_granular_types": [str],
        "quality_gates": [{"types": [str], "min_rating": number,
                           "min_reviews": int}],
        "requires_open_late": bool,
        "vibe_label_matchers": [{"category": str, "labels": [str]}]
      }
    }
"""
from __future__ import annotations

from typing import Any

SORT_STRATEGIES = ("combined_score_desc", "busyness_desc", "rating_desc")
BUSYNESS_MIN = 0
BUSYNESS_MAX = 4

REQUIRED_MODE_FIELDS = (
    "id",
    "label",
    "emoji",
    "description",
    "is_default",
    "enabled",
    "busyness_range",
    "sort_strategy",
    "affinity",
    "filter",
)
REQUIRED_FILTER_FIELDS = (
    "allowed_types",
    "always_pass_types",
    "excluded_granular_types",
    "quality_gates",
    "requires_open_late",
    "vibe_label_matchers",
)
FILTER_STRING_ARRAYS = (
    "allowed_types",
    "always_pass_types",
    "excluded_granular_types",
)


def _is_number(value: Any) -> bool:
    """A JSON number (int or float) but not a bool (``bool`` subclasses ``int``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _mode_ref(mode: Any, index: int) -> str:
    """A human-readable reference to a mode: its id when usable, else its index."""
    if isinstance(mode, dict):
        mode_id = mode.get("id")
        if isinstance(mode_id, str) and mode_id:
            return f"mode {mode_id!r}"
    return f"mode at index {index}"


def _validate_busyness_range(ref: str, mode: dict) -> None:
    rng = mode["busyness_range"]
    if not isinstance(rng, list) or len(rng) != 2 or not all(_is_int(v) for v in rng):
        raise ValueError(
            f"vibe_modes {ref} field 'busyness_range' must be [min, max] integers, "
            f"got {rng!r}"
        )
    low, high = rng
    if not (BUSYNESS_MIN <= low <= high <= BUSYNESS_MAX):
        raise ValueError(
            f"vibe_modes {ref} field 'busyness_range' must satisfy "
            f"{BUSYNESS_MIN} <= min <= max <= {BUSYNESS_MAX}, got {rng!r}"
        )


def _validate_affinity(ref: str, mode: dict) -> None:
    affinity = mode["affinity"]
    if not isinstance(affinity, dict):
        raise ValueError(f"vibe_modes {ref} field 'affinity' must be an object")
    for key, weight in affinity.items():
        if not isinstance(key, str):
            raise ValueError(f"vibe_modes {ref} field 'affinity' keys must be strings")
        if not _is_number(weight):
            raise ValueError(
                f"vibe_modes {ref} field 'affinity.{key}' must be a number, "
                f"got {weight!r}"
            )


def _validate_quality_gates(ref: str, gates: Any) -> None:
    if not isinstance(gates, list):
        raise ValueError(
            f"vibe_modes {ref} field 'filter.quality_gates' must be an array"
        )
    for i, gate in enumerate(gates):
        field = f"filter.quality_gates[{i}]"
        if not isinstance(gate, dict):
            raise ValueError(f"vibe_modes {ref} field '{field}' must be an object")
        if not _is_string_list(gate.get("types")):
            raise ValueError(
                f"vibe_modes {ref} field '{field}.types' must be an array of strings"
            )
        if not _is_number(gate.get("min_rating")):
            raise ValueError(
                f"vibe_modes {ref} field '{field}.min_rating' must be a number"
            )
        if not _is_int(gate.get("min_reviews")):
            raise ValueError(
                f"vibe_modes {ref} field '{field}.min_reviews' must be an integer"
            )


def _validate_vibe_label_matchers(ref: str, matchers: Any) -> None:
    if not isinstance(matchers, list):
        raise ValueError(
            f"vibe_modes {ref} field 'filter.vibe_label_matchers' must be an array"
        )
    for i, matcher in enumerate(matchers):
        field = f"filter.vibe_label_matchers[{i}]"
        if not isinstance(matcher, dict):
            raise ValueError(f"vibe_modes {ref} field '{field}' must be an object")
        category = matcher.get("category")
        if not isinstance(category, str) or not category:
            raise ValueError(
                f"vibe_modes {ref} field '{field}.category' must be a non-empty string"
            )
        if not _is_string_list(matcher.get("labels")):
            raise ValueError(
                f"vibe_modes {ref} field '{field}.labels' must be an array of strings"
            )


def _validate_filter(ref: str, mode: dict) -> None:
    filt = mode["filter"]
    if not isinstance(filt, dict):
        raise ValueError(f"vibe_modes {ref} field 'filter' must be an object")
    for field in REQUIRED_FILTER_FIELDS:
        if field not in filt:
            raise ValueError(
                f"vibe_modes {ref} is missing required field 'filter.{field}'"
            )
    for field in FILTER_STRING_ARRAYS:
        if not _is_string_list(filt[field]):
            raise ValueError(
                f"vibe_modes {ref} field 'filter.{field}' must be an array of strings"
            )
    if not isinstance(filt["requires_open_late"], bool):
        raise ValueError(
            f"vibe_modes {ref} field 'filter.requires_open_late' must be a boolean"
        )
    # Optional but reader-consumed: familia sets requires_family_signal and the
    # evaluator uses it as a truthiness gate. Validate when present so a non-bool
    # (e.g. the string "false", which is truthy) can't silently defeat the gate.
    if "requires_family_signal" in filt and not isinstance(
        filt["requires_family_signal"], bool
    ):
        raise ValueError(
            f"vibe_modes {ref} field 'filter.requires_family_signal' must be a "
            f"boolean when present"
        )
    _validate_quality_gates(ref, filt["quality_gates"])
    _validate_vibe_label_matchers(ref, filt["vibe_label_matchers"])


def _validate_mode(mode: Any, index: int, seen_ids: set[str]) -> tuple[bool, bool]:
    """Validate one mode object, returning ``(is_default, enabled)``."""
    ref = _mode_ref(mode, index)
    if not isinstance(mode, dict):
        raise ValueError(
            f"vibe_modes {ref} must be an object, got {type(mode).__name__}"
        )
    for field in REQUIRED_MODE_FIELDS:
        if field not in mode:
            raise ValueError(f"vibe_modes {ref} is missing required field {field!r}")

    mode_id = mode["id"]
    if not isinstance(mode_id, str) or not mode_id:
        raise ValueError(f"vibe_modes {ref} field 'id' must be a non-empty string")
    if mode_id in seen_ids:
        raise ValueError(f"vibe_modes has duplicate mode id {mode_id!r}")
    seen_ids.add(mode_id)

    for field in ("label", "emoji", "description"):
        if not isinstance(mode[field], str):
            raise ValueError(f"vibe_modes {ref} field {field!r} must be a string")
    for field in ("is_default", "enabled"):
        if not isinstance(mode[field], bool):
            raise ValueError(f"vibe_modes {ref} field {field!r} must be a boolean")

    _validate_busyness_range(ref, mode)
    if mode["sort_strategy"] not in SORT_STRATEGIES:
        raise ValueError(
            f"vibe_modes {ref} field 'sort_strategy' must be one of "
            f"{list(SORT_STRATEGIES)}, got {mode['sort_strategy']!r}"
        )
    _validate_affinity(ref, mode)
    # Optional but reader-consumed: the evaluator runs float(trajectory_weight)
    # for every enabled mode, so a non-numeric value (string, null, list) is a
    # write-time crash vector even though it is not a required field.
    if "trajectory_weight" in mode and not _is_number(mode["trajectory_weight"]):
        raise ValueError(
            f"vibe_modes {ref} field 'trajectory_weight' must be a number "
            f"when present"
        )
    _validate_filter(ref, mode)

    return mode["is_default"], mode["enabled"]


def validate_vibe_modes_config(value: Any) -> Any:
    """Validate a ``vibe_modes`` array, returning it unchanged if valid.

    Raises ``ValueError`` (mapped to HTTP 400 by the admin router) on the first
    violation so nothing is ever persisted for a malformed payload. Unknown extra
    keys are preserved — the returned object is the same one passed in.
    """
    if not isinstance(value, list):
        raise ValueError(
            f"vibe_modes must be a JSON array of mode objects, got "
            f"{type(value).__name__}"
        )
    if not value:
        raise ValueError("vibe_modes must contain at least one mode")

    seen_ids: set[str] = set()
    enabled_count = 0
    default_count = 0
    for index, mode in enumerate(value):
        is_default, enabled = _validate_mode(mode, index, seen_ids)
        default_count += int(is_default)
        enabled_count += int(enabled)

    if enabled_count == 0:
        raise ValueError("vibe_modes must contain at least one enabled mode")
    if default_count > 1:
        raise ValueError(
            f"vibe_modes must have at most one default mode, got {default_count}"
        )

    return value
