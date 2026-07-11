"""RdsVenueStore — the Postgres system-of-record writer/reader.

Interface-matched to tests.rds_fake.InMemoryRdsVenueStore (which is the
behaviour contract proven by BDD/unit tests). This SQLAlchemy implementation is
exercised against real Postgres only after the RDS is provisioned (see
infra/rds/README.md) — there is no local Postgres in CI/dev, so its SQL is
validated by the post-provisioning smoke test, not by the offline suite.

Design: generic JSONB upsert per table + promoted columns + append-only
audit.enrichment_history; never hard-deletes (soft-delete via deleted_at).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import bindparam, create_engine, text

from app.dao.venue_row import split_venue_for_storage

logger = logging.getLogger(__name__)


def _coerce_dt(value):
    """Coerce a timestamp to a datetime (Postgres yields datetime; the fake/JSON
    yields an ISO string). Returns None on a missing/unparseable value."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value

# table_key -> (schema, table, [promoted column names])
_ENRICHMENT = {
    "google_places.vibe_attributes": ("google_places", "vibe_attributes",
                                       ["google_primary_type", "google_place_id"]),
    "google_places.opening_hours": ("google_places", "opening_hours", []),
    "google_places.photos": ("google_places", "photos", []),
    "google_places.reviews": ("google_places", "reviews", []),
    "instagram.handle": ("instagram", "handle", ["instagram_handle"]),
    "instagram.posts": ("instagram", "posts", []),
    "venues.menu_photos": ("venues", "menu_photos", []),
    "venues.menu_data": ("venues", "menu_data", []),
    "venues.vibe_profile": ("venues", "vibe_profile", []),
}
_WEEKLY = "besttime.weekly_forecast"

# The venue row exposed for reconstruction (venue_row.venue_from_row reads the
# scalar columns + `extra`). Address is sourced solely from venues.address — the
# Ex3 contract dropped the venues.venue address columns and the Ex1 contract
# dropped `payload`. lat/lng feed the geo rebuild. Every venue has a 1:1 address
# row (0004 backfill + same-transaction dual-write on upsert), so the LEFT JOIN
# always matches.
_VENUE_SELECT = (
    "SELECT v.venue_id, v.venue_name, "
    "a.raw_text AS venue_address, a.lat AS venue_lat, a.lng AS venue_lng, "
    "v.venue_type, v.price_level, v.price_range, v.google_price_level, "
    "v.besttime_price_level, v.price_level_source, "
    "v.rating, v.reviews, v.forecast, v.processed, "
    "v.priority, v.lifecycle_status, v.deprecated_at, v.deprecated_reason, "
    "v.deprecated_source, v.google_business_status, v.created_at, v.extra "
    "FROM venues.venue v LEFT JOIN venues.address a ON a.venue_id = v.venue_id"
)


class RdsVenueStore:
    def __init__(self, sqlalchemy_url: str):
        self.engine = create_engine(sqlalchemy_url, pool_pre_ping=True, future=True)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _history(self, conn, schema, table, venue_id, payload, op):
        conn.execute(text(
            "INSERT INTO audit.enrichment_history "
            "(schema_name, table_name, venue_id, payload, operation) "
            "VALUES (:s, :t, :v, CAST(:p AS jsonb), :op)"
        ), {"s": schema, "t": table, "v": venue_id, "p": json.dumps(payload), "op": op})

    # ── venue (system of record) ──────────────────────────────────────────────
    def _preserve_deprecation(self, venue) -> None:
        """Mirror RedisVenueDAO.upsert_venue: an active re-add of a venue that is
        deprecated in RDS must NOT resurrect it (a catalog refresh re-finding a
        deprecated drugstore would otherwise flip it active). Protects PR #21
        eligibility once serving reads RDS via the projector. Also preserves
        google_business_status. Reads the lifecycle from the COLUMN (soft_delete
        updates columns)."""
        if not venue.venue_id:
            return
        row = self.get_venue(venue.venue_id)
        if row is None:
            return
        # A geo-link undo is reversible by design: an active re-add of a venue
        # deprecated by an undo IS allowed to reactivate (clearing the deprecation
        # fields). Every other deprecation source keeps the resurrect-block.
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
            venue.google_business_status = row.get("google_business_status")
        elif row.get("google_business_status") and not venue.google_business_status:
            venue.google_business_status = row.get("google_business_status")
        # Refresh priority is managed only by direct SQL (one-time tiering +
        # manual edits); a default-constructed re-upsert (e.g. discovery
        # re-finding a venue) must never reset it.
        if row.get("priority") is not None:
            venue.priority = row["priority"]

    def upsert_venue(self, venue) -> None:
        self._preserve_deprecation(venue)
        # Ex1/Ex3 contracted: scalars are the source of truth in columns, nested
        # fields in the residual `extra`, and address lives only in venues.address.
        # `payload` and the venues.venue address columns were dropped (0007), so
        # they are no longer written — venue_address (NOT NULL DEFAULT '' during the
        # pre-drop window) is omitted from the column list, never passed as NULL,
        # so the relax→drop sequence stays clean.
        _, residual = split_venue_for_storage(venue)
        price_range = (
            json.dumps(venue.price_range.model_dump(by_alias=True))
            if venue.price_range is not None
            else None
        )
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO venues.venue (venue_id, venue_name, "
                "venue_type, price_level, price_range, google_price_level, "
                "besttime_price_level, price_level_source, rating, reviews, priority, "
                "forecast, processed, lifecycle_status, deprecated_reason, "
                "deprecated_source, deprecated_at, google_business_status, extra, updated_at) "
                "VALUES (:venue_id, :venue_name, "
                ":venue_type, :price_level, CAST(:price_range AS jsonb), :google_price_level, "
                ":besttime_price_level, :price_level_source, :rating, :reviews, :priority, "
                ":forecast, :processed, "
                ":lifecycle_status, :deprecated_reason, :deprecated_source, :deprecated_at, "
                ":google_business_status, CAST(:extra AS jsonb), now()) "
                "ON CONFLICT (venue_id) DO UPDATE SET "
                "venue_name=excluded.venue_name, "
                "venue_type=excluded.venue_type, price_level=excluded.price_level, "
                "price_range=excluded.price_range, google_price_level=excluded.google_price_level, "
                "besttime_price_level=excluded.besttime_price_level, "
                "price_level_source=excluded.price_level_source, "
                "rating=excluded.rating, reviews=excluded.reviews, priority=excluded.priority, "
                "forecast=excluded.forecast, "
                "processed=excluded.processed, lifecycle_status=excluded.lifecycle_status, "
                "deprecated_reason=excluded.deprecated_reason, deprecated_source=excluded.deprecated_source, "
                "deprecated_at=excluded.deprecated_at, google_business_status=excluded.google_business_status, "
                "extra=excluded.extra, updated_at=now()"
            ), {
                "venue_id": venue.venue_id, "venue_name": venue.venue_name,
                "venue_type": venue.venue_type,
                "price_level": venue.price_level,
                "price_range": price_range,
                "google_price_level": venue.google_price_level,
                "besttime_price_level": venue.besttime_price_level,
                "price_level_source": venue.price_level_source,
                "rating": venue.rating,
                "reviews": venue.reviews, "priority": venue.priority,
                "forecast": venue.forecast,
                "processed": venue.processed, "lifecycle_status": venue.lifecycle_status,
                "deprecated_reason": venue.deprecated_reason,
                "deprecated_source": venue.deprecated_source,
                "deprecated_at": venue.deprecated_at,
                "google_business_status": venue.google_business_status,
                "extra": json.dumps(residual),
            })
            # venues.address is the sole address source of truth. Structured
            # components (street/neighborhood/city/postal_code) are left as-is —
            # null until Google Places enrichment fills them, never clobbered here.
            conn.execute(text(
                "INSERT INTO venues.address (venue_id, raw_text, lat, lng, updated_at) "
                "VALUES (:venue_id, :raw_text, :lat, :lng, now()) "
                "ON CONFLICT (venue_id) DO UPDATE SET "
                "raw_text=excluded.raw_text, lat=excluded.lat, lng=excluded.lng, updated_at=now()"
            ), {"venue_id": venue.venue_id, "raw_text": venue.venue_address,
                "lat": venue.venue_lat, "lng": venue.venue_lng})

    def soft_delete_venue(self, venue_id, reason, source, google_business_status=None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE venues.venue SET lifecycle_status='deprecated', "
                "deprecated_reason=:r, deprecated_source=:s, deprecated_at=now(), "
                "google_business_status=COALESCE(:g, google_business_status), updated_at=now() "
                "WHERE venue_id=:v"
            ), {"r": reason, "s": source, "g": google_business_status, "v": venue_id})

    def get_venue(self, venue_id) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(text(
                _VENUE_SELECT + " WHERE v.venue_id=:v"
            ), {"v": venue_id}).mappings().first()
            return dict(row) if row else None

    def list_active_venue_ids(self) -> list[str]:
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT venue_id FROM venues.venue WHERE lifecycle_status='active'"
            ))]

    def list_active_venue_ids_by_priority(self, limit: int) -> list[str]:
        """The top-`limit` active venues ordered by refresh priority ascending
        (0 first), tie-broken by reviews desc, rating desc, then venue_id for a
        stable, deterministic selection. Backs the bounded live/weekly refresh.
        A non-positive limit selects nothing (strictly honours a zero budget)."""
        if limit <= 0:
            return []
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT venue_id FROM venues.venue WHERE lifecycle_status='active' "
                "ORDER BY priority ASC, reviews DESC NULLS LAST, rating DESC NULLS LAST, "
                "venue_id ASC LIMIT :limit"
            ), {"limit": limit})]

    def list_deprecated_venue_ids(self) -> list[str]:
        """Venue ids deprecated in RDS — a positive removal signal for the
        projector (remove from the Redis serving set + geo index). Distinct from
        a venue having no RDS row at all, which is absence-of-signal (not pruned).
        """
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT venue_id FROM venues.venue WHERE lifecycle_status='deprecated'"
            ))]

    def list_servable_venue_ids(self) -> list[str]:
        """The eligibility serving view (`serving.eligible_venue`): venue ids that
        are active AND eligible under the live block-list rules, evaluated in SQL.
        This is the projector's serving source and the enrichment gate. The view's
        equivalence to evaluate() is pinned by the parity test against real Postgres
        (there is no local Postgres in CI, so the SQL is validated post-provisioning).
        """
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT venue_id FROM serving.eligible_venue"
            ))]

    def list_servable_venue_ids_by_priority(self, limit: int) -> list[str]:
        """The top-`limit` servable venues (the eligibility serving view) ordered
        by refresh priority ascending, tie-broken by reviews desc, rating desc,
        then venue_id — the served-scoped counterpart of
        list_active_venue_ids_by_priority that backs the bounded live/weekly
        refresh. serving.eligible_venue carries no priority column, so it is joined
        to venues.venue for the ordering keys. A non-positive limit selects
        nothing (mirrors the active variant)."""
        if limit <= 0:
            return []
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT ev.venue_id FROM serving.eligible_venue ev "
                "JOIN venues.venue v ON v.venue_id = ev.venue_id "
                "ORDER BY v.priority ASC, v.reviews DESC NULLS LAST, "
                "v.rating DESC NULLS LAST, v.venue_id ASC LIMIT :limit"
            ), {"limit": limit})]

    def list_all_venue_rows(self) -> list[dict]:
        """Every venue row (active + deprecated) with scalar columns + residual
        `extra` + address (from venues.address) — backs the pipeline
        list_all_venues RDS read (column-based reconstruction) and the
        redis↔rds serving diff."""
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(_VENUE_SELECT)).mappings()]

    # ── bulk per-table readers (projector rebuild, P1) ─────────────────────────
    # Replace the projector's former per-venue read loop (~18 SQL queries per
    # venue per cycle) with one query per table for the whole servable id set.
    # Same row shape as the single-row readers (get_venue / get_enrichment /
    # get_live_forecast) so the projector's per-venue projection logic is
    # unchanged — only the source of each `row`/`rec` moves from a per-call
    # SELECT to a dict lookup on a prefetched map.
    def get_venues_by_ids(self, venue_ids: list[str]) -> dict[str, dict]:
        """Venue rows (scalars + address + residual `extra`) for an id set, keyed
        by venue_id — the bulk counterpart of `get_venue`. Empty input short-
        circuits without a query."""
        if not venue_ids:
            return {}
        stmt = text(_VENUE_SELECT + " WHERE v.venue_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt, {"ids": list(venue_ids)}).mappings()
            return {row["venue_id"]: dict(row) for row in rows}

    def get_enrichment_bulk(self, table_key: str, venue_ids: list[str]) -> dict[str, dict]:
        """Non-deleted enrichment rows of `table_key` for an id set, keyed by
        venue_id — the bulk counterpart of `get_enrichment` for the (non-weekly)
        enrichment tables. Soft-deleted rows are excluded (a caller checking
        `bulk_map.get(venue_id)` sees the same "absent" result as the single-row
        reader's `rec.get("deleted_at") is None` gate). Empty input short-
        circuits without a query."""
        if not venue_ids:
            return {}
        schema, table, _ = _ENRICHMENT[table_key]
        stmt = text(
            f"SELECT venue_id, payload, deleted_at, updated_at FROM {schema}.{table} "
            "WHERE deleted_at IS NULL AND venue_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt, {"ids": list(venue_ids)}).mappings()
            return {row["venue_id"]: dict(row) for row in rows}

    def get_weekly_bulk(self, venue_ids: list[str]) -> dict[str, dict[int, dict]]:
        """All 7 non-deleted weekly-forecast rows for an id set, keyed by
        venue_id -> day_int -> {payload, deleted_at, updated_at} — the bulk
        counterpart of 7x `get_enrichment(besttime.weekly_forecast, "id#day")`.
        Empty input short-circuits without a query."""
        if not venue_ids:
            return {}
        stmt = text(
            "SELECT venue_id, day_int, payload, deleted_at, updated_at "
            "FROM besttime.weekly_forecast "
            "WHERE deleted_at IS NULL AND venue_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        out: dict[str, dict[int, dict]] = {}
        with self.engine.connect() as conn:
            for row in conn.execute(stmt, {"ids": list(venue_ids)}).mappings():
                out.setdefault(row["venue_id"], {})[row["day_int"]] = dict(row)
        return out

    def get_live_bulk(self, venue_ids: list[str]) -> dict[str, dict]:
        """Live-forecast rows for an id set, keyed by venue_id — the bulk
        counterpart of `get_live_forecast`. Empty input short-circuits without a
        query."""
        if not venue_ids:
            return {}
        stmt = text(
            "SELECT venue_id, payload FROM besttime.live_forecast WHERE venue_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt, {"ids": list(venue_ids)}).mappings()
            return {row["venue_id"]: dict(row) for row in rows}

    # ── pipeline cache-freshness gating from RDS ───────────────────────────────
    def list_fresh_enrichment_venue_ids(self, table_key, max_age_seconds=None) -> list[str]:
        """Venue ids whose enrichment of `table_key` is present (not soft-deleted)
        and, if max_age_seconds is given, newer than that age — the RDS equivalent
        of the Redis `list_cached_*` "done/fresh" gate. max_age_seconds=None =
        presence-only (no-TTL enrichments)."""
        schema, table, _ = _ENRICHMENT[table_key]
        sql = f"SELECT venue_id FROM {schema}.{table} WHERE deleted_at IS NULL"
        params = {}
        if max_age_seconds is not None:
            sql += " AND updated_at >= now() - make_interval(secs => :age)"
            params["age"] = float(max_age_seconds)
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(sql), params)]

    def list_fresh_instagram_venue_ids(
        self, found_max_age_seconds, not_found_max_age_seconds
    ) -> list[str]:
        """Status-aware instagram freshness gate: a `not_found` row is fresh only
        within not_found_max_age_seconds; any other status (found/low_confidence)
        within found_max_age_seconds. Mirrors the Redis status-dependent TTL."""
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT venue_id FROM instagram.handle WHERE deleted_at IS NULL AND ("
                "(payload->>'status' = 'not_found' "
                "  AND updated_at >= now() - make_interval(secs => :nf)) "
                "OR (COALESCE(payload->>'status', '') <> 'not_found' "
                "  AND updated_at >= now() - make_interval(secs => :f)))"
            ), {"nf": float(not_found_max_age_seconds), "f": float(found_max_age_seconds)})]

    def delete_live_forecast(self, venue_id) -> None:
        """Delete the current-state live busyness row (section E gap: keep live
        deletes in RDS so no write escapes to Redis-only under writes-only mode)."""
        with self.engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM besttime.live_forecast WHERE venue_id=:v"
            ), {"v": venue_id})

    # ── generic enrichment ────────────────────────────────────────────────────
    def upsert_enrichment(self, table_key, venue_id, payload, *, history, promoted=None) -> None:
        if table_key == _WEEKLY:
            return self._upsert_weekly(venue_id, payload, history)
        schema, table, promoted_cols = _ENRICHMENT[table_key]
        promoted = promoted or {}
        cols = ["venue_id", "payload", "deleted_at", "updated_at"] + promoted_cols
        vals = [":venue_id", "CAST(:payload AS jsonb)", "NULL", "now()"] + [f":{c}" for c in promoted_cols]
        sets = ["payload=excluded.payload", "deleted_at=NULL", "updated_at=now()"] + \
               [f"{c}=excluded.{c}" for c in promoted_cols]
        params = {"venue_id": venue_id, "payload": json.dumps(payload)}
        params.update({c: promoted.get(c) for c in promoted_cols})
        with self.engine.begin() as conn:
            conn.execute(text(
                f"INSERT INTO {schema}.{table} ({', '.join(cols)}) "
                f"VALUES ({', '.join(vals)}) "
                f"ON CONFLICT (venue_id) DO UPDATE SET {', '.join(sets)}"
            ), params)
            if history:
                self._history(conn, schema, table, venue_id, payload, "upsert")

    def _upsert_weekly(self, composite_id, payload, history) -> None:
        venue_id, _, day = composite_id.partition("#")
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO besttime.weekly_forecast (venue_id, day_int, payload, deleted_at, updated_at) "
                "VALUES (:v, :d, CAST(:p AS jsonb), NULL, now()) "
                "ON CONFLICT (venue_id, day_int) DO UPDATE SET "
                "payload=excluded.payload, deleted_at=NULL, updated_at=now()"
            ), {"v": venue_id, "d": int(day), "p": json.dumps(payload)})
            if history:
                self._history(conn, "besttime", "weekly_forecast", venue_id, payload, "upsert")

    def soft_delete_enrichment(self, table_key, venue_id, *, history) -> None:
        if table_key == _WEEKLY:
            return self._soft_delete_weekly(venue_id, history)
        schema, table, _ = _ENRICHMENT[table_key]
        with self.engine.begin() as conn:
            conn.execute(text(
                f"UPDATE {schema}.{table} SET deleted_at=now() WHERE venue_id=:v"
            ), {"v": venue_id})
            if history:
                self._history(conn, schema, table, venue_id, {}, "soft_delete")

    def _soft_delete_weekly(self, composite_id, history) -> None:
        # Weekly forecast is keyed by the composite (venue_id, day_int) and is
        # NOT in _ENRICHMENT, so it needs the same "<venue_id>#<day>" split that
        # _upsert_weekly / get_enrichment use — without this branch a weekly
        # table_key KeyErrors on _ENRICHMENT[table_key]. No production caller
        # currently soft-deletes a weekly row (VenueRepository._DELETE_TABLE has
        # no weekly entry), so this closes a latent gap rather than fixing a live
        # crash, and keeps the three weekly special-cases symmetric.
        venue_id, _, day = composite_id.partition("#")
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE besttime.weekly_forecast SET deleted_at=now() "
                "WHERE venue_id=:v AND day_int=:d"
            ), {"v": venue_id, "d": int(day)})
            if history:
                self._history(conn, "besttime", "weekly_forecast", venue_id, {}, "soft_delete")

    def get_enrichment(self, table_key, venue_id) -> Optional[dict]:
        if table_key == _WEEKLY:
            vid, _, day = venue_id.partition("#")
            with self.engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT payload, deleted_at, updated_at FROM besttime.weekly_forecast "
                    "WHERE venue_id=:v AND day_int=:d"
                ), {"v": vid, "d": int(day)}).mappings().first()
                return dict(row) if row else None
        schema, table, _ = _ENRICHMENT[table_key]
        with self.engine.connect() as conn:
            # updated_at is returned for the projector's photo remaining-TTL math
            # (B2). Postgres yields a tz-aware datetime here; the projector coerces.
            row = conn.execute(text(
                f"SELECT payload, deleted_at, updated_at FROM {schema}.{table} WHERE venue_id=:v"
            ), {"v": venue_id}).mappings().first()
            return dict(row) if row else None

    # ── live busyness (current-state) ─────────────────────────────────────────
    def upsert_live_forecast(self, venue_id, payload) -> bool:
        """Upsert the live-forecast snapshot, no-op when `venue_id` is absent
        from venues.venue instead of raising ForeignKeyViolation
        (live_forecast_venue_id_fkey). The write is keyed off the live-forecast
        payload's own venue_id (see VenueRepository.set_live_forecast), which is
        not always guaranteed to match a currently-cataloged venue — an
        INSERT...SELECT...WHERE EXISTS guard makes the write conditional so a
        missing venue is a benign no-op rather than an exception. Returns True
        when a row was written (inserted or updated), False when skipped."""
        with self.engine.begin() as conn:
            result = conn.execute(text(
                "INSERT INTO besttime.live_forecast (venue_id, payload, updated_at) "
                "SELECT :v, CAST(:p AS jsonb), now() "
                "WHERE EXISTS (SELECT 1 FROM venues.venue WHERE venue_id = :v) "
                "ON CONFLICT (venue_id) DO UPDATE SET payload=excluded.payload, updated_at=now()"
            ), {"v": venue_id, "p": json.dumps(payload)})
            return result.rowcount > 0

    def get_live_forecast(self, venue_id) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT payload FROM besttime.live_forecast WHERE venue_id=:v"
            ), {"v": venue_id}).mappings().first()
            return dict(row) if row else None

    # ── engagement ────────────────────────────────────────────────────────────
    def upsert_favorite(self, user_pseudo, venue_id) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO engagement.favorite (user_pseudo, venue_id, deleted_at, updated_at) "
                "VALUES (:u, :v, NULL, now()) "
                "ON CONFLICT (user_pseudo, venue_id) DO UPDATE SET deleted_at=NULL, updated_at=now()"
            ), {"u": user_pseudo, "v": venue_id})

    def soft_delete_favorite(self, user_pseudo, venue_id) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE engagement.favorite SET deleted_at=now(), updated_at=now() "
                "WHERE user_pseudo=:u AND venue_id=:v"
            ), {"u": user_pseudo, "v": venue_id})

    def add_hot_like_event(self, user_pseudo, venue_id) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO engagement.hot_like_event (user_pseudo, venue_id) VALUES (:u, :v)"
            ), {"u": user_pseudo, "v": venue_id})

    # ── app activity (one row per user per Recife day) ────────────────────────
    def record_app_session(self, user_pseudo, activity_date) -> None:
        # Idempotent per (user, day): the PK + DO NOTHING absorbs repeat pings.
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO engagement.app_session_day (user_pseudo, activity_date) "
                "VALUES (:u, :d) ON CONFLICT (user_pseudo, activity_date) DO NOTHING"
            ), {"u": user_pseudo, "d": activity_date})

    def count_users(self, since_date=None) -> int:
        """Distinct active users; since_date=None counts all-time total, otherwise
        users with any session on/after since_date (inclusive)."""
        sql = "SELECT count(DISTINCT user_pseudo) FROM engagement.app_session_day"
        params: dict = {}
        if since_date is not None:
            sql += " WHERE activity_date >= :since"
            params["since"] = since_date
        with self.engine.connect() as conn:
            return int(conn.execute(text(sql), params).scalar() or 0)

    # ── admin config (system of record; mirrored to Redis by AdminConfigService) ─
    def upsert_admin_config(self, key, value, updated_by=None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO admin.admin_config (key, value, updated_by, updated_at) "
                "VALUES (:k, CAST(:v AS jsonb), :u, now()) "
                "ON CONFLICT (key) DO UPDATE SET "
                "value=excluded.value, updated_by=excluded.updated_by, updated_at=now()"
            ), {"k": key, "v": json.dumps(value), "u": updated_by})

    def get_admin_config(self, key) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT key, value, updated_by, updated_at FROM admin.admin_config WHERE key=:k"
            ), {"k": key}).mappings().first()
            return dict(row) if row else None

    def delete_admin_config(self, key) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM admin.admin_config WHERE key=:k"), {"k": key})

    def list_admin_config(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(
                "SELECT key, value, updated_by, updated_at FROM admin.admin_config"
            )).mappings()]

    # ── eligibility rules (Ex2: normalized admin.eligibility_rule) ─────────────
    def list_eligibility_rules(self) -> list[tuple[str, str]]:
        with self.engine.connect() as conn:
            return [(r[0], r[1]) for r in conn.execute(text(
                "SELECT rule_type, value FROM admin.eligibility_rule "
                "ORDER BY rule_type, value"
            ))]

    def add_eligibility_rule(self, rule_type, value, updated_by=None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO admin.eligibility_rule (rule_type, value, updated_by, updated_at) "
                "VALUES (:t, :v, :u, now()) "
                "ON CONFLICT (rule_type, value) DO UPDATE SET "
                "updated_by=excluded.updated_by, updated_at=now()"
            ), {"t": rule_type, "v": value, "u": updated_by})

    def remove_eligibility_rule(self, rule_type, value) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM admin.eligibility_rule WHERE rule_type=:t AND value=:v"
            ), {"t": rule_type, "v": value})

    def replace_eligibility_rules(self, rules, updated_by=None) -> None:
        """Replace the whole rule set in one transaction (full-blob set)."""
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM admin.eligibility_rule"))
            for rule_type, value in rules:
                conn.execute(text(
                    "INSERT INTO admin.eligibility_rule (rule_type, value, updated_by, updated_at) "
                    "VALUES (:t, :v, :u, now()) ON CONFLICT (rule_type, value) DO NOTHING"
                ), {"t": rule_type, "v": value, "u": updated_by})

    # ── geo-fence (admin.geo_fence enabled flag + admin.geo_fence_city circles;
    #    read by serving.eligible_venue) ────────────────────────────────────────
    def get_geo_fence(self) -> dict:
        """Return the active fence: {"enabled": bool, "cities": [{slug, name,
        lat, lng, radius_km}]} with circles sorted by name. Defensive: any read
        failure (notably the deploy-before-migration window where
        admin.geo_fence_city does not exist yet) logs and serves the seeded
        default so the admin GET never 500s."""
        from app.services.venue_eligibility import default_geo_fence

        try:
            with self.engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT enabled FROM admin.geo_fence WHERE id = 1"
                )).first()
                cities = conn.execute(text(
                    "SELECT slug, name, lat, lng, radius_km "
                    "FROM admin.geo_fence_city ORDER BY name"
                )).mappings().all()
        except Exception as e:
            logger.error(f"[RdsVenueStore] geo-fence read failed; serving defaults: {e}")
            return default_geo_fence()
        if row is None:
            return default_geo_fence()
        return {
            "enabled": bool(row[0]),
            "cities": [{
                "slug": c["slug"], "name": c["name"],
                "lat": float(c["lat"]), "lng": float(c["lng"]),
                "radius_km": float(c["radius_km"]),
            } for c in cities],
        }

    def set_geo_fence(self, fence: dict, updated_by=None) -> None:
        """Transactionally replace admin.geo_fence_city with the validated
        circle list and upsert the singleton enabled flag — all-or-nothing so an
        invalid write can never partially apply. The serving view reads these
        tables directly, so the next projection reflects the new fence."""
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO admin.geo_fence (id, enabled, updated_by, updated_at) "
                "VALUES (1, :enabled, :u, now()) "
                "ON CONFLICT (id) DO UPDATE SET enabled=excluded.enabled, "
                "updated_by=excluded.updated_by, updated_at=now()"
            ), {"enabled": fence.get("enabled", True), "u": updated_by})
            conn.execute(text("DELETE FROM admin.geo_fence_city"))
            for city in fence.get("cities", []):
                conn.execute(text(
                    "INSERT INTO admin.geo_fence_city "
                    "(slug, name, lat, lng, radius_km, updated_by, updated_at) "
                    "VALUES (:slug, :name, :lat, :lng, :radius_km, :u, now())"
                ), {
                    "slug": city["slug"], "name": city["name"],
                    "lat": city["lat"], "lng": city["lng"],
                    "radius_km": city["radius_km"], "u": updated_by,
                })

    # Shared circle-membership predicate (haversine on the 6371.0088 km mean
    # Earth radius) — keep in lockstep with the serving view's geo term. The
    # leading EXISTS keeps an empty circle list fail-open (excludes nothing),
    # matching the view and geo_excluded().
    _OUTSIDE_CIRCLES_SQL = (
        "AND EXISTS (SELECT 1 FROM admin.geo_fence_city) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM admin.geo_fence_city c "
        "  WHERE 2 * 6371.0088 * asin(sqrt("
        "          pow(sin(radians(a.lat - c.lat) / 2), 2)"
        "          + cos(radians(c.lat)) * cos(radians(a.lat))"
        "            * pow(sin(radians(a.lng - c.lng) / 2), 2))) <= c.radius_km)"
    )

    def count_geo_excluded_active_venues(self) -> int:
        """Active venues whose coordinates fall OUTSIDE every enabled fence
        circle — the reversible serve-time exclusion (observability only).
        Fail-open: a disabled fence, an empty circle list, or a NULL-coord
        address contributes zero. The haversine duplicates the view's geo term
        so this gauge stays in lockstep with serving membership."""
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT count(*) FROM venues.venue ve "
                "JOIN venues.address a ON a.venue_id = ve.venue_id "
                "CROSS JOIN admin.geo_fence f "
                "WHERE ve.lifecycle_status = 'active' AND f.id = 1 AND f.enabled "
                "AND a.lat IS NOT NULL AND a.lng IS NOT NULL "
                + self._OUTSIDE_CIRCLES_SQL
            )).first()
        return int(row[0]) if row else 0

    def count_active_venues_outside_circles(self) -> int:
        """Active venues whose coordinates fall outside EVERY configured circle,
        regardless of the enabled flag — the admin panel's warning number: how
        many venues the restriction excludes while on, and how many re-enter
        serving the moment it is turned off (and leave again when re-enabled).
        An empty circle list counts zero (no circles = no restriction)."""
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT count(*) FROM venues.venue ve "
                "JOIN venues.address a ON a.venue_id = ve.venue_id "
                "WHERE ve.lifecycle_status = 'active' "
                "AND a.lat IS NOT NULL AND a.lng IS NOT NULL "
                + self._OUTSIDE_CIRCLES_SQL
            )).first()
        return int(row[0]) if row else 0
