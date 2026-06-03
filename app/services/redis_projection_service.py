"""Rebuild the Redis serving projection from RDS.

rebuild_redis_from_rds(): RDS -> Redis projection for every active venue,
INCLUDING the geo index (via redis_only_dao.upsert_venue -> GEOADD) and live
busyness. This is the scheduled projector body (and manual disaster recovery /
Redis warm). Photos are projected with their remaining TTL so expired Google
URLs refetch instead of serving stale.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.models import (
    LiveForecastResponse,
    Venue,
    WeekRawDay,
)
from app.models.vibe_attributes import VibeAttributes
from app.models.opening_hours import OpeningHours
from app.models.instagram import VenueInstagram, VenueInstagramPosts
from app.models.menu import VenueMenuData, VenueMenuPhotos
from app.models.venue_review import VenueReviews
from app.models.vibe_profile import VenueVibeProfile

logger = logging.getLogger(__name__)


def _age_seconds(updated_at) -> Optional[float]:
    """Seconds since `updated_at`, or None if it is missing/unparseable.

    Coerces both representations: the real RdsVenueStore SELECT yields a
    tz-aware ``datetime``; the in-memory fake / JSON yields an ISO ``str``. A
    naive timestamp is treated as UTC. The real store's SQL is not in CI, so
    handling both types here is what keeps B2 correct on real Postgres.
    """
    if updated_at is None:
        return None
    ts = updated_at
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# RDS enrichment table_key -> (model class, redis-only setter name) for rebuild.
_REBUILD_MODELS = {
    "google_places.vibe_attributes": (VibeAttributes, "set_vibe_attributes"),
    "google_places.opening_hours": (OpeningHours, "set_opening_hours"),
    "google_places.reviews": (VenueReviews, "set_venue_reviews"),
    "instagram.handle": (VenueInstagram, "set_venue_instagram"),
    "instagram.posts": (VenueInstagramPosts, "set_venue_ig_posts"),
    "venues.menu_photos": (VenueMenuPhotos, "set_venue_menu_photos"),
    "venues.menu_data": (VenueMenuData, "set_venue_menu_data"),
    "venues.vibe_profile": (VenueVibeProfile, "set_venue_vibe_profile"),
}


class RedisProjectionService:
    def __init__(self, redis_only_dao, rds_store):
        self.redis_only_dao = redis_only_dao  # Redis-only projection writer
        self.rds_store = rds_store

    # ── rebuild: RDS -> Redis (incl. geo index + live busyness) ───────────────
    def rebuild_redis_from_rds(self) -> dict:
        summary = {"venues": 0, "enrichment": 0, "live": 0, "removed": 0, "errors": 0}
        for venue_id in self.rds_store.list_active_venue_ids():
            row = self.rds_store.get_venue(venue_id)
            try:
                venue = Venue.model_validate(row["payload"])
                self.redis_only_dao.upsert_venue(venue)  # GEOADD + JSON
                summary["venues"] += 1
            except Exception as e:
                summary["errors"] += 1
                logger.warning(f"[Rebuild] venue {venue_id} failed: {e}")
                continue
            for table_key, (model_cls, setter) in _REBUILD_MODELS.items():
                rec = self.rds_store.get_enrichment(table_key, venue_id)
                if rec and rec.get("deleted_at") is None:
                    obj = model_cls.model_validate(rec["payload"])
                    getattr(self.redis_only_dao, setter)(obj)
                    summary["enrichment"] += 1
            self._project_photos(venue_id)
            # weekly (keys stored as "<venue_id>#<day_int>")
            for day_int in range(7):
                wk = self.rds_store.get_enrichment(
                    "besttime.weekly_forecast", f"{venue_id}#{day_int}"
                )
                if wk and wk.get("deleted_at") is None:
                    self.redis_only_dao.set_week_raw_forecast(
                        venue_id, WeekRawDay.model_validate(wk["payload"])
                    )
            live = self.rds_store.get_live_forecast(venue_id)
            if live is not None:
                self.redis_only_dao.set_live_forecast(
                    LiveForecastResponse.model_validate(live["payload"])
                )
                summary["live"] += 1
        # B1: a venue deprecated in RDS is a positive removal signal — drop it
        # from the Redis serving set + geo index. Orphans (no RDS row at all) are
        # never in this list, so they are left untouched (partial-read safe).
        for venue_id in self.rds_store.list_deprecated_venue_ids():
            if self.redis_only_dao.delete_venue(venue_id):
                summary["removed"] += 1
        logger.info(f"[Rebuild] {summary}")
        return summary

    def _project_photos(self, venue_id: str) -> None:
        """B2: project photos with the REMAINING TTL (full − age) so repeated
        runs count the TTL down instead of re-stamping a fresh full TTL; drop
        photos aged past the TTL so stale Google URLs leave serving and the
        refetch trigger fires."""
        rec = self.rds_store.get_enrichment("google_places.photos", venue_id)
        if not rec or rec.get("deleted_at") is not None:
            return
        full_ttl = self.redis_only_dao._resolve_photos_cache_ttl_seconds()
        age = _age_seconds(rec.get("updated_at"))
        remaining = full_ttl if age is None else int(full_ttl - age)
        if remaining > 0:
            self.redis_only_dao.set_venue_photos(
                venue_id, rec["payload"].get("photos", []), ttl_seconds=remaining
            )
        else:
            self.redis_only_dao.delete_venue_photos(venue_id)
