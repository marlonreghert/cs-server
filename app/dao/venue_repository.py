"""RDS-system-of-record repository: RDS is the truth, Redis the serving projection.

VenueRepository subclasses RedisVenueDAO so it keeps the serving geo reads
(get_nearby_venues / count_venues_in_radius) on Redis — by design, not a leftover.
Pipelines use this DAO and:
  - READ their data inputs and cache-freshness gating from RDS (truth), so a later
    pipeline stage sees an earlier stage's output without waiting for projection;
  - WRITE RDS-only — the scheduled off-loop projector is the sole Redis writer for
    pipeline data (it rebuilds the serving projection from RDS).

Never hard-deletes labels in RDS: delete_*/soft_delete map to RDS soft-deletes
(deleted_at) with append-only history for expensive derived labels. Photos and
live busyness are excluded from history (URLs expire / high churn).
"""
from __future__ import annotations

import logging

from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.dao.venue_row import venue_from_row
from app.models import LiveForecastResponse, WeekRawDay
from app.models.instagram import VenueInstagram, VenueInstagramPosts
from app.models.menu import VenueMenuData, VenueMenuPhotos
from app.models.opening_hours import OpeningHours
from app.models.venue_review import VenueReviews
from app.models.vibe_attributes import VibeAttributes
from app.models.vibe_profile import VenueVibeProfile

logger = logging.getLogger(__name__)

# DAO set_* method -> (rds table_key, keep append-only history?)
_HISTORY = True
_NO_HISTORY = False


def _json(model) -> dict:
    return model.model_dump(by_alias=True, mode="json")


class VenueRepository(RedisVenueDAO):
    def __init__(self, client, rds_store):
        super().__init__(client)  # geo reads (get_nearby_venues, etc.) stay on Redis
        self.rds_store = rds_store

    # ── pipeline data reads from RDS ────────────────────────────────────────────
    def _rds_enrichment(self, table_key, model_cls, venue_id):
        """Reconstruct a typed enrichment model from the RDS payload (None if
        absent or soft-deleted). Single reconstruction path for every getter."""
        rec = self.rds_store.get_enrichment(table_key, venue_id)
        if not rec or rec.get("deleted_at") is not None:
            return None
        return model_cls.model_validate(rec["payload"])

    def get_venue(self, venue_id):
        row = self.rds_store.get_venue(venue_id)
        return venue_from_row(row) if row else None

    def get_vibe_attributes(self, venue_id):
        return self._rds_enrichment("google_places.vibe_attributes", VibeAttributes, venue_id)

    def get_opening_hours(self, venue_id):
        return self._rds_enrichment("google_places.opening_hours", OpeningHours, venue_id)

    def get_venue_reviews(self, venue_id):
        return self._rds_enrichment("google_places.reviews", VenueReviews, venue_id)

    def get_venue_instagram(self, venue_id):
        return self._rds_enrichment("instagram.handle", VenueInstagram, venue_id)

    def get_venue_ig_posts(self, venue_id):
        return self._rds_enrichment("instagram.posts", VenueInstagramPosts, venue_id)

    def get_venue_menu_photos(self, venue_id):
        return self._rds_enrichment("venues.menu_photos", VenueMenuPhotos, venue_id)

    def get_venue_menu_data(self, venue_id):
        return self._rds_enrichment("venues.menu_data", VenueMenuData, venue_id)

    def get_venue_vibe_profile(self, venue_id):
        return self._rds_enrichment("venues.vibe_profile", VenueVibeProfile, venue_id)

    def get_venue_photos(self, venue_id):
        rec = self.rds_store.get_enrichment("google_places.photos", venue_id)
        if not rec or rec.get("deleted_at") is not None:
            return None
        return rec["payload"].get("photos") or None

    def get_week_raw_forecast(self, venue_id, day_int):
        rec = self.rds_store.get_enrichment(
            "besttime.weekly_forecast", f"{venue_id}#{day_int}"
        )
        if not rec or rec.get("deleted_at") is not None:
            return None
        return WeekRawDay.model_validate(rec["payload"])

    def get_live_forecast(self, venue_id):
        rec = self.rds_store.get_live_forecast(venue_id)
        return LiveForecastResponse.model_validate(rec["payload"]) if rec else None

    def list_active_venue_ids(self):
        return self.rds_store.list_active_venue_ids()

    def list_servable_venue_ids(self):
        """Active AND eligible venues (the serving view) — the projector's serving
        source and the enrichment gate. Eligibility is applied here, in RDS, not by
        a destructive soft-delete, so block-list edits are reversible by projection."""
        return self.rds_store.list_servable_venue_ids()

    def list_active_venue_ids_by_priority(self, limit):
        return self.rds_store.list_active_venue_ids_by_priority(limit)

    def list_servable_venue_ids_by_priority(self, limit):
        """The served (serving-view) venues ordered by priority — the bounded
        refresh selection source. Served-scoped counterpart of
        list_active_venue_ids_by_priority."""
        return self.rds_store.list_servable_venue_ids_by_priority(limit)

    def list_all_venues(self):
        out = []
        for row in self.rds_store.list_all_venue_rows():
            try:
                out.append(venue_from_row(row))
            except Exception as e:
                logger.warning(f"[VenueRepository] RDS list_all_venues skip: {e}")
        return out

    # ── writes: RDS-only — the projector is the sole Redis writer ────────────────
    # ── core venue ────────────────────────────────────────────────────────────
    def upsert_venue(self, venue) -> None:
        self.rds_store.upsert_venue(venue)  # truth; projector projects to Redis + geo

    def soft_delete_venue(self, venue_id, reason, source, google_business_status=None) -> bool:
        self.rds_store.soft_delete_venue(venue_id, reason, source, google_business_status)
        return True

    # ── besttime ──────────────────────────────────────────────────────────────
    def set_week_raw_forecast(self, venue_id, day) -> None:
        self.rds_store.upsert_enrichment(
            "besttime.weekly_forecast", f"{venue_id}#{day.day_int}",
            _json(day), history=_HISTORY,
        )

    def set_live_forecast(self, forecast) -> None:
        self.rds_store.upsert_live_forecast(forecast.venue_info.venue_id, _json(forecast))

    def delete_live_forecast(self, venue_id):
        self.rds_store.delete_live_forecast(venue_id)
        return None

    # ── google_places ───────────────────────────────────────────────────────────
    def set_vibe_attributes(self, vibe_attrs) -> None:
        self.rds_store.upsert_enrichment(
            "google_places.vibe_attributes", vibe_attrs.venue_id, _json(vibe_attrs),
            history=_HISTORY,
            promoted={
                "google_primary_type": vibe_attrs.google_primary_type,
                "google_place_id": vibe_attrs.google_place_id,
            },
        )

    def set_opening_hours(self, opening_hours) -> None:
        self.rds_store.upsert_enrichment(
            "google_places.opening_hours", opening_hours.venue_id,
            _json(opening_hours), history=_HISTORY,
        )

    def set_venue_photos(self, venue_id, photos, ttl_seconds=None) -> None:
        # Photos excluded from append-only history (Google URLs expire). The
        # projector re-applies the remaining TTL when it projects to Redis (B2).
        self.rds_store.upsert_enrichment(
            "google_places.photos", venue_id, {"photos": photos}, history=_NO_HISTORY,
        )

    def set_venue_reviews(self, reviews) -> None:
        self.rds_store.upsert_enrichment(
            "google_places.reviews", reviews.venue_id, _json(reviews), history=_HISTORY,
        )

    # ── instagram ───────────────────────────────────────────────────────────────
    def set_venue_instagram(self, instagram) -> None:
        self.rds_store.upsert_enrichment(
            "instagram.handle", instagram.venue_id, _json(instagram), history=_HISTORY,
            promoted={"instagram_handle": getattr(instagram, "instagram_handle", None)},
        )

    def set_venue_ig_posts(self, posts) -> None:
        self.rds_store.upsert_enrichment(
            "instagram.posts", posts.venue_id, _json(posts), history=_HISTORY,
        )

    # ── venues (derived / menu / vibe profile) ──────────────────────────────────
    def set_venue_menu_photos(self, menu_photos) -> None:
        self.rds_store.upsert_enrichment(
            "venues.menu_photos", menu_photos.venue_id, _json(menu_photos), history=_HISTORY,
        )

    def set_venue_menu_data(self, menu_data) -> None:
        self.rds_store.upsert_enrichment(
            "venues.menu_data", menu_data.venue_id, _json(menu_data), history=_HISTORY,
        )

    def set_venue_vibe_profile(self, profile) -> None:
        self.rds_store.upsert_enrichment(
            "venues.vibe_profile", profile.venue_id, _json(profile), history=_HISTORY,
        )

    # ── cache-freshness gating: RDS status-aware staleness ───────────────────────
    def list_cached_venue_photos_ids(self):
        return self.rds_store.list_fresh_enrichment_venue_ids(
            "google_places.photos",
            max_age_seconds=self._resolve_photos_cache_ttl_seconds(),
        )

    def list_cached_vibe_profile_venue_ids(self):
        return self.rds_store.list_fresh_enrichment_venue_ids("venues.vibe_profile")

    def list_cached_menu_photos_venue_ids(self):
        return self.rds_store.list_fresh_enrichment_venue_ids("venues.menu_photos")

    def list_cached_ig_posts_venue_ids(self):
        return self.rds_store.list_fresh_enrichment_venue_ids(
            "instagram.posts", max_age_seconds=settings.ig_posts_cache_ttl_days * 86400,
        )

    def list_cached_instagram_venue_ids(self):
        return self.rds_store.list_fresh_instagram_venue_ids(
            found_max_age_seconds=settings.instagram_cache_ttl_days * 86400,
            not_found_max_age_seconds=settings.instagram_not_found_cache_ttl_days * 86400,
        )

    # ── deletes become RDS soft-deletes (never hard-delete labels) ───────────────
    _DELETE_TABLE = {
        "delete_vibe_attributes": "google_places.vibe_attributes",
        "delete_opening_hours": "google_places.opening_hours",
        "delete_venue_reviews": "google_places.reviews",
        "delete_venue_instagram": "instagram.handle",
        "delete_venue_ig_posts": "instagram.posts",
        "delete_venue_menu_photos": "venues.menu_photos",
        "delete_venue_menu_data": "venues.menu_data",
        "delete_venue_vibe_profile": "venues.vibe_profile",
    }

    def _soft_delete_enrichment(self, name, venue_id):
        table_key = self._DELETE_TABLE[name]
        history = table_key != "google_places.photos"
        self.rds_store.soft_delete_enrichment(table_key, venue_id, history=history)
        return None

    def delete_vibe_attributes(self, venue_id):
        return self._soft_delete_enrichment("delete_vibe_attributes", venue_id)

    def delete_opening_hours(self, venue_id):
        return self._soft_delete_enrichment("delete_opening_hours", venue_id)

    def delete_venue_reviews(self, venue_id):
        return self._soft_delete_enrichment("delete_venue_reviews", venue_id)

    def delete_venue_instagram(self, venue_id):
        return self._soft_delete_enrichment("delete_venue_instagram", venue_id)

    def delete_venue_ig_posts(self, venue_id):
        return self._soft_delete_enrichment("delete_venue_ig_posts", venue_id)

    def delete_venue_menu_photos(self, venue_id):
        return self._soft_delete_enrichment("delete_venue_menu_photos", venue_id)

    def delete_venue_menu_data(self, venue_id):
        return self._soft_delete_enrichment("delete_venue_menu_data", venue_id)

    def delete_venue_vibe_profile(self, venue_id):
        return self._soft_delete_enrichment("delete_venue_vibe_profile", venue_id)
