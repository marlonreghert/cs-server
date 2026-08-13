"""Hide promoter-sourced events from admin reads without touching a row.

See plans/260813_hide-promoter-events.md. The operator is descoping promoter
accounts to finish a venue-coverage deliverable; promoter items outnumber
venue items roughly five to one in the admin console, and hiding them is
reversible while re-statusing 587 rows would not be (see the plan's "Hiding
beats staling" section). `events.post_item` never reaches the Redis serving
projection, so this is admin-surface only — nothing here can affect
vibes_bot's app API or mobile.

## Admin config, not a code constant

`hide_promoter_events` is admin config (Redis-backed, RDS system of record),
not a constant in `app/config.py` — the SAME Redis-mirror-with-fallback shape
`app.models.menu_lifecycle.load_menu_expiry_days` already established, which
this module mirrors exactly (including its None-redis_like escape hatch for a
caller with no wired Redis client). Bringing promoters back into scope must
not need a deploy.

## Unanimity, not "any"

An item is promoter-sourced only when EVERY attached `post_item_source` row
has `source_kind == "promoter_post"`. Cross-post merging
(`app.services.event_merge._absorb_unresolved_sibling` and the ordinary merge
path both do this via `reattach_event_sources`) can attach a promoter source
to an item that already carries a venue source — hiding that item would
remove real venue coverage, the exact opposite of this plan's purpose. An
item with NO sources at all is not promoter-sourced either (there is no
promoter evidence to unanimously agree on) and stays visible.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY = "admin_config:hide_promoter_events"

# Hidden by default — the operator's whole ask. Flipping it back to False
# (e.g. `AdminConfigService.set("hide_promoter_events", False)`) is the
# rollback; no code or deploy changes either way.
DEFAULT_HIDE_PROMOTER_EVENTS = True

PROMOTER_SOURCE_KIND = "promoter_post"


def validate_hide_promoter_events_config(value) -> bool:
    """Validate an admin write to `admin_config:hide_promoter_events` before
    persistence (AdminConfigService.set dispatches here). Body shape: a
    plain boolean. Raises TypeError on anything else — a truthy/falsy
    non-bool (e.g. 1, "true") must never be silently coerced."""
    if not isinstance(value, bool):
        raise TypeError("hide_promoter_events must be a boolean")
    return value


def load_hide_promoter_events(redis_like) -> tuple[bool, Optional[str]]:
    """Read the admin override, falling back to
    `DEFAULT_HIDE_PROMOTER_EVENTS` on any problem. Returns
    `(hide_promoter_events, fallback_reason)`; `fallback_reason` is None
    unless the caller should count a fallback metric — a missing key is NOT
    a fallback, it is the expected pre-first-write state (and here, the
    expected STEADY state too, since the shipped default already matches
    what the operator wants). Mirrors
    `app.models.menu_lifecycle.load_menu_expiry_days` exactly."""
    if redis_like is None:
        return DEFAULT_HIDE_PROMOTER_EVENTS, None
    try:
        raw = redis_like.get(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[promoter_event_visibility] config read failed, using default: {e}")
        return DEFAULT_HIDE_PROMOTER_EVENTS, "unreadable"
    if raw is None:
        return DEFAULT_HIDE_PROMOTER_EVENTS, None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"[promoter_event_visibility] config invalid JSON, using default: {e}")
        return DEFAULT_HIDE_PROMOTER_EVENTS, "invalid_json"
    try:
        return validate_hide_promoter_events_config(data), None
    except TypeError as e:
        logger.warning(f"[promoter_event_visibility] config invalid shape, using default: {e}")
        return DEFAULT_HIDE_PROMOTER_EVENTS, "invalid_shape"


def is_promoter_only_item(sources: list[dict]) -> bool:
    """The unanimity predicate (plan §A): True only when `sources` is
    non-empty AND every source's `source_kind` is `"promoter_post"`.

    - Empty list -> False. No sources is no promoter evidence either; an
      item with none stays visible (plan's own acceptance criterion).
    - Any non-promoter source (including one with a missing/None
      `source_kind`) -> False. A single venue-sourced post is enough to
      keep the whole item visible, regardless of how many promoter sources
      also announced it.
    - Every source promoter -> True.
    """
    if not sources:
        return False
    return all(s.get("source_kind") == PROMOTER_SOURCE_KIND for s in sources)


__all__ = [
    "ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY", "DEFAULT_HIDE_PROMOTER_EVENTS",
    "PROMOTER_SOURCE_KIND", "validate_hide_promoter_events_config",
    "load_hide_promoter_events", "is_promoter_only_item",
]
