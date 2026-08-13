"""The scheduled Instagram crawl's SEED lookback: admin config, not a
Settings constant.

See plans/260813_dormant-vs-broken-targets.md §B. Three targets that had
never produced a post (`armazem14.recifeantigo`, `downtownrecife`,
`burburinhobar`) were assumed broken; probed against Apify with the date
bound removed, all three answered normally — their newest posts are simply
older than the steady-state lookback (3 months). `burburinhobar` misses by
five days. A seed is a one-time, historical read with no reason to share the
steady-state window's cadence-driven shortness.

## Admin config, not a Settings env var

`app.config.Settings.crawl_default_initial_lookback` (default `"3 months"`)
already exists and remains the LAST-RESORT fallback (used when no
`AdminConfigService` is wired at all — see `ScheduledInstagramCrawlService.
_resolve_seed_lookback`), but changing it requires an env var change and a
redeploy. This module is the SAME "runtime-editable, no deploy needed" shape
`menu_lifecycle.py`/`promoter_event_visibility.py` already established for
their own admin-config keys — 12 months is a starting proposal, not a fact,
and changing it must not need a deploy.

## Per-target override: no new mechanism

The per-target override is `events.crawl_target.initial_lookback` (migration
0030_crawl_target) — already read ONLY on a stream's SEED run (`compute_bound`
ignores it once a cursor is set) and already runtime-editable through the
admin CRUD route, exactly the `results_limit`/`seed_results_limit` precedent
this plan is told to follow. Nothing new is added at the per-target layer;
this module only replaces what a target with NO override falls back to.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Apify's own relative-date grammar ("3 months", "45 days", "1 year") — the
# UNPARSED format `initial_lookback`/`crawl_default_initial_lookback` already
# store and forward verbatim to `onlyPostsNewerThan`. Defined HERE (not in
# app.services.instagram_crawl_service, which imports this SAME pattern
# instead of redefining it) so the seed-lookback admin-config validator and
# the crawl service's own best-effort local-cutoff parser
# (`_parse_relative_lookback`) can never drift apart on what counts as a
# valid lookback string. `app.services.instagram_crawl_service` already
# depends on `app.models` elsewhere in this codebase's layering
# (routers -> services -> models); importing it FROM there (rather than the
# reverse) avoids a circular import between this module and that one.
RELATIVE_LOOKBACK_RE = re.compile(
    r"^\s*(\d+)\s*(day|days|month|months|year|years)\s*$", re.IGNORECASE
)

# Bare key — AdminConfigService (app/services/admin_config_service.py) adds
# the `admin_config:` prefix itself; every caller here goes through that
# service, never raw Redis.
ADMIN_CONFIG_CRAWL_SEED_LOOKBACK_KEY = "crawl_seed_lookback"

# plans/260813_dormant-vs-broken-targets.md §B's own numbers: 12 months would
# have caught `burburinhobar` (misses the 3-month window by 5 days) and
# `armazem14.recifeantigo`, while still excluding `downtownrecife` (~28
# months dormant) — a starting proposal, not a fact, hence admin config
# rather than a code constant.
DEFAULT_CRAWL_SEED_LOOKBACK = "12 months"


def validate_crawl_seed_lookback_config(value) -> str:
    """Validate an admin write to `crawl_seed_lookback` before persistence
    (`AdminConfigService.set` dispatches here). Body shape: Apify's own
    relative-date syntax as a bare string (`"12 months"`, `"45 days"`,
    `"1 year"`) — the SAME grammar `initial_lookback`/`crawl_default_
    initial_lookback` already accept UNPARSED and forward verbatim to
    `onlyPostsNewerThan` (`RELATIVE_LOOKBACK_RE` above). Raises
    TypeError/ValueError on a malformed shape."""
    if not isinstance(value, str):
        raise TypeError(f"crawl_seed_lookback must be a string, got {type(value).__name__}")
    if not RELATIVE_LOOKBACK_RE.match(value):
        raise ValueError(
            f"crawl_seed_lookback must match Apify's relative-date syntax "
            f"(e.g. '12 months', '45 days', '1 year'), got {value!r}"
        )
    return value


__all__ = [
    "RELATIVE_LOOKBACK_RE",
    "ADMIN_CONFIG_CRAWL_SEED_LOOKBACK_KEY", "DEFAULT_CRAWL_SEED_LOOKBACK",
    "validate_crawl_seed_lookback_config",
]
