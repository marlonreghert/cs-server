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

from app.dao.venue_row import split_venue_for_storage


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
        # Ex3: venues.address (1:1) — address raw text + structured components +
        # lat/lng. Dual-written on every venue upsert; the source for reconstruction.
        self.addresses: dict[str, dict] = {}
        # table_key ("schema.table") -> venue_id -> row dict
        self.enrichment: dict[str, dict[str, dict]] = {}
        self.live_forecast: dict[str, dict] = {}
        self.favorites: dict[tuple[str, str], dict] = {}
        self.hot_like_events: list[dict] = []
        self.history: list[dict] = []
        self.admin_config: dict[str, dict] = {}
        # Ex2: admin.eligibility_rule — (rule_type, value) -> metadata.
        self.eligibility_rules: dict[tuple[str, str], dict] = {}
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
        gbs = row.get("google_business_status")
        if row.get("lifecycle_status") == "deprecated" and venue.is_active():
            venue.lifecycle_status = "deprecated"
            venue.deprecated_reason = row.get("deprecated_reason")
            venue.deprecated_source = row.get("deprecated_source")
            venue.deprecated_at = _coerce_dt(row.get("deprecated_at"))
            venue.google_business_status = gbs
        elif gbs and not venue.google_business_status:
            venue.google_business_status = gbs
        # Refresh priority is managed only by direct SQL (one-time tiering +
        # manual edits); a default-constructed re-upsert must never reset it.
        if row.get("priority") is not None:
            venue.priority = row["priority"]

    def upsert_venue(self, venue) -> None:
        self._guard()
        self._preserve_deprecation(venue)
        existing = self.venues.get(venue.venue_id, {})
        # Ex1: scalars in columns (source of truth), nested fields in `extra`. The
        # full payload stays as the retained v1 golden baseline (dual-write during
        # expand). Mirrors RdsVenueStore.upsert_venue so the offline contract is
        # column-based reconstruction, not a payload round-trip.
        columns, residual = split_venue_for_storage(venue)
        row = dict(columns)
        row["extra"] = residual
        row["payload"] = venue.model_dump(by_alias=True, mode="json")
        row["created_at"] = existing.get("created_at", _now())
        row["updated_at"] = _now()
        self.venues[venue.venue_id] = row
        # Ex3 dual-write: the address table is the read source; structured
        # components stay null until Google Places enrichment fills them.
        existing_addr = self.addresses.get(venue.venue_id, {})
        self.addresses[venue.venue_id] = {
            "venue_id": venue.venue_id,
            "raw_text": venue.venue_address,
            "street": existing_addr.get("street"),
            "neighborhood": existing_addr.get("neighborhood"),
            "city": existing_addr.get("city"),
            "postal_code": existing_addr.get("postal_code"),
            "lat": venue.venue_lat,
            "lng": venue.venue_lng,
            "updated_at": _now(),
        }

    def _row_with_address(self, row: dict) -> dict:
        """Source venue_address/lat/lng from venues.address (Ex3 read cutover),
        falling back to the retained venue columns if no address row exists."""
        out = copy.deepcopy(row)
        addr = self.addresses.get(row["venue_id"])
        if addr is not None:
            out["venue_address"] = addr["raw_text"]
            out["venue_lat"] = addr["lat"]
            out["venue_lng"] = addr["lng"]
        return out

    def get_address(self, venue_id) -> Optional[dict]:
        return self.addresses.get(venue_id)

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
        row = self.venues.get(venue_id)
        return self._row_with_address(row) if row is not None else None

    def list_active_venue_ids(self) -> list[str]:
        return [
            vid for vid, row in self.venues.items()
            if row.get("lifecycle_status", "active") == "active"
        ]

    def list_active_venue_ids_by_priority(self, limit: int) -> list[str]:
        """Mirror RdsVenueStore: top-`limit` active venues ordered by priority
        asc, reviews desc, rating desc, venue_id asc. priority/reviews/rating are
        read from the stored payload (priority defaults to 5; NULL reviews/rating
        sort last). A non-positive limit selects nothing."""
        if limit <= 0:
            return []

        def _key(item):
            vid, row = item
            priority = row.get("priority", 5)
            reviews = row.get("reviews")
            rating = row.get("rating")
            reviews_key = -(reviews if reviews is not None else float("-inf"))
            rating_key = -(rating if rating is not None else float("-inf"))
            return (priority, reviews_key, rating_key, vid)

        active = [
            (vid, row) for vid, row in self.venues.items()
            if row.get("lifecycle_status", "active") == "active"
        ]
        active.sort(key=_key)
        return [vid for vid, _ in active[:limit]]

    def list_deprecated_venue_ids(self) -> list[str]:
        return [
            vid for vid, row in self.venues.items()
            if row.get("lifecycle_status", "active") == "deprecated"
        ]

    def list_all_venue_payloads(self) -> list[dict]:
        return [row["payload"] for row in self.venues.values()]

    def list_all_venue_rows(self) -> list[dict]:
        return [self._row_with_address(row) for row in self.venues.values()]

    # ── pipeline cache-freshness gating from RDS (Pass 2b) ─────────────────────
    def _age_seconds(self, row) -> float:
        ts = _coerce_dt(row.get("updated_at"))
        if ts is None:
            return 0.0
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def list_fresh_enrichment_venue_ids(self, table_key, max_age_seconds=None) -> list[str]:
        out = []
        for vid, row in self.enrichment.get(table_key, {}).items():
            if row.get("deleted_at") is not None:
                continue
            if max_age_seconds is not None and self._age_seconds(row) > max_age_seconds:
                continue
            out.append(vid)
        return out

    def list_fresh_instagram_venue_ids(
        self, found_max_age_seconds, not_found_max_age_seconds
    ) -> list[str]:
        out = []
        for vid, row in self.enrichment.get("instagram.handle", {}).items():
            if row.get("deleted_at") is not None:
                continue
            status = (row.get("payload") or {}).get("status")
            limit = not_found_max_age_seconds if status == "not_found" else found_max_age_seconds
            if self._age_seconds(row) <= limit:
                out.append(vid)
        return out

    def delete_live_forecast(self, venue_id) -> None:
        self._guard()
        self.live_forecast.pop(venue_id, None)

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

    # ── eligibility rules (Ex2: normalized admin.eligibility_rule) ─────────────
    def list_eligibility_rules(self) -> list[tuple[str, str]]:
        return sorted(self.eligibility_rules.keys())

    def add_eligibility_rule(self, rule_type, value, updated_by=None) -> None:
        self._guard()
        self.eligibility_rules[(rule_type, value)] = {
            "updated_by": updated_by, "updated_at": _now(),
        }

    def remove_eligibility_rule(self, rule_type, value) -> None:
        self._guard()
        self.eligibility_rules.pop((rule_type, value), None)

    def replace_eligibility_rules(self, rules, updated_by=None) -> None:
        """Replace the whole rule set (full-blob set decomposed into rows)."""
        self._guard()
        self.eligibility_rules = {
            (rt, v): {"updated_by": updated_by, "updated_at": _now()} for rt, v in rules
        }

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
