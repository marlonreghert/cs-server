"""In-memory fake of RdsVenueStore for BDD + unit tests (no Postgres needed).

Mirrors the RdsVenueStore contract used by VenueRepository: dict-backed tables,
append-only history for expensive labels, soft-delete via deleted_at, an outage
toggle, and engagement (favorites current-state / hot_like events). AGENTS.md
forbids live external calls in BDD, so this is the deterministic stand-in;
real-Postgres fidelity is covered by post-provisioning DB tests.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Optional


class RdsUnavailable(RuntimeError):
    """Raised by the fake when the outage toggle is on (RDS-outage scenario)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_dt(value):
    """Coerce an ISO string / datetime to a datetime (matches the real store's
    _coerce_dt so the un-deprecate guard behaves identically)."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


class InMemoryRdsVenueStore:
    def __init__(self) -> None:
        self.venues: dict[str, dict] = {}
        # table_key ("schema.table") -> venue_id -> row dict
        self.enrichment: dict[str, dict[str, dict]] = {}
        self.live_forecast: dict[str, dict] = {}
        self.favorites: dict[tuple[str, str], dict] = {}
        self.hot_like_events: list[dict] = []
        self.history: list[dict] = []
        self.admin_config: dict[str, dict] = {}
        self._down = False

    # ── test controls ────────────────────────────────────────────────────────
    def set_unavailable(self, on: bool) -> None:
        self._down = on

    def _guard(self) -> None:
        if self._down:
            raise RdsUnavailable("RDS is unavailable (fake outage)")

    # ── venue (system of record) ──────────────────────────────────────────────
    def _preserve_deprecation(self, venue) -> None:
        """Mirror RdsVenueStore._preserve_deprecation / RedisVenueDAO.upsert_venue:
        an active re-add must NOT resurrect a venue deprecated in RDS. Behaviour
        parity is asserted by tests/test_rds_store_contract.py."""
        if not venue.venue_id:
            return
        row = self.venues.get(venue.venue_id)
        if row is None:
            return
        gbs = (row.get("payload") or {}).get("google_business_status")
        if row.get("lifecycle_status") == "deprecated" and venue.is_active():
            venue.lifecycle_status = "deprecated"
            venue.deprecated_reason = row.get("deprecated_reason")
            venue.deprecated_source = row.get("deprecated_source")
            venue.deprecated_at = _coerce_dt(row.get("deprecated_at"))
            venue.google_business_status = gbs
        elif gbs and not venue.google_business_status:
            venue.google_business_status = gbs

    def upsert_venue(self, venue) -> None:
        self._guard()
        self._preserve_deprecation(venue)
        existing = self.venues.get(venue.venue_id, {})
        self.venues[venue.venue_id] = {
            "venue_id": venue.venue_id,
            "venue_name": venue.venue_name,
            "venue_lat": venue.venue_lat,
            "venue_lng": venue.venue_lng,
            "venue_type": venue.venue_type,
            "lifecycle_status": venue.lifecycle_status,
            "deprecated_reason": venue.deprecated_reason,
            "deprecated_source": venue.deprecated_source,
            "deprecated_at": venue.deprecated_at.isoformat() if venue.deprecated_at else None,
            "payload": venue.model_dump(by_alias=True, mode="json"),
            "created_at": existing.get("created_at", _now()),
            "updated_at": _now(),
        }

    def soft_delete_venue(self, venue_id, reason, source, google_business_status=None) -> None:
        self._guard()
        row = self.venues.get(venue_id)
        if row is None:
            return
        row.update({
            "lifecycle_status": "deprecated",
            "deprecated_reason": reason,
            "deprecated_source": source,
            "deprecated_at": _now(),
            "updated_at": _now(),
        })

    def get_venue(self, venue_id) -> Optional[dict]:
        return self.venues.get(venue_id)

    def list_active_venue_ids(self) -> list[str]:
        return [
            vid for vid, row in self.venues.items()
            if row.get("lifecycle_status", "active") == "active"
        ]

    def list_deprecated_venue_ids(self) -> list[str]:
        return [
            vid for vid, row in self.venues.items()
            if row.get("lifecycle_status", "active") == "deprecated"
        ]

    # ── generic enrichment (JSONB payload + optional append-only history) ─────
    def upsert_enrichment(self, table_key, venue_id, payload, *, history, promoted=None) -> None:
        self._guard()
        self.enrichment.setdefault(table_key, {})[venue_id] = {
            "payload": copy.deepcopy(payload),
            "deleted_at": None,
            "updated_at": _now(),
            **(promoted or {}),
        }
        if history:
            self.history.append({
                "table_key": table_key, "venue_id": venue_id,
                "payload": copy.deepcopy(payload), "operation": "upsert",
                "written_at": _now(),
            })

    def soft_delete_enrichment(self, table_key, venue_id, *, history) -> None:
        self._guard()
        row = self.enrichment.get(table_key, {}).get(venue_id)
        if row is None:
            return
        row["deleted_at"] = _now()
        if history:
            self.history.append({
                "table_key": table_key, "venue_id": venue_id,
                "payload": row["payload"], "operation": "soft_delete",
                "written_at": _now(),
            })

    def get_enrichment(self, table_key, venue_id) -> Optional[dict]:
        return self.enrichment.get(table_key, {}).get(venue_id)

    def history_count(self, table_key, venue_id) -> int:
        return sum(
            1 for h in self.history
            if h["table_key"] == table_key and h["venue_id"] == venue_id
        )

    # ── live busyness (current-state, no history) ─────────────────────────────
    def upsert_live_forecast(self, venue_id, payload) -> None:
        self._guard()
        self.live_forecast[venue_id] = {"payload": copy.deepcopy(payload), "updated_at": _now()}

    def get_live_forecast(self, venue_id) -> Optional[dict]:
        return self.live_forecast.get(venue_id)

    # ── engagement ────────────────────────────────────────────────────────────
    def upsert_favorite(self, user_pseudo, venue_id) -> None:
        self._guard()
        self.favorites[(user_pseudo, venue_id)] = {"deleted_at": None, "updated_at": _now()}

    def soft_delete_favorite(self, user_pseudo, venue_id) -> None:
        self._guard()
        row = self.favorites.get((user_pseudo, venue_id))
        if row is not None:
            row["deleted_at"] = _now()

    def get_favorite(self, user_pseudo, venue_id) -> Optional[dict]:
        return self.favorites.get((user_pseudo, venue_id))

    def active_favorite_venue_ids(self, user_pseudo) -> list[str]:
        return [
            vid for (up, vid), row in self.favorites.items()
            if up == user_pseudo and row.get("deleted_at") is None
        ]

    def add_hot_like_event(self, user_pseudo, venue_id) -> None:
        self._guard()
        self.hot_like_events.append({
            "user_pseudo": user_pseudo, "venue_id": venue_id, "created_at": _now(),
        })

    # ── admin config (system of record; mirrored to Redis by AdminConfigService) ─
    def upsert_admin_config(self, key, value, updated_by=None) -> None:
        self._guard()
        self.admin_config[key] = {
            "key": key, "value": copy.deepcopy(value),
            "updated_by": updated_by, "updated_at": _now(),
        }

    def get_admin_config(self, key) -> Optional[dict]:
        return self.admin_config.get(key)

    def delete_admin_config(self, key) -> None:
        self._guard()
        self.admin_config.pop(key, None)

    def list_admin_config(self) -> list[dict]:
        return list(self.admin_config.values())

    def hot_like_event_count(self, venue_id) -> int:
        return sum(1 for e in self.hot_like_events if e["venue_id"] == venue_id)

    # raw user id must never appear anywhere in the store
    def contains_raw_value(self, raw: str) -> bool:
        import json
        blob = json.dumps({
            "fav": list(self.favorites.keys()),
            "hot": self.hot_like_events,
        })
        return raw in blob
