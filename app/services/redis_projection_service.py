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

from app.dao.venue_row import venue_from_row
from app.metrics import (
    REDIS_PROJECTION_ENTITY_DELETES_TOTAL,
    REDIS_PROJECTION_REMOVED_TOTAL,
    REDIS_PROJECTION_VENUES,
    SERVING_VIEW_VENUES,
    VENUES_GEO_EXCLUDED,
)
from app.models import (
    LiveForecastResponse,
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


# RDS enrichment table_key -> (model class, redis-only setter name, redis-only
# deleter name) for rebuild. When the bulk-prefetched record for a servable
# venue is absent (soft-deleted or never written in RDS -- get_enrichment_bulk
# already filters `deleted_at IS NULL`), the deleter propagates that absence
# to Redis so a stale key never outlives its RDS row.
_REBUILD_MODELS = {
    "google_places.vibe_attributes": (VibeAttributes, "set_vibe_attributes", "delete_vibe_attributes"),
    "google_places.opening_hours": (OpeningHours, "set_opening_hours", "delete_opening_hours"),
    "google_places.reviews": (VenueReviews, "set_venue_reviews", "delete_venue_reviews"),
    "instagram.handle": (VenueInstagram, "set_venue_instagram", "delete_venue_instagram"),
    "instagram.posts": (VenueInstagramPosts, "set_venue_ig_posts", "delete_venue_ig_posts"),
    "venues.menu_photos": (VenueMenuPhotos, "set_venue_menu_photos", "delete_venue_menu_photos"),
    "venues.menu_data": (VenueMenuData, "set_venue_menu_data", "delete_venue_menu_data"),
    "venues.vibe_profile": (VenueVibeProfile, "set_venue_vibe_profile", "delete_venue_vibe_profile"),
}

_WEEK_DAYS = range(7)


class RedisProjectionService:
    def __init__(self, redis_only_dao, rds_store, eligibility_rule_service=None):
        self.redis_only_dao = redis_only_dao  # Redis-only projection writer
        self.rds_store = rds_store
        # Optional: the eligibility serving mirror (an admin carve-out) is
        # re-asserted from its rows each cycle so a Redis flush self-heals,
        # symmetric with the venue projection. Delegated + isolated.
        self.eligibility_rule_service = eligibility_rule_service

    # ── rebuild: RDS -> Redis (incl. geo index + live busyness) ───────────────
    def rebuild_redis_from_rds(self) -> dict:
        summary = {
            "venues": 0, "enrichment": 0, "live": 0, "removed": 0, "errors": 0,
            # Venue ids that hit an isolated per-venue exception this cycle
            # (observability: "the run summary must report at least one error
            # naming <venue>" -- the log line carries the failing stage too).
            "error_venues": [],
        }
        # Serving source = the eligibility view (active AND eligible under the live
        # block-list). A failed view read must NOT blanket-delete the serving set —
        # abort the cycle and leave Redis intact (fail-safe).
        try:
            servable_ids = self.rds_store.list_servable_venue_ids()
        except Exception as e:
            logger.error(f"[Rebuild] serving view read failed; aborting cycle: {e}")
            summary["errors"] += 1
            return summary
        servable_set = set(servable_ids)
        SERVING_VIEW_VENUES.set(len(servable_set))
        # Geo-fence effect (observability only): active venues currently dropped
        # from serving because their coords are outside the enabled box. Best-effort
        # — a count failure must never abort the projection.
        try:
            geo_excluded_count = self.rds_store.count_geo_excluded_active_venues()
            VENUES_GEO_EXCLUDED.set(geo_excluded_count)
            summary["geo_excluded"] = geo_excluded_count
        except Exception as e:
            logger.warning(f"[Rebuild] geo-excluded count failed: {e}")
        # Bulk-prefetch every input the per-venue loop below needs, once per
        # cycle (P1): 1 venue-rows query + 9 enrichment-table queries (the 8
        # _REBUILD_MODELS tables + photos) + 1 weekly query + 1 live query = 12
        # bulk reads total, independent of how many servable venues exist —
        # replacing what was ~18 SQL queries PER VENUE. The per-venue projection
        # logic below is unchanged; only the source of each row/rec moves from a
        # per-call SELECT to a dict lookup on these prefetched maps.
        venue_rows = self.rds_store.get_venues_by_ids(servable_ids)
        enrichment_maps = {
            table_key: self.rds_store.get_enrichment_bulk(table_key, servable_ids)
            for table_key in _REBUILD_MODELS
        }
        photos_map = self.rds_store.get_enrichment_bulk(
            "google_places.photos", servable_ids
        )
        weekly_map = self.rds_store.get_weekly_bulk(servable_ids)
        live_map = self.rds_store.get_live_bulk(servable_ids)

        for venue_id in servable_ids:
            # Isolation boundary: any exception while reading/projecting this ONE
            # venue's row, enrichment, photos, weekly, or live data must not abort
            # the run for other venues or skip the reconcile/removal pass below.
            # `stage` tags the log line with where it failed; the try wraps every
            # per-entity stage (widened from the old venue-read-only try) so a
            # single poisoned row (e.g. a payload that fails Pydantic validation)
            # degrades to "this venue's remaining stages wait for next cycle"
            # instead of killing the whole projection run.
            stage = "venue"
            try:
                row = venue_rows.get(venue_id)
                venue = venue_from_row(row)  # Ex1: columns + residual, not payload
                self.redis_only_dao.upsert_venue(venue)  # GEOADD + JSON
                summary["venues"] += 1

                stage = "enrichment"
                for table_key, (model_cls, setter, deleter) in _REBUILD_MODELS.items():
                    rec = enrichment_maps[table_key].get(venue_id)
                    if rec is not None:
                        obj = model_cls.model_validate(rec["payload"])
                        getattr(self.redis_only_dao, setter)(obj)
                        summary["enrichment"] += 1
                    elif getattr(self.redis_only_dao, deleter)(venue_id):
                        REDIS_PROJECTION_ENTITY_DELETES_TOTAL.labels(entity=table_key).inc()

                stage = "photos"
                self._project_photos(venue_id, photos_map.get(venue_id))

                # weekly (RDS composite key "<venue_id>#<day_int>"; Redis key per day)
                stage = "weekly"
                present_days = weekly_map.get(venue_id, {})
                for day_int, wk in present_days.items():
                    self.redis_only_dao.set_week_raw_forecast(
                        venue_id, WeekRawDay.model_validate(wk["payload"])
                    )
                for day_int in _WEEK_DAYS:
                    if day_int not in present_days:
                        if self.redis_only_dao.delete_week_raw_forecast(venue_id, day_int):
                            REDIS_PROJECTION_ENTITY_DELETES_TOTAL.labels(entity="weekly").inc()

                stage = "live"
                live = live_map.get(venue_id)
                if live is not None:
                    self.redis_only_dao.set_live_forecast(
                        LiveForecastResponse.model_validate(live["payload"])
                    )
                    summary["live"] += 1
                elif self.redis_only_dao.delete_live_forecast(venue_id):
                    REDIS_PROJECTION_ENTITY_DELETES_TOTAL.labels(entity="live").inc()
            except Exception as e:
                summary["errors"] += 1
                summary["error_venues"].append(venue_id)
                logger.warning(f"[Rebuild] venue {venue_id} failed at stage={stage}: {e}")
                continue
        REDIS_PROJECTION_VENUES.set(summary["venues"])
        # Reconcile: remove from Redis any venue that has an RDS row but is not in
        # the serving view — deprecated OR active-but-ineligible. Editing the
        # block-list thus removes/restores venues here, both directions, with no
        # lifecycle change. Orphans (no RDS row at all) are absence-of-signal and
        # left untouched (partial-read safe); a failed listing skips removal rather
        # than risk a bad delete.
        try:
            rds_known = set(self.rds_store.list_active_venue_ids()) | set(
                self.rds_store.list_deprecated_venue_ids()
            )
        except Exception as e:
            logger.warning(f"[Rebuild] reconcile listing failed; skipping removal: {e}")
            rds_known = set()
        for venue_id in rds_known - servable_set:
            if self.redis_only_dao.delete_venue(venue_id):
                summary["removed"] += 1
                REDIS_PROJECTION_REMOVED_TOTAL.inc()
        # Self-heal the eligibility serving mirror from its rows. Delegated to the
        # carve-out owner and isolated — rehydrate_mirror is already degrade-safe,
        # but guard anyway so it can never abort the venue projection.
        if self.eligibility_rule_service is not None:
            try:
                self.eligibility_rule_service.rehydrate_mirror()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[Rebuild] eligibility mirror rehydration error: {e}")
        logger.info(f"[Rebuild] {summary}")
        return summary

    def _project_photos(self, venue_id: str, rec: Optional[dict]) -> None:
        """B2: project photos with the REMAINING TTL (full − age) so repeated
        runs count the TTL down instead of re-stamping a fresh full TTL; drop
        photos aged past the TTL so stale Google URLs leave serving and the
        refetch trigger fires.

        `rec` is the prefetched (already deleted_at-IS-NULL-filtered) photos row
        for this venue from the bulk enrichment map, or None when absent — the
        same "absent" signal the single-row reader's `not rec or rec.get(...)
        is not None` gate produced."""
        if not rec:
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
