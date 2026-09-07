"""Rebuild the Redis serving projection from RDS.

rebuild_redis_from_rds(): RDS -> Redis projection for every active venue,
INCLUDING the geo index (via redis_only_dao.upsert_venue -> GEOADD) and live
busyness. This is the scheduled projector body (and manual disaster recovery /
Redis warm). Photos are projected with their remaining TTL so expired Google
URLs refetch instead of serving stale.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.dao.venue_row import venue_from_row
from app.metrics import (
    DEEP_REVIEWS_PROJECTED_VENUES,
    EVENT_FLYER_BYTES,
    EVENT_FLYER_OBJECTS,
    EVENTS_PROJECTED_OCCURRENCES,
    EVENTS_PROJECTION_BYTES,
    EVENTS_PROJECTION_DURATION_SECONDS,
    EVENTS_PROJECTION_ERRORS_TOTAL,
    EVENTS_PROJECTION_NIGHTLIFE_ROLLBACK_TOTAL,
    EVENTS_PROJECTION_SOURCE_ROWS,
    EVENTS_PROJECTION_TICKET_URL_TOTAL,
    REDIS_PROJECTION_ENTITY_DELETES_TOTAL,
    REDIS_PROJECTION_REMOVED_TOTAL,
    REDIS_PROJECTION_VENUES,
    SERVING_VIEW_VENUES,
    VENUES_GEO_EXCLUDED,
    VENUE_PROFILE_PHOTO_EDGE_COLOR_VENUES,
    VENUE_PROFILE_PHOTO_PROJECTED_VENUES,
)
from app.models import (
    LiveForecastResponse,
    WeekRawDay,
)
from app.models.event_occurrence import EventOccurrence
from app.models.promoter_event_visibility import is_promoter_only_item, load_hide_promoter_events
from app.services.event_city_slug import nearest_city_slug
from app.services.event_date_resolver import RECIFE_TZ
from app.services.event_occurrences import expand_occurrences, nightlife_date
from app.services.event_ticket_url import classify_ticket_url
from app.models.vibe_attributes import VibeAttributes
from app.models.opening_hours import OpeningHours
from app.models.instagram import (
    VenueInstagram,
    VenueInstagramPosts,
    VenueInstagramProfilePhoto,
)
from app.models.menu import VenueMenuData, VenueMenuPhotos
from app.models.venue_review import VenueReviews, VenueReviewsDeep
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
    # The venue-list hero. Registry entry only, deliberately: the deleter below
    # is what guarantees a soft-deleted RDS row cannot leave a `venue_profile_
    # photo_v1` key pointing at an object nobody meant to serve any more. The
    # setter writes without a TTL (RedisVenueDAO.set_venue_profile_photo) — this
    # projection is re-asserted every cycle, so the row is the lifetime, not a
    # clock.
    "instagram.profile_photo": (
        VenueInstagramProfilePhoto, "set_venue_profile_photo", "delete_venue_profile_photo",
    ),
    "venues.menu_photos": (VenueMenuPhotos, "set_venue_menu_photos", "delete_venue_menu_photos"),
    "venues.menu_data": (VenueMenuData, "set_venue_menu_data", "delete_venue_menu_data"),
    "venues.vibe_profile": (VenueVibeProfile, "set_venue_vibe_profile", "delete_venue_vibe_profile"),
    # The setter itself bounds what actually reaches Redis to the newest
    # `settings.reviews_deep_projection_max` (RedisVenueDAO.set_venue_reviews_
    # deep) — this entry follows the exact same call/delete shape every other
    # family here does; only that one setter's INTERNAL behavior differs.
    "venues.reviews_deep": (VenueReviewsDeep, "set_venue_reviews_deep", "delete_venue_reviews_deep"),
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
        # Optional, wired post-construction by the container exactly like
        # eligibility_rule_service above (plans/260905_events-serving-
        # projection.md): the flyer copier (app.services.event_flyer_
        # service.EventFlyerService). None when the media bucket/CDN is not
        # configured — project_events() then simply never copies a flyer,
        # never a reason to fail the cycle.
        self.event_flyer_service = None

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
        # cycle (P1): 1 venue-rows query + 11 enrichment-table queries (the 10
        # _REBUILD_MODELS tables, incl. venues.reviews_deep and
        # instagram.profile_photo, + photos) + 1 weekly query + 1 live query =
        # 14 bulk reads total, independent of how many servable venues exist —
        # replacing what was ~18 SQL queries PER VENUE. The count tracks the
        # number of FAMILIES (a new family adds exactly one read); what must
        # never grow is the number of VENUES, which is what
        # tests/bdd/persistence/projector-and-serving-bulk-reads.feature pins.
        # The per-venue projection logic below is unchanged; only the source of
        # each row/rec moves from a per-call SELECT to a dict lookup on these
        # prefetched maps.
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
        # Deep-review-corpus-specific observability (plan Error Handling And
        # Observability): a gauge of how many venues carry a projected slice
        # this cycle, plus the total bytes actually written — the projection
        # slice cap is the one place this feature could hurt production
        # Redis, so its size is logged from the same place the risk lives.
        deep_reviews_projected = 0
        deep_reviews_bytes = 0
        # Same reasoning one family over: how many venues actually carry a hero
        # right now is the coverage number this feature lives or dies by, and
        # "the job is behind" has to be visible as a gauge rather than inferred
        # from a wall of missing thumbnails in the app.
        profile_photos_projected = 0
        # Coverage of the additive `edge_color`. Counted here, in the projector,
        # because this is the only place that sees the exact set the app will
        # be served from — the backfill job's own counters say what one run did,
        # not what the catalog currently holds.
        profile_photos_with_edge_color = 0

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
                        result = getattr(self.redis_only_dao, setter)(obj)
                        summary["enrichment"] += 1
                        if table_key == "venues.reviews_deep":
                            deep_reviews_projected += 1
                            if isinstance(result, int):
                                deep_reviews_bytes += result
                        elif table_key == "instagram.profile_photo":
                            profile_photos_projected += 1
                            if getattr(obj, "edge_color", None):
                                profile_photos_with_edge_color += 1
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
        DEEP_REVIEWS_PROJECTED_VENUES.set(deep_reviews_projected)
        VENUE_PROFILE_PHOTO_PROJECTED_VENUES.set(profile_photos_projected)
        VENUE_PROFILE_PHOTO_EDGE_COLOR_VENUES.set(profile_photos_with_edge_color)
        summary["profile_photos"] = profile_photos_projected
        summary["profile_photos_with_edge_color"] = profile_photos_with_edge_color
        if deep_reviews_projected:
            logger.info(
                f"[Rebuild] deep review projection: {deep_reviews_projected} venues, "
                f"{deep_reviews_bytes} bytes"
            )
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

    # ── events serving projection (plans/260905_events-serving-projection.md) ─
    # A SIBLING pass, never a _REBUILD_MODELS entry: rebuild_redis_from_rds's
    # contract is one venue-keyed record per venue; the unit here is an
    # occurrence. Isolation is structural, not just a try/except: this method
    # runs to completion (or aborts) entirely on its own Redis key family
    # (event_occurrence_v1:*, events_index_v1:*, events_venue_v1:*) — it
    # never calls any venue-projection setter, so nothing it does can
    # corrupt or partially roll back what rebuild_redis_from_rds already
    # wrote. Callers (main.py) additionally wrap the call itself so an
    # escaping exception here can never fail the shared scheduled job.
    def project_events(self, *, now: Optional[datetime] = None) -> dict:
        """`now` defaults to the real wall clock; overridable for
        deterministic tests (mirrors event_date_resolver.resolve_event_
        datetime's own "never reads the wall clock internally, the caller
        supplies it" discipline, one level up)."""
        summary = {
            "occurrences": 0, "source_rows": 0, "bytes": 0, "errors": 0,
            "error_events": [], "flyer": {},
        }
        if not settings.events_projection_enabled:
            return summary

        started = time.perf_counter()
        now = now or datetime.now(timezone.utc)

        # One cheap, unambiguous production signal that the nightlife-day
        # rollback is actually live in the six hours where it matters: an
        # absent or flat series after a night in production means the deploy
        # did not take. Bumped once per CYCLE, never per event. The naive
        # coercion mirrors `event_occurrences._as_aware_utc`, so a fixture
        # clock and the real wall clock are compared the same way
        # `nightlife_date` compares them.
        now_aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if nightlife_date(now_aware) != now_aware.astimezone(RECIFE_TZ).date():
            EVENTS_PROJECTION_NIGHTLIFE_ROLLBACK_TOTAL.inc()

        # ── preparation: selection + geo-fence + address join ────────────
        # All-or-nothing, mirroring rebuild_redis_from_rds's own fail-safe
        # posture on a serving-view read failure: an empty/partial read here
        # must never be mistaken for "there are no events", so any failure
        # aborts the WHOLE cycle before a single Redis key is touched.
        try:
            rows = self.rds_store.list_events_for_projection(now=now)
            fence = self.rds_store.get_geo_fence()
            cities = fence.get("cities") or []
            venue_ids = sorted({r["venue_id"] for r in rows if r.get("venue_id")})
            address_by_venue = self.rds_store.get_address_bulk(venue_ids)
            hide_promoter, _ = load_hide_promoter_events(self.redis_only_dao.client)
            sources_by_event: dict[str, list[dict]] = {}
            if hide_promoter:
                selected_event_ids = sorted({
                    r["event_id"] for r in rows if r.get("event_id")
                })
                for source in self.rds_store.list_event_sources_bulk(selected_event_ids):
                    sources_by_event.setdefault(source["event_id"], []).append(source)
        except Exception as e:
            logger.error(
                f"[EventsProjection] preparation read failed; aborting cycle, "
                f"Redis left intact: {e}"
            )
            summary["errors"] += 1
            EVENTS_PROJECTION_ERRORS_TOTAL.labels(stage="selection").inc()
            return summary

        if hide_promoter:
            rows = [
                r for r in rows
                if not is_promoter_only_item(sources_by_event.get(r["event_id"], []))
            ]
        summary["source_rows"] = len(rows)
        EVENTS_PROJECTION_SOURCE_ROWS.set(len(rows))

        # ── the events city vocabulary ───────────────────────────────────
        # `events_known_cities_v1` is what vibes_bot validates an incoming
        # `city` param against, and until now its ONLY writer was
        # `index_event_occurrence` — so a configured city holding zero
        # occurrences was simply not in the set, and validating against it
        # would 422 a real-but-quiet city instead of serving 200-empty.
        # Remembering every CONFIGURED fence slug every cycle makes the set
        # a complete, enumerable vocabulary, and puts a newly-added city in
        # it BEFORE its first occurrence is indexed. Best-effort, exactly
        # like every other optional read/write in this method: the set is an
        # optimisation for the prune and a vocabulary for downstream, never
        # a reason to lose a cycle.
        try:
            for city in cities:
                self.redis_only_dao.remember_city_slug(city["slug"])
        except Exception as e:
            logger.warning(
                f"[EventsProjection] remembering configured city slugs failed; "
                f"continuing this cycle: {e}"
            )

        # ── flyer copy (Phase 1) — isolated, never aborts the cycle ──────
        flyer_summary: dict = {}
        if self.event_flyer_service is not None:
            try:
                flyer_summary = asyncio.run(
                    self.event_flyer_service.copy_flyers(
                        rows, max_per_cycle=settings.event_flyer_copy_max_per_cycle,
                    )
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.error(
                    f"[EventsProjection] flyer copy pass failed, continuing "
                    f"without fresh flyer urls this cycle: {e}"
                )
        flyer_urls = flyer_summary.get("flyer_urls", {})
        summary["flyer"] = {
            k: v for k, v in flyer_summary.items() if k != "flyer_urls"
        }

        # ── per-event expansion + payload construction ───────────────────
        # Isolated per event: a single bad row (a Pydantic validation
        # failure, a city_slug that cannot be derived) degrades to "this
        # event waits for next cycle" instead of losing every other event
        # in the same run.
        fresh_by_venue: dict[str, dict[str, float]] = {}
        fresh_by_city: dict[str, dict[str, float]] = {}
        occurrences: list[EventOccurrence] = []
        error_events: list[str] = []
        for row in rows:
            event_id = row.get("event_id")
            try:
                venue_id = row["venue_id"]
                addr = address_by_venue.get(venue_id) or {}
                lat, lng = addr.get("lat"), addr.get("lng")
                city_slug = nearest_city_slug(lat, lng, cities)
                if city_slug is None:
                    raise ValueError(
                        "no geo-fence city configured; city_slug cannot be derived"
                    )
                flyer_url = flyer_urls.get(event_id, row.get("flyer_url"))
                # ONCE PER SOURCE ROW, deliberately outside the occurrence
                # loop below: `classify_ticket_url` is a pure function of
                # the row, so calling it per occurrence would recompute an
                # identical answer up to 22 times (23 while the nightlife
                # day is rolled back) AND scale the counter by recurrence
                # multiplicity — one badly-extracted recurring row would
                # read as 22 rejections. Same shape as `flyer_url` above:
                # the projection substitutes a serving-shaped value over
                # the RDS one, and RDS keeps the verbatim extraction.
                ticket_url, ticket_outcome = classify_ticket_url(row.get("ticket_url"))
                EVENTS_PROJECTION_TICKET_URL_TOTAL.labels(outcome=ticket_outcome).inc()

                for occ in expand_occurrences(
                    row,
                    horizon_days=settings.events_projection_horizon_days,
                    reference_time=now,
                ):
                    score = occ.starts_at.timestamp()
                    payload = EventOccurrence(
                        occurrence_id=occ.occurrence_id,
                        event_id=event_id,
                        occurrence_date=occ.occurrence_date,
                        starts_at=occ.starts_at,
                        ends_at=row.get("ends_at"),
                        time_known=bool(row.get("time_known")),
                        is_recurring=bool(row.get("is_recurring")),
                        recurrence_text=row.get("recurrence_text"),
                        title=row.get("title"),
                        description=row.get("description"),
                        category=row.get("category"),
                        price_text=row.get("price_text"),
                        ticket_info=row.get("ticket_info"),
                        ticket_url=ticket_url,
                        lineup=row.get("lineup"),
                        attractions=row.get("attractions"),
                        flyer_url=flyer_url,
                        venue_id=venue_id,
                        venue_name=row.get("venue_name"),
                        venue_neighborhood=addr.get("neighborhood"),
                        venue_lat=lat,
                        venue_lng=lng,
                        source_permalink=row.get("source_permalink"),
                        source_handle=row.get("source_handle"),
                        city_slug=city_slug,
                        status=row.get("status"),
                        updated_at=row.get("updated_at"),
                    )
                    occurrences.append(payload)
                    fresh_by_venue.setdefault(venue_id, {})[occ.occurrence_id] = score
                    fresh_by_city.setdefault(city_slug, {})[occ.occurrence_id] = score
            except Exception as e:
                summary["errors"] += 1
                error_events.append(event_id)
                EVENTS_PROJECTION_ERRORS_TOTAL.labels(stage="event").inc()
                logger.warning(f"[EventsProjection] event {event_id} failed: {e}")
                continue
        summary["error_events"] = error_events

        # ── write pass ────────────────────────────────────────────────────
        total_bytes = 0
        for payload in occurrences:
            self.redis_only_dao.set_event_occurrence(payload)
            score = fresh_by_venue[payload.venue_id][payload.occurrence_id]
            self.redis_only_dao.index_event_occurrence(
                city_slug=payload.city_slug, venue_id=payload.venue_id,
                occurrence_id=payload.occurrence_id, score=score,
            )
            total_bytes += len(payload.model_dump_json(by_alias=True).encode("utf-8"))

        # ── re-assert and prune — enumerable id spaces, never KEYS/SCAN ──
        # Mirrors rebuild_redis_from_rds's own reconcile-listing fail-safe:
        # a failed venue-id listing SKIPS the prune (logs + moves on) rather
        # than risking a bad delete, exactly like that method's own
        # `rds_known = set()` fallback.
        removed_ids: set[str] = set()
        try:
            rds_known = set(self.rds_store.list_active_venue_ids()) | set(
                self.rds_store.list_deprecated_venue_ids()
            )
        except Exception as e:
            logger.warning(
                f"[EventsProjection] venue-id listing failed; skipping "
                f"venue-index prune: {e}"
            )
            rds_known = set()
        for venue_id in rds_known:
            current = set(self.redis_only_dao.get_venue_events_index(venue_id))
            fresh = set(fresh_by_venue.get(venue_id, {}).keys())
            for occ_id in current - fresh:
                self.redis_only_dao.remove_from_venue_events_index(venue_id, occ_id)
                removed_ids.add(occ_id)
        # City slugs to visit: the currently-configured set UNION every slug
        # ever written (the durable `events_known_cities_v1` set —
        # `remember_city_slug`'s docstring). `admin.geo_fence_city` alone is
        # NOT durable: `set_geo_fence` does a literal DELETE with no
        # history, so a city removed from the fence would otherwise vanish
        # from `cities` on this very cycle and never be visited (or
        # emptied) again — the city-side analogue of `rds_known` above. A
        # failed known-slugs read degrades to pruning only the currently
        # configured cities this cycle (never risking a bad delete, and
        # `cities` itself is already known-good from the preparation step
        # above), rather than skipping the whole city prune.
        try:
            known_slugs = set(self.redis_only_dao.list_known_city_slugs())
        except Exception as e:
            logger.warning(
                f"[EventsProjection] known-city-slug read failed; pruning "
                f"only currently configured cities this cycle: {e}"
            )
            known_slugs = set()
        configured_slugs = {city["slug"] for city in cities}
        for slug in configured_slugs | known_slugs:
            current = set(self.redis_only_dao.get_city_events_index(slug))
            fresh = set(fresh_by_city.get(slug, {}).keys())
            for occ_id in current - fresh:
                self.redis_only_dao.remove_from_city_events_index(slug, occ_id)
                removed_ids.add(occ_id)
        for occ_id in removed_ids:
            self.redis_only_dao.delete_event_occurrence(occ_id)

        summary["occurrences"] = len(occurrences)
        summary["bytes"] = total_bytes
        EVENTS_PROJECTED_OCCURRENCES.set(len(occurrences))
        EVENTS_PROJECTION_BYTES.set(total_bytes)

        # Flyer retention gauges (Phase 1) — computed fresh from RDS every
        # cycle, best-effort: a failure here must never undo the write pass
        # above.
        try:
            obj_count, obj_bytes = self.rds_store.count_event_flyers()
            EVENT_FLYER_OBJECTS.set(obj_count)
            EVENT_FLYER_BYTES.set(obj_bytes)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[EventsProjection] flyer gauge read failed: {e}")

        EVENTS_PROJECTION_DURATION_SECONDS.observe(time.perf_counter() - started)
        logger.info(f"[EventsProjection] {summary}")
        return summary
