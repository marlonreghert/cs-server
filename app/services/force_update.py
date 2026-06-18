"""Write-time validation for the ``force_update`` admin config policy.

The ``force_update`` admin config key defines, per platform, the minimum
supported and minimum recommended app versions plus the store URL and optional
messages. cs-server owns only the durable system of record (``admin.admin_config``)
and its Redis mirror (``admin_config:force_update``); vibes_bot reads the mirror
and makes the serve-time update-gate decision, and mobile renders it.

Because a hard gate with an inverted floor, a typo'd version, or a missing store
URL can block *every* user, the policy is validated before any write. This
mirrors the ``venue_eligibility`` precedent: the validator raises
``ValueError``/``TypeError`` on a malformed body and otherwise returns the body
unchanged, so the persisted bytes stay byte-compatible with what vibes_bot parses.

Stored shape (per configured platform; an absent platform block means "no policy
for that platform")::

    {
      "min_supported_version": "x.y.z",
      "min_recommended_version": "x.y.z",
      "store_url": "https://…",
      "hard_message"?: str,
      "soft_message"?: str
    }
"""
from __future__ import annotations

from typing import Any

ALLOWED_PLATFORMS = ("ios", "android")
REQUIRED_VERSION_FIELDS = ("min_supported_version", "min_recommended_version")
OPTIONAL_MESSAGE_FIELDS = ("hard_message", "soft_message")


def _parse_version(platform: str, field: str, raw: Any) -> tuple[int, int, int]:
    """Parse ``MAJOR.MINOR.PATCH`` into a comparable tuple of non-negative ints.

    Raises ``TypeError`` when the version is not a string and ``ValueError`` when
    it is not three dot-separated runs of ASCII digits (which also rejects signs,
    leading/trailing whitespace, and empty parts).
    """
    if not isinstance(raw, str):
        raise TypeError(
            f"force_update.{platform}.{field} must be a version string, "
            f"got {type(raw).__name__}"
        )
    parts = raw.split(".")
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        raise ValueError(
            f"force_update.{platform}.{field} must be semver "
            f"MAJOR.MINOR.PATCH of non-negative integers, got {raw!r}"
        )
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def validate_force_update_config(value: Any) -> Any:
    """Validate a ``force_update`` policy body, returning it unchanged if valid.

    Raises ``ValueError``/``TypeError`` (mapped to HTTP 400 by the admin router)
    on the first violation so nothing is ever persisted for a malformed policy.
    """
    if not isinstance(value, dict):
        raise TypeError(
            f"force_update policy must be an object, got {type(value).__name__}"
        )
    if not value:
        raise ValueError("force_update policy must define at least one platform")
    unknown = sorted(set(value) - set(ALLOWED_PLATFORMS))
    if unknown:
        raise ValueError(
            f"force_update policy has unknown platform key(s): {unknown}; "
            f"allowed: {list(ALLOWED_PLATFORMS)}"
        )

    for platform in ALLOWED_PLATFORMS:
        if platform not in value:
            continue  # absent platform block = no policy for that platform
        block = value[platform]
        if not isinstance(block, dict):
            raise TypeError(
                f"force_update.{platform} must be an object, "
                f"got {type(block).__name__}"
            )
        for field in REQUIRED_VERSION_FIELDS:
            if field not in block:
                raise ValueError(
                    f"force_update.{platform} is missing required field {field!r}"
                )
        supported = _parse_version(
            platform, "min_supported_version", block["min_supported_version"]
        )
        recommended = _parse_version(
            platform, "min_recommended_version", block["min_recommended_version"]
        )
        if supported > recommended:
            raise ValueError(
                f"force_update.{platform}.min_supported_version "
                f"({block['min_supported_version']}) must not exceed "
                f"min_recommended_version ({block['min_recommended_version']})"
            )

        store_url = block.get("store_url")
        if not isinstance(store_url, str) or not store_url:
            raise ValueError(f"force_update.{platform} is missing a store_url")
        if not store_url.startswith("https://"):
            raise ValueError(
                f"force_update.{platform}.store_url must be an https:// URL"
            )

        for field in OPTIONAL_MESSAGE_FIELDS:
            if field in block and not isinstance(block[field], str):
                raise TypeError(
                    f"force_update.{platform}.{field} must be a string when present"
                )

    return value
