"""Rebuild Redis from RDS, and one-time backfill RDS from Redis.

- rebuild_redis_from_rds(): RDS -> Redis projection for every active venue,
  INCLUDING the geo index (via redis_only_dao.upsert_venue -> GEOADD) and live
  busyness. This is disaster recovery + Redis warm. Photos are projected with
  their remaining TTL so expired Google URLs refetch instead of serving stale.
- backfill_rds_from_redis(): one-time import of the existing Redis dataset into
  RDS by re-running each record through the write-through repository (venues
  first, satisfying the FK order). Idempotent.
"""
from __future__ import annotations

import logging

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

# Redis getter <-> repository setter pairs for the "model in / model out" types
# used by backfill. (venue, photos, weekly, live are handled specially.)
_MODEL_PAIRS = [
    ("get_vibe_attributes", "set_vibe_attributes"),
    ("get_opening_hours", "set_opening_hours"),
    ("get_venue_reviews", "set_venue_reviews"),
    ("get_venue_instagram", "set_venue_instagram"),
    ("get_venue_ig_posts", "set_venue_ig_posts"),
    ("get_venue_menu_photos", "set_venue_menu_photos"),
    ("get_venue_menu_data", "set_venue_menu_data"),
    ("get_venue_vibe_profile", "set_venue_vibe_profile"),
]

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
    def __init__(self, repository, redis_only_dao, rds_store):
        self.repository = repository          # write-through (RDS + Redis)
        self.redis_only_dao = redis_only_dao  # Redis-only projection writer
        self.rds_store = rds_store

    # ── backfill: Redis -> RDS (one-time) ─────────────────────────────────────
    def backfill_rds_from_redis(self) -> dict:
        summary = {"venues": 0, "enrichment": 0, "errors": 0}
        venues = self.redis_only_dao.list_all_venues()
        for venue in venues:  # venues first -> FK order satisfied
            try:
                self.repository.upsert_venue(venue)
                summary["venues"] += 1
            except Exception as e:
                summary["errors"] += 1
                logger.warning(f"[Backfill] venue {venue.venue_id} failed: {e}")
                continue
            vid = venue.venue_id
            for getter, setter in _MODEL_PAIRS:
                obj = getattr(self.redis_only_dao, getter)(vid)
                if obj is not None:
                    getattr(self.repository, setter)(obj)
                    summary["enrichment"] += 1
            photos = self.redis_only_dao.get_venue_photos(vid)
            if photos:
                self.repository.set_venue_photos(vid, photos)
                summary["enrichment"] += 1
            for day_int in range(7):
                day = self.redis_only_dao.get_week_raw_forecast(vid, day_int)
                if day is not None:
                    self.repository.set_week_raw_forecast(vid, day)
                    summary["enrichment"] += 1
            live = self.redis_only_dao.get_live_forecast(vid)
            if live is not None:
                self.repository.set_live_forecast(live)
        logger.info(f"[Backfill] {summary}")
        return summary

    # ── rebuild: RDS -> Redis (incl. geo index + live busyness) ───────────────
    def rebuild_redis_from_rds(self) -> dict:
        summary = {"venues": 0, "enrichment": 0, "live": 0, "errors": 0}
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
            # photos: project with remaining TTL (expired -> absent -> refetch)
            photos_rec = self.rds_store.get_enrichment("google_places.photos", venue_id)
            if photos_rec and photos_rec.get("deleted_at") is None:
                self.redis_only_dao.set_venue_photos(
                    venue_id, photos_rec["payload"].get("photos", [])
                )
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
        logger.info(f"[Rebuild] {summary}")
        return summary
