"""Admin config write-through: RDS is the system of record, Redis a mirror.

Admin configuration is owned by RDS (`admin.admin_config`) and mirrored into the
existing Redis `admin_config:*` keys in the SAME request, so every runtime reader
(cs-server's eligibility/budget/discovery/photo-TTL readers and vibes_bot's
readers) keeps reading the Redis mirror unchanged.

This is a synchronous RDS-write-then-Redis-mirror carve-out (the same shape as
EngagementService), NOT the venue projector: config keys are global (not
venue-keyed), need immediate read-back, and are single-row writes.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ADMIN_CONFIG_PREFIX = "admin_config:"


class AdminConfigService:
    def __init__(
        self,
        redis_client,
        rds_store,
        validators: Optional[dict[str, Callable[[Any], Any]]] = None,
    ) -> None:
        # raw redis client (supports get/set/delete/scan_iter)
        self.redis = redis_client
        self.rds_store = rds_store
        # key -> validator(value) -> value_to_persist; raises ValueError/TypeError
        # on invalid input. The persisted value is what the validator returns, so
        # it stays byte-compatible with what the runtime reader parses.
        self.validators = validators or {}

    def _redis_key(self, key: str) -> str:
        return f"{ADMIN_CONFIG_PREFIX}{key}"

    def set(self, key: str, value: Any, updated_by: Optional[str] = None) -> Any:
        """Validate, write RDS (truth), then mirror Redis. Returns the stored value.

        Validation runs BEFORE any write (a malformed value never reaches RDS or
        Redis). If the Redis mirror fails after the RDS commit, the exception
        propagates so the caller returns a non-success and retries (the RDS upsert
        is idempotent, so a retry converges and restores the mirror).
        """
        validator = self.validators.get(key)
        to_store = validator(value) if validator is not None else value
        self.rds_store.upsert_admin_config(key, to_store, updated_by)  # truth first
        self.redis.set(self._redis_key(key), json.dumps(to_store))  # mirror
        return to_store

    def get(self, key: str) -> Any:
        """Return the live value from the Redis mirror (kept in sync with RDS).
        Falls back to the durable RDS value if the mirror is absent (e.g. if it
        was evicted)."""
        raw = self.redis.get(self._redis_key(key))
        if raw is not None:
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return raw
        row = self.rds_store.get_admin_config(key)
        if row is not None:
            return row["value"]
        return None

    def delete(self, key: str) -> None:
        """Hard-delete the config: remove the RDS row and the Redis mirror.
        Readers fall back to their built-in defaults on a missing key."""
        self.rds_store.delete_admin_config(key)
        self.redis.delete(self._redis_key(key))

    def list_keys(self) -> list[str]:
        return [row["key"] for row in self.rds_store.list_admin_config()]

