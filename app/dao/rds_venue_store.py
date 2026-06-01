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
from typing import Optional

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

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
    def upsert_venue(self, venue) -> None:
        p = venue.model_dump(by_alias=True, mode="json")
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO venues.venue (venue_id, venue_name, venue_address, "
                "venue_lat, venue_lng, venue_type, price_level, rating, reviews, "
                "forecast, processed, lifecycle_status, deprecated_reason, "
                "deprecated_source, deprecated_at, google_business_status, payload, updated_at) "
                "VALUES (:venue_id, :venue_name, :venue_address, :venue_lat, :venue_lng, "
                ":venue_type, :price_level, :rating, :reviews, :forecast, :processed, "
                ":lifecycle_status, :deprecated_reason, :deprecated_source, :deprecated_at, "
                ":google_business_status, CAST(:payload AS jsonb), now()) "
                "ON CONFLICT (venue_id) DO UPDATE SET "
                "venue_name=excluded.venue_name, venue_address=excluded.venue_address, "
                "venue_lat=excluded.venue_lat, venue_lng=excluded.venue_lng, "
                "venue_type=excluded.venue_type, price_level=excluded.price_level, "
                "rating=excluded.rating, reviews=excluded.reviews, forecast=excluded.forecast, "
                "processed=excluded.processed, lifecycle_status=excluded.lifecycle_status, "
                "deprecated_reason=excluded.deprecated_reason, deprecated_source=excluded.deprecated_source, "
                "deprecated_at=excluded.deprecated_at, google_business_status=excluded.google_business_status, "
                "payload=excluded.payload, updated_at=now()"
            ), {
                "venue_id": venue.venue_id, "venue_name": venue.venue_name,
                "venue_address": venue.venue_address, "venue_lat": venue.venue_lat,
                "venue_lng": venue.venue_lng, "venue_type": venue.venue_type,
                "price_level": venue.price_level, "rating": venue.rating,
                "reviews": venue.reviews, "forecast": venue.forecast,
                "processed": venue.processed, "lifecycle_status": venue.lifecycle_status,
                "deprecated_reason": venue.deprecated_reason,
                "deprecated_source": venue.deprecated_source,
                "deprecated_at": venue.deprecated_at,
                "google_business_status": venue.google_business_status,
                "payload": json.dumps(p),
            })

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
                "SELECT venue_id, lifecycle_status, deprecated_reason, deprecated_source, "
                "payload FROM venues.venue WHERE venue_id=:v"
            ), {"v": venue_id}).mappings().first()
            return dict(row) if row else None

    def list_active_venue_ids(self) -> list[str]:
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT venue_id FROM venues.venue WHERE lifecycle_status='active'"
            ))]

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
        schema, table, _ = _ENRICHMENT[table_key]
        with self.engine.begin() as conn:
            conn.execute(text(
                f"UPDATE {schema}.{table} SET deleted_at=now() WHERE venue_id=:v"
            ), {"v": venue_id})
            if history:
                self._history(conn, schema, table, venue_id, {}, "soft_delete")

    def get_enrichment(self, table_key, venue_id) -> Optional[dict]:
        if table_key == _WEEKLY:
            vid, _, day = venue_id.partition("#")
            with self.engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT payload, deleted_at FROM besttime.weekly_forecast "
                    "WHERE venue_id=:v AND day_int=:d"
                ), {"v": vid, "d": int(day)}).mappings().first()
                return dict(row) if row else None
        schema, table, _ = _ENRICHMENT[table_key]
        with self.engine.connect() as conn:
            row = conn.execute(text(
                f"SELECT payload, deleted_at FROM {schema}.{table} WHERE venue_id=:v"
            ), {"v": venue_id}).mappings().first()
            return dict(row) if row else None

    # ── live busyness (current-state) ─────────────────────────────────────────
    def upsert_live_forecast(self, venue_id, payload) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO besttime.live_forecast (venue_id, payload, updated_at) "
                "VALUES (:v, CAST(:p AS jsonb), now()) "
                "ON CONFLICT (venue_id) DO UPDATE SET payload=excluded.payload, updated_at=now()"
            ), {"v": venue_id, "p": json.dumps(payload)})

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
