"""Write-through repository: RDS is the system of record, Redis the projection.

VenueRepository subclasses RedisVenueDAO so it exposes the identical interface
(reads inherited unchanged — including the geo index used by serving). Write and
delete methods first persist to RDS (truth), then project to Redis via the
inherited DAO method (which keeps the GEOADD geo index intact). When no RDS
store is wired (rds_enabled=false), it behaves exactly like RedisVenueDAO.

Never hard-deletes labels in RDS: delete_*/soft_delete map to RDS soft-deletes
(deleted_at) with append-only history for expensive derived labels. Photos and
live busyness are excluded from history (URLs expire / high churn).
"""
from __future__ import annotations

import logging

from app.dao.redis_venue_dao import RedisVenueDAO

logger = logging.getLogger(__name__)

# DAO set_* method -> (rds table_key, keep append-only history?)
_HISTORY = True
_NO_HISTORY = False


def _json(model) -> dict:
    return model.model_dump(by_alias=True, mode="json")


class VenueRepository(RedisVenueDAO):
    def __init__(self, client, rds_store=None):
        super().__init__(client)
        self.rds_store = rds_store

    # ── core venue ────────────────────────────────────────────────────────────
    def upsert_venue(self, venue) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_venue(venue)  # truth first; raises on failure
        super().upsert_venue(venue)  # projection (incl. GEOADD geo index)

    def soft_delete_venue(self, venue_id, reason, source, google_business_status=None) -> bool:
        if self.rds_store is not None:
            self.rds_store.soft_delete_venue(venue_id, reason, source, google_business_status)
        return super().soft_delete_venue(venue_id, reason, source, google_business_status)

    # ── besttime ──────────────────────────────────────────────────────────────
    def set_week_raw_forecast(self, venue_id, day) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "besttime.weekly_forecast", f"{venue_id}#{day.day_int}",
                _json(day), history=_HISTORY,
            )
        super().set_week_raw_forecast(venue_id, day)

    def set_live_forecast(self, forecast) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_live_forecast(
                forecast.venue_info.venue_id, _json(forecast)
            )
        super().set_live_forecast(forecast)

    # ── google_places ───────────────────────────────────────────────────────────
    def set_vibe_attributes(self, vibe_attrs) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "google_places.vibe_attributes", vibe_attrs.venue_id, _json(vibe_attrs),
                history=_HISTORY,
                promoted={
                    "google_primary_type": vibe_attrs.google_primary_type,
                    "google_place_id": vibe_attrs.google_place_id,
                },
            )
        super().set_vibe_attributes(vibe_attrs)

    def set_opening_hours(self, opening_hours) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "google_places.opening_hours", opening_hours.venue_id,
                _json(opening_hours), history=_HISTORY,
            )
        super().set_opening_hours(opening_hours)

    def set_venue_photos(self, venue_id, photos, ttl_seconds=None) -> None:
        # Photos excluded from append-only history (Google URLs expire).
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "google_places.photos", venue_id, {"photos": photos}, history=_NO_HISTORY,
            )
        # super() keeps the setex TTL — the freshness-refetch deliverable.
        super().set_venue_photos(venue_id, photos, ttl_seconds=ttl_seconds)

    def set_venue_reviews(self, reviews) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "google_places.reviews", reviews.venue_id, _json(reviews), history=_HISTORY,
            )
        super().set_venue_reviews(reviews)

    # ── instagram ───────────────────────────────────────────────────────────────
    def set_venue_instagram(self, instagram) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "instagram.handle", instagram.venue_id, _json(instagram), history=_HISTORY,
                promoted={"instagram_handle": getattr(instagram, "instagram_handle", None)},
            )
        super().set_venue_instagram(instagram)

    def set_venue_ig_posts(self, posts) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "instagram.posts", posts.venue_id, _json(posts), history=_HISTORY,
            )
        super().set_venue_ig_posts(posts)

    # ── venues (derived / menu / vibe profile) ──────────────────────────────────
    def set_venue_menu_photos(self, menu_photos) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "venues.menu_photos", menu_photos.venue_id, _json(menu_photos), history=_HISTORY,
            )
        super().set_venue_menu_photos(menu_photos)

    def set_venue_menu_data(self, menu_data) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "venues.menu_data", menu_data.venue_id, _json(menu_data), history=_HISTORY,
            )
        super().set_venue_menu_data(menu_data)

    def set_venue_vibe_profile(self, profile) -> None:
        if self.rds_store is not None:
            self.rds_store.upsert_enrichment(
                "venues.vibe_profile", profile.venue_id, _json(profile), history=_HISTORY,
            )
        super().set_venue_vibe_profile(profile)

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

    def _soft_delete_then_super(self, name, venue_id):
        table_key = self._DELETE_TABLE[name]
        history = table_key != "google_places.photos"
        if self.rds_store is not None:
            self.rds_store.soft_delete_enrichment(table_key, venue_id, history=history)
        return getattr(super(), name)(venue_id)

    def delete_vibe_attributes(self, venue_id):
        return self._soft_delete_then_super("delete_vibe_attributes", venue_id)

    def delete_opening_hours(self, venue_id):
        return self._soft_delete_then_super("delete_opening_hours", venue_id)

    def delete_venue_reviews(self, venue_id):
        return self._soft_delete_then_super("delete_venue_reviews", venue_id)

    def delete_venue_instagram(self, venue_id):
        return self._soft_delete_then_super("delete_venue_instagram", venue_id)

    def delete_venue_ig_posts(self, venue_id):
        return self._soft_delete_then_super("delete_venue_ig_posts", venue_id)

    def delete_venue_menu_photos(self, venue_id):
        return self._soft_delete_then_super("delete_venue_menu_photos", venue_id)

    def delete_venue_menu_data(self, venue_id):
        return self._soft_delete_then_super("delete_venue_menu_data", venue_id)

    def delete_venue_vibe_profile(self, venue_id):
        return self._soft_delete_then_super("delete_venue_vibe_profile", venue_id)
