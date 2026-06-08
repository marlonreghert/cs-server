"""Ex2: single-rule eligibility editing over the normalized admin.eligibility_rule
rows, with the Redis mirror kept as a derived projection.

Rows are the source of truth. Every write reassembles an equivalent
`admin_config:venue_eligibility` blob from the rows (same effective config; the
block-lists are membership sets, so element order may normalize) and pushes it
through `AdminConfigService` (RDS admin_config row + Redis mirror), so serving /
refresh / vibes_bot keep reading the fast mirror unchanged. When no rows remain the
override is removed and readers fall back to the hardcoded defaults.

See plans/260605_rds-schema-normalization.md (Step Ex2).
"""
from __future__ import annotations

import logging

from app.metrics import ELIGIBILITY_MIRROR_REHYDRATION_TOTAL
from app.services.venue_eligibility import (
    EligibilityConfig,
    RULE_TYPES,
    assemble_eligibility_blob,
    decompose_eligibility_blob,
    eligibility_config_from_rules,
    normalize_rule_value,
)

logger = logging.getLogger(__name__)

_ELIGIBILITY_KEY = "venue_eligibility"


class EligibilityRuleService:
    def __init__(self, rds_store, admin_config_service):
        self.rds_store = rds_store
        self.admin_config_service = admin_config_service

    # ── reads ────────────────────────────────────────────────────────────────
    def effective_config(self) -> EligibilityConfig:
        """The effective config assembled from the rows (defaults when empty)."""
        return eligibility_config_from_rules(self.rds_store.list_eligibility_rules())

    # ── writes (rows are truth; the mirror is reassembled from them) ───────────
    def add_rule(self, rule_type: str, value: str, updated_by=None) -> EligibilityConfig:
        rt, v = self._validate(rule_type, value)
        self.rds_store.add_eligibility_rule(rt, v, updated_by)
        logger.info("[eligibility] rule added %s=%r by %s", rt, v, updated_by)
        return self._remirror(updated_by)

    def remove_rule(self, rule_type: str, value: str, updated_by=None) -> EligibilityConfig:
        rt, v = self._validate(rule_type, value)
        self.rds_store.remove_eligibility_rule(rt, v)
        logger.info("[eligibility] rule removed %s=%r by %s", rt, v, updated_by)
        return self._remirror(updated_by)

    def set_full_config(self, blob: dict, updated_by=None) -> EligibilityConfig:
        """Replace all rows from a full override blob (validated), then re-mirror."""
        EligibilityConfig.from_dict(blob, from_admin_override=True)  # raises on invalid
        self.rds_store.replace_eligibility_rules(
            decompose_eligibility_blob(blob), updated_by
        )
        return self._remirror(updated_by)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _validate(self, rule_type: str, value: str) -> tuple[str, str]:
        if rule_type not in RULE_TYPES:
            raise ValueError(f"unknown eligibility rule_type: {rule_type!r}")
        v = (value or "").strip()
        if not v:
            raise ValueError("eligibility rule value must be non-empty")
        return rule_type, normalize_rule_value(rule_type, v)

    def _project_mirror(self, rules) -> None:
        """Write the Redis serving mirror directly from the rows (or clear it when
        no rows remain). Redis-only: the rows are the sole durable truth, so no
        redundant RDS admin_config blob is persisted."""
        if rules:
            self.admin_config_service.set_mirror(
                _ELIGIBILITY_KEY, assemble_eligibility_blob(rules)
            )
        else:
            # No override left -> drop the key so readers fall back to defaults.
            self.admin_config_service.delete_mirror(_ELIGIBILITY_KEY)

    def _remirror(self, updated_by=None) -> EligibilityConfig:
        # Admin write path: propagate errors so the caller surfaces a retryable
        # failure (the router maps this to HTTP 502).
        rules = self.rds_store.list_eligibility_rules()
        self._project_mirror(rules)
        return eligibility_config_from_rules(rules)

    def rehydrate_mirror(self) -> None:
        """Rebuild the Redis eligibility mirror from the rows. Called at startup
        and from the periodic projector cycle so a Redis flush self-heals instead
        of silently degrading filtering to the hardcoded defaults. Degrade-safe:
        an RDS/Redis error is logged + counted and never raised (must not block
        startup or abort the projector)."""
        try:
            rules = self.rds_store.list_eligibility_rules()
            self._project_mirror(rules)
        except Exception as e:
            logger.warning("[eligibility] mirror rehydration failed: %s", e)
            ELIGIBILITY_MIRROR_REHYDRATION_TOTAL.labels(result="failure").inc()
            return
        logger.info("[eligibility] mirror rehydrated from %d eligibility rule rows", len(rules))
        ELIGIBILITY_MIRROR_REHYDRATION_TOTAL.labels(result="success").inc()
