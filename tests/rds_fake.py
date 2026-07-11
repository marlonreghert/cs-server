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

# venues.venue address columns dropped by the batched contract — address lives
# only in venues.address (self.addresses). Kept out of the stored venue row so the
# fake mirrors the contracted real store.
_ADDRESS_COLUMNS = ("venue_address", "venue_lat", "venue_lng")


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
        # Dedup set mirroring the real store's unique index on
        # (user_pseudo, venue_id, business_period) + ON CONFLICT DO NOTHING.
        self._hot_like_keys: set[tuple] = set()
        # engagement.app_session_day: one row per (user_pseudo, activity_date).
        # Mirrors the real PK + ON CONFLICT DO NOTHING via a de-duplicating set.
        self.app_sessions: set[tuple[str, object]] = set()
        self.history: list[dict] = []
        self.admin_config: dict[str, dict] = {}
        # Ex2: admin.eligibility_rule — (rule_type, value) -> metadata.
        self.eligibility_rules: dict[tuple[str, str], dict] = {}
        # admin.geo_fence enabled flag + admin.geo_fence_city circles, held as
        # one {"enabled", "cities": [...]} dict. Seeded with the default fence
        # (recife @ 40 km) so the fake mirrors the migration-seeded real tables.
        from app.services.venue_eligibility import default_geo_fence
        self.geo_fence: dict = default_geo_fence()
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
        # Parity with RdsVenueStore: a geo-link undo is reversible — an active
        # re-add of an undo-deprecated venue reactivates it; any other source
        # keeps the resurrect-block.
        reactivating_undo = (
            row.get("lifecycle_status") == "deprecated"
            and venue.is_active()
            and row.get("deprecated_source") == "admin_geo_link_undo"
        )
        if (
            row.get("lifecycle_status") == "deprecated"
            and venue.is_active()
            and not reactivating_undo
        ):
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
        # Contracted shape: scalars in columns (source of truth), nested fields in
        # `extra`. No `payload` baseline and no venues.venue address columns —
        # address lives only in self.addresses. Mirrors RdsVenueStore.upsert_venue.
        columns, residual = split_venue_for_storage(venue)
        row = {k: v for k, v in columns.items() if k not in _ADDRESS_COLUMNS}
        row["extra"] = residual
        row["created_at"] = existing.get("created_at", _now())
        row["updated_at"] = _now()
        self.venues[venue.venue_id] = row
        # venues.address is the sole address source; structured components stay
        # null until Google Places enrichment fills them.
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
        """Source venue_address/lat/lng solely from venues.address — the
        venues.venue address columns were dropped by the contract. Every venue has
        a 1:1 address row written in the same upsert, so the lookup always
        matches; a missing one yields a row without address (reconstruction
        fails), mirroring the real LEFT JOIN."""
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
        read from the stored columns (priority defaults to 5; NULL reviews/rating
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

    def list_servable_venue_ids(self) -> list[str]:
        """The eligibility serving view: venue ids that are active AND eligible
        under the live block-list rules (from admin.eligibility_rule). Mirrors the
        real serving.eligible_venue SQL view. Reuses evaluate() — the single
        eligibility source of truth — so the fake is a faithful behaviour contract;
        the real SQL view's equivalence to evaluate() is pinned by the parity test
        (post-provisioning). A venue is servable iff its verdict is not
        soft_deletable (high-confidence ineligible); unlabeled/ambiguous venues
        stay in the view, matching the block-list policy."""
        from app.services.venue_eligibility import (
            evaluate as _evaluate,
            eligibility_config_from_rules as _config_from_rules,
            geo_excluded as _geo_excluded,
        )

        self._guard()
        config = _config_from_rules(self.list_eligibility_rules())
        fence = self.geo_fence
        out = []
        for vid, row in self.venues.items():
            if row.get("lifecycle_status", "active") != "active":
                continue
            gtype = None
            va = self.enrichment.get("google_places.vibe_attributes", {}).get(vid)
            if va is not None and va.get("deleted_at") is None:
                gtype = va.get("google_primary_type") or (
                    va.get("payload") or {}
                ).get("google_primary_type")
            if _evaluate(
                row.get("venue_name"), row.get("venue_type"), gtype, config
            ).soft_deletable:
                continue
            # Geo-fence is a SEPARATE, reversible predicate (third state): drop an
            # out-of-fence venue from serving without soft-deleting it. Coords come
            # from venues.address (mirrors the SQL view's LEFT JOIN). Mirrors the
            # real serving.eligible_venue geo predicate; parity is pinned by
            # tests/test_eligibility_serving_view_parity.py.
            addr = self.addresses.get(vid) or {}
            if _geo_excluded(addr.get("lat"), addr.get("lng"), fence):
                continue
            out.append(vid)
        return out

    def list_servable_venue_ids_by_priority(self, limit: int) -> list[str]:
        """Mirror RdsVenueStore: the top-`limit` servable (active AND eligible)
        venue ids ordered by priority asc, reviews desc, rating desc, venue_id asc.
        Reuses list_servable_venue_ids() (the eligibility serving view, the single
        source of truth) and applies the same ordering keys as
        list_active_venue_ids_by_priority. A non-positive limit selects nothing."""
        if limit <= 0:
            return []
        servable = set(self.list_servable_venue_ids())

        def _key(item):
            vid, row = item
            priority = row.get("priority", 5)
            reviews = row.get("reviews")
            rating = row.get("rating")
            reviews_key = -(reviews if reviews is not None else float("-inf"))
            rating_key = -(rating if rating is not None else float("-inf"))
            return (priority, reviews_key, rating_key, vid)

        rows = [(vid, row) for vid, row in self.venues.items() if vid in servable]
        rows.sort(key=_key)
        return [vid for vid, _ in rows[:limit]]

    def list_all_venue_rows(self) -> list[dict]:
        return [self._row_with_address(row) for row in self.venues.values()]

    # ── bulk per-table readers (projector rebuild, P1) ─────────────────────────
    # Mirrors RdsVenueStore's bulk readers so the fake stays the behaviour
    # contract for the projector (pinned by test_rds_store_contract.py).
    def get_venues_by_ids(self, venue_ids: list[str]) -> dict[str, dict]:
        wanted = set(venue_ids)
        return {
            vid: self._row_with_address(row)
            for vid, row in self.venues.items()
            if vid in wanted
        }

    def get_enrichment_bulk(self, table_key: str, venue_ids: list[str]) -> dict[str, dict]:
        wanted = set(venue_ids)
        return {
            vid: copy.deepcopy(row)
            for vid, row in self.enrichment.get(table_key, {}).items()
            if vid in wanted and row.get("deleted_at") is None
        }

    def get_weekly_bulk(self, venue_ids: list[str]) -> dict[str, dict[int, dict]]:
        wanted = set(venue_ids)
        out: dict[str, dict[int, dict]] = {}
        for composite_id, row in self.enrichment.get("besttime.weekly_forecast", {}).items():
            if row.get("deleted_at") is not None:
                continue
            vid, _, day = composite_id.partition("#")
            if vid not in wanted:
                continue
            out.setdefault(vid, {})[int(day)] = copy.deepcopy(row)
        return out

    def get_live_bulk(self, venue_ids: list[str]) -> dict[str, dict]:
        wanted = set(venue_ids)
        return {
            vid: copy.deepcopy(row)
            for vid, row in self.live_forecast.items()
            if vid in wanted
        }

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
    def upsert_live_forecast(self, venue_id, payload) -> bool:
        """Mirrors the real store's FK guard: no-op (return False) when
        venue_id has no row in self.venues, instead of writing an orphaned
        entry the real store's live_forecast_venue_id_fkey would reject."""
        self._guard()
        if venue_id not in self.venues:
            return False
        self.live_forecast[venue_id] = {"payload": copy.deepcopy(payload), "updated_at": _now()}
        return True

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

    def add_hot_like_event(self, user_pseudo, venue_id, business_period) -> bool:
        """Mirrors the real store's unique index + ON CONFLICT DO NOTHING:
        returns True when this (user, venue, day) tuple is new, False when
        it's a dedup-suppressed retry."""
        self._guard()
        key = (user_pseudo, venue_id, business_period)
        if key in self._hot_like_keys:
            return False
        self._hot_like_keys.add(key)
        self.hot_like_events.append({
            "user_pseudo": user_pseudo, "venue_id": venue_id,
            "business_period": business_period, "created_at": _now(),
        })
        return True

    # ── app activity (one row per user per day; total + active-window counts) ──
    def record_app_session(self, user_pseudo, activity_date) -> None:
        self._guard()
        self.app_sessions.add((user_pseudo, activity_date))  # PK dedup == ON CONFLICT DO NOTHING

    def count_users(self, since_date=None) -> int:
        self._guard()
        if since_date is None:
            return len({up for up, _ in self.app_sessions})
        return len({up for up, d in self.app_sessions if d >= since_date})

    def app_session_rows_for(self, activity_date) -> list[str]:
        return [up for up, d in self.app_sessions if d == activity_date]

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
        self._guard()  # a real SELECT hits Postgres and fails on an RDS outage
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

    # ── geo-fence (enabled flag + capital circles; read by the serving view) ───
    def get_geo_fence(self) -> dict:
        self._guard()
        return copy.deepcopy(self.geo_fence)

    def set_geo_fence(self, fence: dict, updated_by=None) -> None:
        """Persist the validated fence ({"enabled", "cities": [...]}) whole.
        Mirrors the real store's transactional replace of admin.geo_fence_city
        plus the admin.geo_fence enabled upsert."""
        self._guard()
        self.geo_fence = copy.deepcopy(fence)

    def count_geo_excluded_active_venues(self) -> int:
        """Active venues with coordinates outside every enabled fence circle (the
        reversible serve-time exclusion). Missing coords / a disabled fence / an
        empty circle list count as zero (fail-open). Observability only —
        mirrors the real store's COUNT."""
        self._guard()
        from app.services.venue_eligibility import geo_excluded as _geo_excluded

        fence = self.geo_fence
        count = 0
        for vid, row in self.venues.items():
            if row.get("lifecycle_status", "active") != "active":
                continue
            addr = self.addresses.get(vid) or {}
            if _geo_excluded(addr.get("lat"), addr.get("lng"), fence):
                count += 1
        return count

    def count_active_venues_outside_circles(self) -> int:
        """Active venues outside every configured circle regardless of the
        enabled flag — the admin panel's warning number. Mirrors the real
        store's COUNT: an empty circle list counts zero."""
        self._guard()
        from app.services.venue_eligibility import geo_excluded as _geo_excluded

        fence = {**self.geo_fence, "enabled": True}
        count = 0
        for vid, row in self.venues.items():
            if row.get("lifecycle_status", "active") != "active":
                continue
            addr = self.addresses.get(vid) or {}
            if _geo_excluded(addr.get("lat"), addr.get("lng"), fence):
                count += 1
        return count

    def hot_like_event_count(self, venue_id) -> int:
        return sum(1 for e in self.hot_like_events if e["venue_id"] == venue_id)

    # raw user id must never appear anywhere in the store
    def contains_raw_value(self, raw: str) -> bool:
        import json
        blob = json.dumps({
            "fav": list(self.favorites.keys()),
            "hot": self.hot_like_events,
            "act": [[up, str(d)] for up, d in self.app_sessions],
        }, default=str)  # business_period / dates aren't natively JSON-serializable
        return raw in blob
