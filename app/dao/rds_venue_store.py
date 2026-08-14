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
from app.services.pipeline_run_registry import new_run_id

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
                                       ["google_primary_type", "google_place_id",
                                        "primary_type_locked"]),
    "google_places.opening_hours": ("google_places", "opening_hours", []),
    "google_places.photos": ("google_places", "photos", []),
    "google_places.reviews": ("google_places", "reviews", []),
    "instagram.handle": ("instagram", "handle", ["instagram_handle", "source"]),
    "instagram.posts": ("instagram", "posts", []),
    "venues.menu_photos": ("venues", "menu_photos", []),
    "venues.menu_data": ("venues", "menu_data", []),
    "venues.vibe_profile": ("venues", "vibe_profile", []),
    "venues.reviews_deep": ("venues", "reviews_deep", []),
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
    "v.deprecated_source, v.google_business_status, v.venue_source, "
    "v.created_at, v.extra "
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
                "deprecated_source, deprecated_at, google_business_status, venue_source, "
                "extra, updated_at) "
                "VALUES (:venue_id, :venue_name, "
                ":venue_type, :price_level, CAST(:price_range AS jsonb), :google_price_level, "
                ":besttime_price_level, :price_level_source, :rating, :reviews, :priority, "
                ":forecast, :processed, "
                ":lifecycle_status, :deprecated_reason, :deprecated_source, :deprecated_at, "
                ":google_business_status, :venue_source, CAST(:extra AS jsonb), now()) "
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
                "venue_source=excluded.venue_source, "
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
                "venue_source": venue.venue_source,
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
        A non-positive limit selects nothing (strictly honours a zero budget).

        Excludes venue_source='google_only': those venues carry no BestTime id
        to query, so feeding one to BestTime refresh would be a bug, not just a
        wasted credit. See plans/260804_add-venue-google-only.md."""
        if limit <= 0:
            return []
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT venue_id FROM venues.venue "
                "WHERE lifecycle_status='active' AND venue_source <> 'google_only' "
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

    # ── admin.venue_closure_signal ────────────────────────────────────────────
    # Closure is evidence-derived and reversible: these rows gate
    # serving.eligible_venue without touching lifecycle_status, so a reopened
    # venue returns on its own once the detector clears its signal.
    def set_closure_signal(self, venue_id: str, signal: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO admin.venue_closure_signal "
                    "(venue_id, closed, reason, confidence, evidence_publish_time, "
                    " matched_phrase, detected_at, updated_at) "
                    "VALUES (:venue_id, :closed, :reason, :confidence, "
                    "        :evidence_publish_time, :matched_phrase, now(), now()) "
                    "ON CONFLICT (venue_id) DO UPDATE SET "
                    "  closed = EXCLUDED.closed, "
                    "  reason = EXCLUDED.reason, "
                    "  confidence = EXCLUDED.confidence, "
                    "  evidence_publish_time = EXCLUDED.evidence_publish_time, "
                    "  matched_phrase = EXCLUDED.matched_phrase, "
                    "  updated_at = now()"
                ),
                {
                    "venue_id": venue_id,
                    "closed": bool(signal.get("closed")),
                    "reason": signal.get("reason"),
                    "confidence": signal.get("confidence", "high"),
                    "evidence_publish_time": signal.get("evidence_publish_time"),
                    "matched_phrase": signal.get("matched_phrase"),
                },
            )

    def clear_closure_signal(self, venue_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM admin.venue_closure_signal WHERE venue_id = :venue_id"),
                {"venue_id": venue_id},
            )

    def get_closure_signal(self, venue_id: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT venue_id, closed, reason, confidence, "
                    "       evidence_publish_time, matched_phrase "
                    "FROM admin.venue_closure_signal WHERE venue_id = :venue_id"
                ),
                {"venue_id": venue_id},
            ).mappings().first()
            return dict(row) if row else None

    def list_closure_signals(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT venue_id, closed, reason, confidence, "
                        "       evidence_publish_time, matched_phrase "
                        "FROM admin.venue_closure_signal ORDER BY venue_id"
                    )
                ).mappings()
            ]

    def list_servable_venue_ids_by_priority(self, limit: int) -> list[str]:
        """The top-`limit` servable venues (the eligibility serving view) ordered
        by refresh priority ascending, tie-broken by reviews desc, rating desc,
        then venue_id — the served-scoped counterpart of
        list_active_venue_ids_by_priority that backs the bounded live/weekly
        refresh. serving.eligible_venue carries no priority column, so it is joined
        to venues.venue for the ordering keys. A non-positive limit selects
        nothing (mirrors the active variant).

        Excludes venue_source='google_only' — see list_active_venue_ids_by_priority;
        these venues stay servable (a google_only venue IS eligible for serving
        via serving.eligible_venue itself), only bounded refresh selection skips
        them."""
        if limit <= 0:
            return []
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT ev.venue_id FROM serving.eligible_venue ev "
                "JOIN venues.venue v ON v.venue_id = ev.venue_id "
                "WHERE v.venue_source <> 'google_only' "
                "ORDER BY v.priority ASC, v.reviews DESC NULLS LAST, "
                "v.rating DESC NULLS LAST, v.venue_id ASC LIMIT :limit"
            ), {"limit": limit})]

    def list_event_candidate_ids_by_priority(self, limit: int) -> list[str]:
        """The top-`limit` servable venues ordered by refresh priority, WITHOUT
        the `venue_source <> 'google_only'` filter that
        list_servable_venue_ids_by_priority applies for BestTime refresh.

        A google_only venue is one BestTime could not forecast — in this
        catalog that skews toward the small independent club and event space,
        precisely the venue most likely to run events. Reusing the BestTime-
        scoped selection here would silently drop exactly those venues (a
        shorter list, not an error), so this is a separate method rather than a
        flag on the existing one: a boolean someone can pass wrong is a worse
        guard than two functions that cannot be confused. See
        plans/260804_event-venue-targeting.md.
        """
        if limit <= 0:
            return []
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT ev.venue_id FROM serving.eligible_venue ev "
                "JOIN venues.venue v ON v.venue_id = ev.venue_id "
                "ORDER BY v.priority ASC, v.reviews DESC NULLS LAST, "
                "v.rating DESC NULLS LAST, v.venue_id ASC LIMIT :limit"
            ), {"limit": limit})]

    # ── events.venue_event_profile (event venue targeting verdict) ───────────
    def upsert_venue_event_profile(
        self, venue_id, *, tier, category_pass, category_reason=None,
        evidence_score=None, evidence_sample=None, evaluated_at=None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO events.venue_event_profile "
                "(venue_id, tier, category_pass, category_reason, evidence_score, "
                " evidence_sample, evaluated_at, updated_at) "
                "VALUES (:venue_id, :tier, :category_pass, :category_reason, "
                " :evidence_score, CAST(:evidence_sample AS jsonb), :evaluated_at, now()) "
                "ON CONFLICT (venue_id) DO UPDATE SET "
                "tier=excluded.tier, category_pass=excluded.category_pass, "
                "category_reason=excluded.category_reason, "
                "evidence_score=excluded.evidence_score, "
                "evidence_sample=excluded.evidence_sample, "
                "evaluated_at=excluded.evaluated_at, updated_at=now()"
            ), {
                "venue_id": venue_id, "tier": tier, "category_pass": category_pass,
                "category_reason": category_reason, "evidence_score": evidence_score,
                "evidence_sample": (
                    json.dumps(evidence_sample) if evidence_sample is not None else None
                ),
                "evaluated_at": evaluated_at,
            })

    def get_venue_event_profile(self, venue_id) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT venue_id, tier, category_pass, category_reason, "
                "evidence_score, evidence_sample, evaluated_at, updated_at "
                "FROM events.venue_event_profile WHERE venue_id=:v"
            ), {"v": venue_id}).mappings().first()
            return dict(row) if row else None

    def list_venue_event_profiles(self, tiers: Optional[list[str]] = None) -> list[dict]:
        """Every venue_event_profile row, joined to venue_source for the
        tier x venue_source gauge — optionally filtered to a tier subset (the
        `event_candidates` eligibility mode resolution)."""
        sql = (
            "SELECT p.venue_id, p.tier, p.category_pass, p.category_reason, "
            "p.evidence_score, p.evidence_sample, p.evaluated_at, p.updated_at, "
            "v.venue_source FROM events.venue_event_profile p "
            "JOIN venues.venue v ON v.venue_id = p.venue_id"
        )
        params: dict = {}
        if tiers:
            sql += " WHERE p.tier IN :tiers"
            stmt = text(sql).bindparams(bindparam("tiers", expanding=True))
            params["tiers"] = list(tiers)
        else:
            stmt = text(sql)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(stmt, params).mappings()]

    def list_all_venue_rows(self) -> list[dict]:
        """Every venue row (active + deprecated) with scalar columns + residual
        `extra` + address (from venues.address) — backs the pipeline
        list_all_venues RDS read (column-based reconstruction) and the
        redis↔rds serving diff."""
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(_VENUE_SELECT)).mappings()]

    # ── events.post_item / events.post_item_source (plans/260804_instagram-
    # event-extraction.md, restructured by plans/260807_one-event-many-
    # posts.md, renamed by plans/260811_post-items-and-categories.md — the
    # tables were `events.event`/`events.event_source` before that plan;
    # see migration 0034_post_items for the ALTER TABLE RENAME and its
    # verified-against-real-Postgres constraint/index renames) ──────────────
    # `events.post_item` holds MERGED fields only; per-post provenance
    # (source_kind/handle/shortcode/permalink/source_event_key/
    # source_event_index/cover_photo_key/raw_extraction/first_seen_at/
    # last_seen_at) lives in `events.post_item_source`, one row per
    # announcing post, several of which can now share one `post_item_id`
    # (migration 0026).
    #
    # The PYTHON-FACING dict key stays "event_id" (never "post_item_id")
    # deliberately — plans/260811_post-items-and-categories.md keeps every
    # response field name stable in this PR even where the table renamed
    # (the console is updated separately), and this DAO is the ONE seam
    # that translates: every SELECT below aliases the real
    # `post_item_id` column back to `event_id`, and `_EVENT_SQL_COLUMN`
    # translates it the other way for INSERT/UPDATE/DELETE. No caller above
    # this module (services, router, tests) ever sees `post_item_id`.
    _EVENT_COLUMNS = (
        "event_id", "venue_id", "starts_at", "ends_at", "is_recurring",
        "recurrence_text", "title", "description", "lineup", "ticket_url",
        "price_text", "location_text", "confidence", "status", "review_reason",
        # plans/260804_instagram-promoter-events.md (migration 0024) — how, if
        # at all, a promoter event's venue was resolved.
        "location_resolution", "location_confidence", "linked_by", "linked_at",
        # plans/260807_auto-accept-and-field-level-protection.md (migration
        # 0027) — the union of field names an operator has PATCHed; NULL
        # means unknown (a legacy row, or one confirmed without ever being
        # PATCHed) and is never invented by anything other than a real PATCH.
        "operator_edited_fields",
        # plans/260808_event-ticket-info-and-attractions.md (migration 0028)
        # — ticket_info is an ordinary scalar (merged like price_text);
        # attractions is a jsonb list, additive across posts like lineup.
        "ticket_info", "attractions",
        # plans/260811_post-items-and-categories.md (migration 0034) —
        # post_type is what a post yielded (event/promotion/menu/food/other,
        # or an unrecognised value stored verbatim); category is free text
        # steered by an admin-configurable vocabulary. Both operator-
        # protectable via operator_edited_fields, like every other scalar
        # here.
        "post_type", "category",
        # plans/260811_expose-time-known.md (migration 0035) — whether
        # starts_at's clock time was actually read, as opposed to a
        # defaulted midnight. A plain SQL column like any other here, but
        # deliberately NOT in app.services.event_reconciliation.
        # PROTECTABLE_EVENT_FIELDS — it is metadata about starts_at, not
        # independent content, and travels WITH whichever starts_at value
        # a merge/re-extraction decision lands on (see that module's
        # _confirmed_update_fields docstring).
        "time_known",
        # plans/260812_event-dedup-fuzzy-title.md §E (migration 0038) — the
        # event that absorbed THIS row via title/lineup similarity, when its
        # status is 'superseded' for that reason. NULL for every row an
        # exact-identity merge deleted instead (that path never sets this —
        # the row is simply gone) and for every row that has never been
        # absorbed at all.
        "superseded_by",
    )
    _EVENT_JSONB_COLUMNS = ("lineup", "operator_edited_fields", "attractions")
    # Python dict key -> real SQL column name, for the one column whose
    # physical name (`post_item_id`, migration 0034) differs from the
    # Python-facing key this module and every caller above it still use
    # (`event_id` — see the module comment above). Every other column name
    # is identical on both sides.
    _EVENT_SQL_COLUMN = {"event_id": "post_item_id"}

    @classmethod
    def _event_sql_col(cls, python_key: str) -> str:
        return cls._EVENT_SQL_COLUMN.get(python_key, python_key)

    # The per-source columns `insert_event`/`update_event` route to
    # events.post_item_source instead of events.post_item — see the
    # module-level split in tests.rds_fake.InMemoryRdsVenueStore, which
    # this mirrors.
    _EVENT_SOURCE_FIELDS = (
        "source_kind", "source_handle", "source_shortcode", "source_permalink",
        "source_event_key", "source_event_index", "cover_photo_key",
        "raw_extraction", "first_seen_at", "last_seen_at",
        # plans/260812_crawl-error-visibility.md §C/§D (migration 0036):
        # Apify's own media type ("Video"/"Image"/"Sidecar" — NOT
        # events.post_item.post_type, an unrelated event/promotion/menu
        # classification) and the post's own upload timestamp (distinct from
        # first_seen_at/last_seen_at above, which are CRAWL times), plus
        # whether the per-post event cap (§D) dropped trailing entries from
        # this post's own extraction.
        "source_media_type", "source_uploaded_at", "source_events_truncated",
        # plans/260812_event-attribution-and-dates.md §C (migration 0037):
        # the model's structured date-interpretation fallback, persisted
        # NEXT TO raw_extraction (its own column, not nested inside that
        # blob) — the determinism guard's own store, read back by
        # `list_events_by_source` before a re-extraction resolves a date.
        "date_interpretation",
    )
    _EVENT_SOURCE_JSONB_COLUMNS = ("raw_extraction", "date_interpretation")

    # plans/260807_review-queue-completeness-and-venue-names.md: LEFT JOIN,
    # never INNER — an unresolved promoter event has `venue_id IS NULL` (or a
    # venue_id with no matching row) and must still be selected; an INNER
    # join would silently drop exactly the rows the review queue exists for.
    # Every `events.post_item` column is qualified `e.` because the join
    # makes `venue_id` ambiguous otherwise; `v.venue_name` is the only column
    # pulled from venues.venue. `ps` (primary source) is a LATERAL picking
    # the MOST RECENTLY SEEN source per event — the four legacy scalars
    # (source_kind/handle/shortcode/permalink/cover_photo_key... the first
    # four of which the admin console reads today) are derived from it, so
    # the console keeps working unchanged across a merge
    # (plans/260807_one-event-many-posts.md's compatibility guarantee). `agg`
    # aggregates first_seen_at/last_seen_at across EVERY source (earliest
    # first-seen, latest last-seen) — the columns leave events.post_item, the
    # response shape does not.
    #
    # `e.post_item_id AS event_id` is the one seam that keeps the Python-
    # facing dict key `event_id` stable across migration 0034's physical
    # rename — see the `_EVENT_SQL_COLUMN` comment above.
    _EVENT_SELECT = (
        "SELECT e.post_item_id AS event_id, e.venue_id, e.starts_at, e.ends_at, e.is_recurring, "
        "e.recurrence_text, e.title, e.description, e.lineup, e.ticket_url, "
        "e.price_text, e.location_text, e.confidence, e.status, e.review_reason, "
        "e.location_resolution, e.location_confidence, e.linked_by, e.linked_at, "
        "e.operator_edited_fields, e.ticket_info, e.attractions, "
        "e.post_type, e.category, e.time_known, e.superseded_by, "
        "e.updated_at, v.venue_name, "
        "ps.source_kind, ps.source_handle, ps.source_shortcode, ps.source_permalink, "
        "ps.source_event_key, ps.source_event_index, ps.cover_photo_key, ps.raw_extraction, "
        "ps.source_media_type, ps.source_uploaded_at, ps.source_events_truncated, "
        "ps.date_interpretation, "
        "agg.first_seen_at, agg.last_seen_at "
        "FROM events.post_item e "
        "LEFT JOIN venues.venue v ON v.venue_id = e.venue_id "
        "LEFT JOIN LATERAL ("
        "  SELECT source_kind, source_handle, source_shortcode, source_permalink, "
        "         source_event_key, source_event_index, cover_photo_key, raw_extraction, "
        "         source_media_type, source_uploaded_at, source_events_truncated, "
        "         date_interpretation "
        "  FROM events.post_item_source es WHERE es.post_item_id = e.post_item_id "
        "  ORDER BY es.last_seen_at DESC, es.id DESC LIMIT 1"
        ") ps ON true "
        "LEFT JOIN LATERAL ("
        "  SELECT min(first_seen_at) AS first_seen_at, max(last_seen_at) AS last_seen_at "
        "  FROM events.post_item_source es2 WHERE es2.post_item_id = e.post_item_id"
        ") agg ON true"
    )

    # Per-SOURCE projection for list_events_by_source: reconciliation needs
    # THIS post's own source_event_key/raw_extraction/timestamps, never the
    # merged event's primary/aggregate values (which could belong to a
    # DIFFERENT source once several posts share one event).
    _EVENT_SOURCE_SELECT = (
        "SELECT e.post_item_id AS event_id, e.venue_id, e.starts_at, e.ends_at, e.is_recurring, "
        "e.recurrence_text, e.title, e.description, e.lineup, e.ticket_url, "
        "e.price_text, e.location_text, e.confidence, e.status, e.review_reason, "
        "e.location_resolution, e.location_confidence, e.linked_by, e.linked_at, "
        "e.operator_edited_fields, e.ticket_info, e.attractions, "
        "e.post_type, e.category, e.time_known, e.superseded_by, "
        "e.updated_at, v.venue_name, "
        "es.source_kind, es.source_handle, es.source_shortcode, es.source_permalink, "
        "es.source_event_key, es.source_event_index, es.cover_photo_key, "
        "es.raw_extraction, es.first_seen_at, es.last_seen_at, "
        "es.source_media_type, es.source_uploaded_at, es.source_events_truncated, "
        "es.date_interpretation "
        "FROM events.post_item_source es "
        "JOIN events.post_item e ON e.post_item_id = es.post_item_id "
        "LEFT JOIN venues.venue v ON v.venue_id = e.venue_id"
    )

    # `_EVENT_SOURCE_SELECT` widened with the source row's own `id` — neither
    # that query nor `list_event_sources`'s bespoke one carries BOTH the
    # post_item content AND the source's own primary key at once.
    # plans/260812_history-repair-dates.md needs both: `id` to resume by and
    # to target ONE specific source row, the post_item content to decide
    # whether that row's parent is protected (confirmed/operator-edited/
    # superseded) — string-built from `_EVENT_SOURCE_SELECT` so the column
    # list can never drift from the one `list_events_by_source` already
    # trusts.
    _EVENT_SOURCE_SELECT_WITH_ID = "SELECT es.id, " + _EVENT_SOURCE_SELECT[len("SELECT "):]

    def get_event(self, event_id: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"{self._EVENT_SELECT} WHERE e.post_item_id=:id"), {"id": event_id},
            ).mappings().first()
            return dict(row) if row else None

    def get_event_by_source(self, source_handle: str, source_shortcode: str) -> Optional[dict]:
        rows = self.list_events_by_source(source_handle, source_shortcode)
        return rows[0] if rows else None

    def list_events_by_source(self, source_handle: str, source_shortcode: str) -> list[dict]:
        """Every event row sharing one post, ordered for display
        (plans/260806_multi-event-posts.md) — `get_event_by_source` returns
        at most one row and is unsafe once a post can hold several. Post-
        merge (plans/260807_one-event-many-posts.md), several returned rows
        can share one `event_id` too — the same merged event, seen through
        each of its own sources' provenance."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"{self._EVENT_SOURCE_SELECT} "
                    "WHERE es.source_handle=:h AND es.source_shortcode=:s "
                    "ORDER BY es.source_event_index NULLS LAST, e.post_item_id"
                ),
                {"h": source_handle, "s": source_shortcode},
            ).mappings()
            return [dict(r) for r in rows]

    def list_event_sources(self, event_id: str) -> list[dict]:
        """Every source (announcing post) attached to one event, oldest
        first-seen first — the admin API's `sources[]`."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, post_item_id AS event_id, source_kind, source_handle, "
                    "source_shortcode, source_permalink, source_event_key, source_event_index, "
                    "cover_photo_key, raw_extraction, first_seen_at, last_seen_at, "
                    "source_media_type, source_uploaded_at, source_events_truncated, "
                    "date_interpretation "
                    "FROM events.post_item_source WHERE post_item_id=:e ORDER BY first_seen_at, id"
                ),
                {"e": event_id},
            ).mappings()
            return [dict(r) for r in rows]

    def list_all_event_sources(self) -> list[dict]:
        """Every `events.post_item_source` row in the table, oldest
        first-seen first — the whole-table view `list_event_sources(event_id)`
        cannot give (that one is scoped to a single event). Exists for
        `scripts.backfill_source_provenance`, which must consider every
        source row exactly once regardless of how many share a `post_item_id`
        (plans/260813_backfill-source-provenance.md)."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, post_item_id AS event_id, source_kind, source_handle, "
                    "source_shortcode, source_permalink, source_event_key, source_event_index, "
                    "cover_photo_key, first_seen_at, last_seen_at, "
                    "source_media_type, source_uploaded_at "
                    "FROM events.post_item_source ORDER BY first_seen_at, id"
                )
            ).mappings()
            return [dict(r) for r in rows]

    def list_all_event_sources_with_context(self) -> list[dict]:
        """Every `events.post_item_source` row across the WHOLE table, each
        joined with its OWN post_item's content/protection fields — the same
        per-source projection `list_events_by_source` trusts
        (`_EVENT_SOURCE_SELECT`), widened with the source's own `id` and with
        no WHERE clause. `list_all_event_sources` (scripts.
        backfill_source_provenance's own whole-table read) deliberately omits
        `raw_extraction`/`date_interpretation`/`title`/`status`/
        `operator_edited_fields`; `list_events`/`get_event` only ever surface
        the PRIMARY (most recently seen) source. `scripts.repair_event_dates`
        (plans/260812_history-repair-dates.md) needs EVERY source's own
        `raw_extraction.date_text`/`.time_text`, `source_uploaded_at`, and
        `date_interpretation`, plus its parent post_item's `status`/
        `operator_edited_fields`/`post_type` to decide whether it may be
        touched at all — no other existing read gives all of that at once."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(f"{self._EVENT_SOURCE_SELECT_WITH_ID} ORDER BY es.first_seen_at, es.id")
            ).mappings()
            return [dict(r) for r in rows]

    def update_event_source_provenance(
        self, source_id: str, *, source_uploaded_at=None, source_media_type: Optional[str] = None,
    ) -> bool:
        """Fill `source_uploaded_at`/`source_media_type` on ONE
        `events.post_item_source` row, identified by its own `id` (never by
        `event_id`+`source_handle`+`source_shortcode` — the caller already
        has the exact row from `list_all_event_sources`, and re-deriving the
        ambiguity-prone lookup `update_event` uses would be the wrong tool
        here).

        `COALESCE(existing, :new)` is the never-overwrite guarantee enforced
        AT THE SQL LEVEL, independent of whatever the caller's own dry-run
        decision logic already computed — a row written since the forward
        fixes keeps its live value even if a caller bug ever passed one in.
        Returns whether the row still exists (True) or vanished (False);
        never raises on a no-op write (both columns already set is a normal,
        expected outcome, not an error).
        """
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE events.post_item_source SET "
                    "source_uploaded_at = COALESCE(source_uploaded_at, :source_uploaded_at), "
                    "source_media_type = COALESCE(source_media_type, :source_media_type) "
                    "WHERE id=:id"
                ),
                {
                    "id": source_id, "source_uploaded_at": source_uploaded_at,
                    "source_media_type": source_media_type,
                },
            )
            return result.rowcount > 0

    def list_events_by_handle(self, source_handle: str) -> list[dict]:
        """Every event with AT LEAST ONE source posted under `source_handle`
        — plans/260811_merge-unresolved-into-resolved-sibling.md's handle
        identity needs to find a resolved sibling regardless of which of an
        event's (possibly several, post-merge) sources carries the matching
        handle, not just its primary (most-recently-seen) one — an EXISTS
        correlated subquery against every source, never the `_EVENT_SELECT`
        LATERAL's primary-source-only projection."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"{self._EVENT_SELECT} WHERE EXISTS ("
                    "SELECT 1 FROM events.post_item_source esh "
                    "WHERE esh.post_item_id = e.post_item_id AND esh.source_handle=:h"
                    ")"
                ),
                {"h": source_handle},
            ).mappings()
            return [dict(r) for r in rows]

    def reattach_event_sources(self, from_event_id: str, to_event_id: str) -> None:
        """Re-point every source currently on `from_event_id` at
        `to_event_id` — step 3 of a cross-post merge
        (plans/260807_one-event-many-posts.md): provenance must survive the
        collapse even though the duplicate event row does not."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE events.post_item_source SET post_item_id=:to_id WHERE post_item_id=:from_id"
                ),
                {"to_id": to_event_id, "from_id": from_event_id},
            )

    def reattach_event_source_by_id(self, source_id: str, to_event_id: str) -> None:
        """Re-point ONE specific `events.post_item_source` row (by its own
        `id`, never by `from_event_id`) at `to_event_id` —
        plans/260812_event-dedup-fuzzy-title.md §E's reversal primitive.
        `reattach_event_sources` above moves EVERY source currently on one
        event, which is correct for an absorption but wrong for a reversal:
        the canonical may have gained sources of its own (or from an
        earlier, unrelated merge) since the absorption this call is
        undoing, and only the SPECIFIC source ids that moved during THAT
        merge (`events.event_merge_suggestion.moved_source_ids`) may move
        back."""
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE events.post_item_source SET post_item_id=:to_id WHERE id=:id"),
                {"to_id": to_event_id, "id": source_id},
            )

    def delete_event(self, event_id: str) -> None:
        """Hard delete — only ever correct for a now-sourceless duplicate a
        merge has already reattached every source away from.
        `post_item_source.post_item_id` is a real, non-deferrable FK, so this
        raises (rather than silently orphaning provenance) if any source
        still references it."""
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM events.post_item WHERE post_item_id=:e"), {"e": event_id},
            )

    def insert_event(self, fields: dict) -> dict:
        """INSERTs the merged events.post_item row and its first
        events.post_item_source row in one transaction. Relies on the UNIQUE
        (source_handle, source_shortcode, source_event_key) constraint
        (migration 0026, moved from events.event by 0025; renamed
        uq_post_item_source_post by migration 0034) to reject a duplicate —
        the service is expected to check list_events_by_source first; this
        is the backstop, not the primary idempotency mechanism."""
        event_cols = [c for c in self._EVENT_COLUMNS if c in fields]
        event_assign = {c: fields[c] for c in event_cols}
        for c in self._EVENT_JSONB_COLUMNS:
            if c in event_assign and event_assign[c] is not None:
                event_assign[c] = json.dumps(event_assign[c])
        event_col_list = ", ".join(self._event_sql_col(c) for c in event_cols)
        event_val_list = ", ".join(
            f"CAST(:{c} AS jsonb)" if c in self._EVENT_JSONB_COLUMNS else f":{c}"
            for c in event_cols
        )

        source_cols = [c for c in self._EVENT_SOURCE_FIELDS if c in fields]
        source_assign = {c: fields[c] for c in source_cols}
        for c in self._EVENT_SOURCE_JSONB_COLUMNS:
            if c in source_assign and source_assign[c] is not None:
                source_assign[c] = json.dumps(source_assign[c])
        source_assign["event_id"] = fields["event_id"]
        # events.post_item_source.id is a NOT NULL primary key with no
        # column default (app-generated, like events.post_item.post_item_id
        # itself — app.services.event_reconciliation.new_event_id) — never
        # left for Postgres to fill in.
        source_assign["id"] = f"evsrc_{new_run_id()}"
        source_insert_cols = ["id", "event_id"] + source_cols
        source_col_list = ", ".join(self._event_sql_col(c) for c in source_insert_cols)
        source_val_list = ", ".join(
            f"CAST(:{c} AS jsonb)" if c in self._EVENT_SOURCE_JSONB_COLUMNS else f":{c}"
            for c in source_insert_cols
        )

        with self.engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO events.post_item ({event_col_list}) VALUES ({event_val_list})"),
                event_assign,
            )
            conn.execute(
                text(
                    f"INSERT INTO events.post_item_source ({source_col_list}) "
                    f"VALUES ({source_val_list})"
                ),
                source_assign,
            )
        return self.get_event(fields["event_id"])

    def update_event(self, event_id: str, fields: dict) -> Optional[dict]:
        """Partial update: only the keys present in `fields` change. Event-
        level keys update `events.post_item`; source-level keys (see
        `_EVENT_SOURCE_FIELDS`) update the ONE `events.post_item_source` row
        identified by `source_handle`+`source_shortcode` when both are
        given, or the event's single source when it has exactly one —
        raising when that is ambiguous, never guessing. Always bumps
        `events.post_item.updated_at`, matching the fake's contract."""
        if not fields:
            fields = {}
        event_cols = [c for c in self._EVENT_COLUMNS if c in fields and c != "event_id"]
        source_cols = [c for c in self._EVENT_SOURCE_FIELDS if c in fields]

        with self.engine.begin() as conn:
            if event_cols:
                event_assign = {c: fields[c] for c in event_cols}
                for c in self._EVENT_JSONB_COLUMNS:
                    if c in event_assign and event_assign[c] is not None:
                        event_assign[c] = json.dumps(event_assign[c])
                set_clauses = [
                    f"{c}=CAST(:{c} AS jsonb)" if c in self._EVENT_JSONB_COLUMNS else f"{c}=:{c}"
                    for c in event_cols
                ]
                set_clauses.append("updated_at=now()")
                event_assign["event_id"] = event_id
                result = conn.execute(
                    text(
                        f"UPDATE events.post_item SET {', '.join(set_clauses)} "
                        "WHERE post_item_id=:event_id"
                    ),
                    event_assign,
                )
            else:
                result = conn.execute(
                    text("UPDATE events.post_item SET updated_at=now() WHERE post_item_id=:event_id"),
                    {"event_id": event_id},
                )
            if result.rowcount == 0:
                return None

            if source_cols:
                handle = fields.get("source_handle")
                shortcode = fields.get("source_shortcode")
                if handle is not None and shortcode is not None:
                    target = conn.execute(
                        text(
                            "SELECT id FROM events.post_item_source "
                            "WHERE post_item_id=:e AND source_handle=:h AND source_shortcode=:s"
                        ),
                        {"e": event_id, "h": handle, "s": shortcode},
                    ).mappings().first()
                    if target is None:
                        raise ValueError(
                            f"update_event: no event_source row for event_id={event_id!r} "
                            f"source_handle={handle!r} source_shortcode={shortcode!r}"
                        )
                else:
                    candidates = conn.execute(
                        text("SELECT id FROM events.post_item_source WHERE post_item_id=:e"),
                        {"e": event_id},
                    ).mappings().all()
                    if len(candidates) != 1:
                        raise ValueError(
                            f"update_event: cannot route source-level fields "
                            f"{sorted(source_cols)} for event_id={event_id!r} without "
                            f"source_handle/source_shortcode — {len(candidates)} source(s) exist"
                        )
                    target = candidates[0]

                source_assign = {c: fields[c] for c in source_cols}
                for c in self._EVENT_SOURCE_JSONB_COLUMNS:
                    if c in source_assign and source_assign[c] is not None:
                        source_assign[c] = json.dumps(source_assign[c])
                set_clauses = [
                    f"{c}=CAST(:{c} AS jsonb)" if c in self._EVENT_SOURCE_JSONB_COLUMNS else f"{c}=:{c}"
                    for c in source_cols
                ]
                source_assign["id"] = target["id"]
                conn.execute(
                    text(
                        f"UPDATE events.post_item_source SET {', '.join(set_clauses)} WHERE id=:id"
                    ),
                    source_assign,
                )
        return self.get_event(event_id)

    def list_events(
        self, *, venue_id: Optional[str] = None, status: Optional[str] = None,
        since=None, until=None,
    ) -> list[dict]:
        sql = self._EVENT_SELECT
        clauses = []
        params: dict = {}
        if venue_id is not None:
            clauses.append("e.venue_id=:venue_id")
            params["venue_id"] = venue_id
        if status is not None:
            clauses.append("e.status=:status")
            params["status"] = status
        if since is not None:
            clauses.append("e.starts_at >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("e.starts_at <= :until")
            params["until"] = until
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.starts_at NULLS LAST, e.post_item_id"
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def list_events_awaiting_decision(self) -> list[dict]:
        """Every event still awaiting a human decision — the review queue's
        whole population (plans/260807_review-queue-completeness-and-venue-
        names.md, replacing the old, narrower `list_events_pending_location`,
        which excluded every venue-post event BY CONSTRUCTION regardless of
        status).

        The union of three genuine decision states:
          - `status = 'pending_review'`: nobody has confirmed this event's
            data yet. `event_reconciliation.py` sets this on EVERY persisted
            event, so this is the queue's main, wide population — an inbox,
            not an error flag.
          - `e.venue_id IS NULL`: nobody has decided where it happens.
            plans/260810_date-correctness-review-reasons-and-path-parity.md
            §D widened this from `ps.source_kind = 'promoter_post' AND
            location_resolution IS NULL` — the OLD condition keyed on
            `source_kind`, which is exactly what made the shared-handle
            crawl path's provenance bug (`_chain_shared_handle` stamping a
            venue's own post `promoter_post`) the ONLY reason those events
            reached the queue at all. `venue_id` is the real signal
            regardless of source kind: a venue-post event's `venue_id` is
            constant attribution (always the posting venue) and can only be
            NULL when a shared-handle post could not be attributed to
            either of its handle's venues, and a promoter event's `venue_id`
            is NULL for both `RESOLUTION_QUEUED` (candidates awaiting a
            pick) and `RESOLUTION_UNRESOLVED` (none found) alike — a
            strictly more complete signal than `location_resolution IS
            NULL`, which missed the `unresolved` case (that resolution
            writes the literal string `'unresolved'`, not NULL). NOT
            redundant with the first clause — an operator can `/confirm` an
            event's fields while its venue is still unresolved, which moves
            status off `pending_review` but leaves `venue_id` NULL; that
            event still needs a decision and must not silently drop out,
            whichever crawl path produced it.

        The second clause is guarded with `status NOT IN ('rejected',
        'superseded')`: `/reject` and the reconciliation supersede path both
        leave `venue_id` untouched, so an event that was queued for a venue
        decision and never got one before an operator rejected it (or a
        later crawl superseded it) would otherwise resurrect here forever —
        exactly the "finished with" case the plan requires this queue to
        keep excluding.

        A third clause, `status = 'extraction_failed'`, is explicit for BOTH
        source kinds (plans/260807_date-resolution-correctness.md, defect 3):
        an extraction-failure placeholder ALWAYS carries a non-NULL
        `venue_id` for a venue post (the venue currently being crawled) —
        it never matches the second clause on its own — and even a
        promoter-post one is explicit rather than incidental. A total
        extraction failure is lost signal on a post already paid for
        (archived, classified); it deserves an operator's attention at
        least as much as a clean unconfirmed event, never less.

        plans/260811_post-items-and-categories.md: `post_type` is
        DELIBERATELY absent from this predicate. What needs a decision is a
        property of the row's STATE (confirmed? venue linked? extraction
        clean?), never of its type — a clean menu item auto-accepts and
        never queues, exactly as a clean event does, and a flagged item of
        ANY type comes back here. Do not add a `post_type` filter.
        """
        sql = (
            f"{self._EVENT_SELECT} WHERE e.status = 'pending_review' "
            "OR (e.venue_id IS NULL AND e.status NOT IN ('rejected', 'superseded')) "
            "OR e.status = 'extraction_failed' "
            "ORDER BY agg.first_seen_at, e.post_item_id"
        )
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql)).mappings()]

    # ── instagram.handle reverse index (plans/260804_instagram-promoter-events.md) ─
    def list_instagram_handles(self) -> dict[str, str]:
        """venue_id -> instagram_handle, for every venue with a confirmed
        handle. The resolution ladder's rung 1 reverses this into
        handle -> venue_id; free, since it reads what the cascade already
        discovered rather than a new provider call."""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT venue_id, instagram_handle FROM instagram.handle "
                "WHERE deleted_at IS NULL AND instagram_handle IS NOT NULL"
            )).mappings()
            return {r["venue_id"]: r["instagram_handle"] for r in rows}

    # ── events.promoter_account (plans/260804_instagram-promoter-events.md) ──
    _PROMOTER_COLUMNS = (
        "handle", "display_name", "status", "discovery_source",
        "discovered_from_event_id", "mention_count", "notes", "added_by",
        "last_crawled_at", "posts_crawled", "events_extracted",
    )
    _PROMOTER_SELECT = (
        "SELECT handle, display_name, status, discovery_source, "
        "discovered_from_event_id, mention_count, notes, added_by, "
        "last_crawled_at, posts_crawled, events_extracted, "
        "created_at, updated_at FROM events.promoter_account"
    )

    def get_promoter_account(self, handle: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"{self._PROMOTER_SELECT} WHERE handle=:h"), {"h": handle},
            ).mappings().first()
            return dict(row) if row else None

    def list_promoter_accounts(self, status: Optional[str] = None) -> list[dict]:
        sql = self._PROMOTER_SELECT
        params: dict = {}
        if status is not None:
            sql += " WHERE status=:status"
            params["status"] = status
        sql += " ORDER BY handle"
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def upsert_promoter_account(self, handle: str, fields: dict) -> dict:
        """INSERT ... ON CONFLICT (handle) DO UPDATE, so registering an
        already-known handle (e.g. discovery re-proposing a manually added
        one) updates it in place rather than raising. Only the keys present
        in `fields` are set on conflict; the primary key and any column the
        caller omitted are left untouched — mirrors update_event's partial-
        update contract."""
        cols = [c for c in self._PROMOTER_COLUMNS if c in fields and c != "handle"]
        assign = {c: fields[c] for c in cols}
        assign["handle"] = handle
        insert_cols = ["handle"] + cols
        col_list = ", ".join(insert_cols)
        val_list = ", ".join(f":{c}" for c in insert_cols)
        if cols:
            update_clause = ", ".join(f"{c}=:{c}" for c in cols) + ", updated_at=now()"
            conflict_sql = f"DO UPDATE SET {update_clause}"
        else:
            conflict_sql = "DO NOTHING"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO events.promoter_account ({col_list}) "
                    f"VALUES ({val_list}) "
                    f"ON CONFLICT (handle) {conflict_sql}"
                ),
                assign,
            )
        return self.get_promoter_account(handle)

    # ── events.event_venue_link_candidate (plans/260804_instagram-promoter-events.md) ─
    def replace_event_venue_link_candidates(self, event_id: str, candidates: list[dict]) -> None:
        """Replace the whole ranked candidate list for one event — a later
        crawl of the same post recomputes the ladder from scratch, so the
        old ranking must not linger alongside a new one. DELETE-then-INSERT
        in one transaction, matching the fake's replace-whole-list contract."""
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM events.event_venue_link_candidate WHERE event_id=:e"),
                {"e": event_id},
            )
            for c in candidates:
                conn.execute(
                    text(
                        "INSERT INTO events.event_venue_link_candidate "
                        "(event_id, venue_id, rank, score, method, evidence) "
                        "VALUES (:event_id, :venue_id, :rank, :score, :method, "
                        "CAST(:evidence AS jsonb))"
                    ),
                    {
                        "event_id": event_id, "venue_id": c["venue_id"],
                        "rank": c["rank"], "score": c.get("score"),
                        "method": c["method"],
                        "evidence": json.dumps(c.get("evidence") or {}),
                    },
                )

    def list_event_venue_link_candidates(self, event_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT event_id, venue_id, rank, score, method, evidence, created_at "
                    "FROM events.event_venue_link_candidate "
                    "WHERE event_id=:e ORDER BY rank"
                ),
                {"e": event_id},
            ).mappings()
            return [dict(r) for r in rows]

    # ── events.event_merge_suggestion (plans/260812_event-dedup-fuzzy-title.md
    # §C/§E, migration 0038) ────────────────────────────────────────────────
    _MERGE_SUGGESTION_COLUMNS = (
        "suggestion_id", "event_id", "candidate_event_id", "band", "reasons",
        "event_distinctive_words", "candidate_distinctive_words", "shared_lineup_names",
        "decision", "moved_source_ids", "absorbed_status_before", "created_at",
        "decided_at", "decided_by",
    )
    _MERGE_SUGGESTION_JSONB_COLUMNS = (
        "reasons", "event_distinctive_words", "candidate_distinctive_words",
        "shared_lineup_names", "moved_source_ids",
    )
    _MERGE_SUGGESTION_SELECT = (
        "SELECT suggestion_id, event_id, candidate_event_id, band, reasons, "
        "event_distinctive_words, candidate_distinctive_words, shared_lineup_names, "
        "decision, moved_source_ids, absorbed_status_before, created_at, decided_at, "
        "decided_by FROM events.event_merge_suggestion"
    )

    def create_event_merge_suggestion(self, fields: dict) -> dict:
        """Insert one `events.event_merge_suggestion` row — written both for
        a SUGGEST-band pair awaiting an operator (`decision='pending'`) and,
        in the SAME shape, for every AUTO-band absorption this feature
        itself performs (`decision='auto_merged'`) — see the migration's own
        docstring for why one table serves both."""
        cols = [c for c in self._MERGE_SUGGESTION_COLUMNS if c in fields]
        assign = {c: fields[c] for c in cols}
        for c in self._MERGE_SUGGESTION_JSONB_COLUMNS:
            if c in assign and assign[c] is not None:
                assign[c] = json.dumps(assign[c])
        col_list = ", ".join(cols)
        val_list = ", ".join(
            f"CAST(:{c} AS jsonb)" if c in self._MERGE_SUGGESTION_JSONB_COLUMNS else f":{c}"
            for c in cols
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO events.event_merge_suggestion ({col_list}) VALUES ({val_list})"),
                assign,
            )
        return self.get_event_merge_suggestion(fields["suggestion_id"])

    def get_event_merge_suggestion(self, suggestion_id: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"{self._MERGE_SUGGESTION_SELECT} WHERE suggestion_id=:id"),
                {"id": suggestion_id},
            ).mappings().first()
            return dict(row) if row else None

    def list_event_merge_suggestions(
        self, *, event_id: Optional[str] = None, candidate_event_id: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> list[dict]:
        """Every suggestion touching `event_id` on EITHER side (surviving or
        candidate) — the review queue needs both directions on one event, and
        a reversal looks a row up by `candidate_event_id` alone (the absorbed
        side, which no longer knows its own canonical any other way)."""
        sql = self._MERGE_SUGGESTION_SELECT
        clauses = []
        params: dict = {}
        if event_id is not None:
            clauses.append("(event_id=:event_id OR candidate_event_id=:event_id)")
            params["event_id"] = event_id
        if candidate_event_id is not None:
            clauses.append("candidate_event_id=:candidate_event_id")
            params["candidate_event_id"] = candidate_event_id
        if decision is not None:
            clauses.append("decision=:decision")
            params["decision"] = decision
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, suggestion_id"
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def update_event_merge_suggestion(self, suggestion_id: str, fields: dict) -> Optional[dict]:
        """Partial update — used to move a suggestion's `decision` (pending
        -> applied/rejected/reversed) and to record `moved_source_ids`/
        `absorbed_status_before` once a merge actually applies."""
        if not fields:
            return self.get_event_merge_suggestion(suggestion_id)
        cols = [c for c in self._MERGE_SUGGESTION_COLUMNS if c in fields and c != "suggestion_id"]
        assign = {c: fields[c] for c in cols}
        for c in self._MERGE_SUGGESTION_JSONB_COLUMNS:
            if c in assign and assign[c] is not None:
                assign[c] = json.dumps(assign[c])
        set_clauses = [
            f"{c}=CAST(:{c} AS jsonb)" if c in self._MERGE_SUGGESTION_JSONB_COLUMNS else f"{c}=:{c}"
            for c in cols
        ]
        assign["suggestion_id"] = suggestion_id
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    f"UPDATE events.event_merge_suggestion SET {', '.join(set_clauses)} "
                    "WHERE suggestion_id=:suggestion_id"
                ),
                assign,
            )
            if result.rowcount == 0:
                return None
        return self.get_event_merge_suggestion(suggestion_id)

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

    def list_instagram_sources(self) -> list[tuple]:
        """(source, count) across live handles.

        The point of promoting `source` to a column: this is the question the
        cascade exists to answer, and it must not require jsonb extraction over
        the whole catalog.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT source, count(*) FROM instagram.handle "
                    "WHERE deleted_at IS NULL GROUP BY source"
                )
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

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
        # Only write promoted columns the caller actually provided. An omitted
        # column falls to its DB default (e.g. primary_type_locked → false)
        # instead of an explicit NULL, and on a conflict its existing value is
        # preserved rather than reset. A caller that means NULL still passes the
        # key explicitly with a None value.
        active_promoted = [c for c in promoted_cols if c in promoted]
        cols = ["venue_id", "payload", "deleted_at", "updated_at"] + active_promoted
        vals = [":venue_id", "CAST(:payload AS jsonb)", "NULL", "now()"] + [f":{c}" for c in active_promoted]
        sets = ["payload=excluded.payload", "deleted_at=NULL", "updated_at=now()"] + \
               [f"{c}=excluded.{c}" for c in active_promoted]
        params = {"venue_id": venue_id, "payload": json.dumps(payload)}
        params.update({c: promoted[c] for c in active_promoted})
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

    def get_favorite(self, user_pseudo, venue_id) -> Optional[dict]:
        """Mirrors get_block exactly (same SELECT shape, over engagement.favorite
        instead of engagement.blocked_venue) — the symmetric read accessor for
        favorites, used to assert the block-clears-favorite atomicity directly."""
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT user_pseudo, venue_id, created_at, deleted_at, updated_at "
                "FROM engagement.favorite WHERE user_pseudo=:u AND venue_id=:v"
            ), {"u": user_pseudo, "v": venue_id}).mappings().first()
            return dict(row) if row else None

    def block_venue(self, user_pseudo, venue_id) -> bool:
        """Block a venue and atomically clear any active favorite for the same
        (user_pseudo, venue_id) — ONE transaction, so a venue is never
        observably both blocked and favorited, and a mid-transaction failure
        (e.g. the venue_id FK violation) leaves neither write applied. Mirrors
        the upsert shape `upsert_favorite` uses (ON CONFLICT DO UPDATE SET
        deleted_at=NULL) plus a conditional favorite soft-delete, the same
        multi-statement-one-`engine.begin()` pattern `set_geo_fence` and
        `purge_user_engagement` already use.

        Returns:
            True when this call actually soft-deleted an active favorite for
            this (user, venue) pair, False otherwise (nothing to clear).
        """
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO engagement.blocked_venue (user_pseudo, venue_id, deleted_at, updated_at) "
                "VALUES (:u, :v, NULL, now()) "
                "ON CONFLICT (user_pseudo, venue_id) DO UPDATE SET deleted_at=NULL, updated_at=now()"
            ), {"u": user_pseudo, "v": venue_id})
            result = conn.execute(text(
                "UPDATE engagement.favorite SET deleted_at=now(), updated_at=now() "
                "WHERE user_pseudo=:u AND venue_id=:v AND deleted_at IS NULL"
            ), {"u": user_pseudo, "v": venue_id})
            return (result.rowcount or 0) > 0

    def soft_delete_block(self, user_pseudo, venue_id) -> None:
        """Unblock: soft-delete the block row only. Deliberately does NOT touch
        engagement.favorite — restoring a favorite on unblock is a locked
        product non-goal, not an oversight."""
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE engagement.blocked_venue SET deleted_at=now(), updated_at=now() "
                "WHERE user_pseudo=:u AND venue_id=:v"
            ), {"u": user_pseudo, "v": venue_id})

    def get_block(self, user_pseudo, venue_id) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT user_pseudo, venue_id, created_at, deleted_at, updated_at "
                "FROM engagement.blocked_venue WHERE user_pseudo=:u AND venue_id=:v"
            ), {"u": user_pseudo, "v": venue_id}).mappings().first()
            return dict(row) if row else None

    def add_hot_like_event(self, user_pseudo, venue_id, business_period) -> bool:
        """Idempotent per (user_pseudo, venue_id, business_period): the unique
        index (migration 0016) + ON CONFLICT DO NOTHING absorb a retried write
        (vibes_bot retries on a 5xx per the engagement_router contract) instead
        of persisting a second event row. `business_period` is the caller's
        Recife calendar day (recife_today()), the same "one row per local day"
        idempotency key record_app_session uses.

        Returns:
            True when a new row was inserted, False when an existing row for
            this (user, venue, day) already covered it (conflict-suppressed).
        """
        with self.engine.begin() as conn:
            result = conn.execute(text(
                "INSERT INTO engagement.hot_like_event (user_pseudo, venue_id, business_period) "
                "VALUES (:u, :v, :b) "
                "ON CONFLICT (user_pseudo, venue_id, business_period) DO NOTHING"
            ), {"u": user_pseudo, "v": venue_id, "b": business_period})
            return result.rowcount > 0

    # ── app activity (one row per user per Recife day) ────────────────────────
    # ── erasure (account deletion) ────────────────────────────────────────────
    def list_user_hot_like_venue_ids(self, user_pseudo) -> list[str]:
        """DISTINCT venues this user hot-liked. The unique index is per business
        period, so one (user, venue) pair legitimately holds many rows — DISTINCT
        keeps the caller from re-cleaning the same Redis set repeatedly."""
        with self.engine.connect() as conn:
            return [
                r[0] for r in conn.execute(
                    text(
                        "SELECT DISTINCT venue_id FROM engagement.hot_like_event "
                        "WHERE user_pseudo = :p ORDER BY venue_id"
                    ),
                    {"p": user_pseudo},
                )
            ]

    def purge_user_engagement(self, user_pseudo) -> dict:
        """HARD-delete every engagement row bearing this pseudonym, in one
        transaction. Deliberately not a soft delete: a surviving pseudonymized
        row makes the operation a deactivation rather than an erasure."""
        with self.engine.begin() as conn:
            favorites = conn.execute(
                text("DELETE FROM engagement.favorite WHERE user_pseudo = :p"),
                {"p": user_pseudo},
            ).rowcount or 0
            hot_likes = conn.execute(
                text("DELETE FROM engagement.hot_like_event WHERE user_pseudo = :p"),
                {"p": user_pseudo},
            ).rowcount or 0
            sessions = conn.execute(
                text("DELETE FROM engagement.app_session_day WHERE user_pseudo = :p"),
                {"p": user_pseudo},
            ).rowcount or 0
            blocked_venues = conn.execute(
                text("DELETE FROM engagement.blocked_venue WHERE user_pseudo = :p"),
                {"p": user_pseudo},
            ).rowcount or 0
        return {
            "favorites": favorites,
            "hot_like_events": hot_likes,
            "app_sessions": sessions,
            "blocked_venues": blocked_venues,
        }

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

    # ── events.crawl_target (plans/260809_scheduled-incremental-instagram-crawl.md) ──
    _CRAWL_TARGET_KINDS = ("venue", "promoter")
    _CRAWL_TARGET_COLUMNS = (
        "handle", "kind", "enabled", "cron", "timezone", "crawl_reels",
        "classify_images", "initial_lookback", "results_limit", "seed_results_limit",
        "reels_results_limit", "reels_seed_results_limit",
        "cursor_posts_at", "cursor_reels_at", "last_run_at", "last_run_results",
        "last_run_cost_usd", "consecutive_failures", "notes",
        # plans/260810_stream-dedupe-and-venue-attribution.md §B (migration
        # 0033): per-run reels-overlap counts, written only on a run whose
        # reels stream actually executed — NULL means "not yet measured",
        # never zero.
        "last_run_reels_fetched", "last_run_reels_new",
        # plans/260812_crawl-error-visibility.md §B (migration 0036): which
        # outcome (blocked, handle_not_found, or failed) the target's last
        # FAILED run hit, and when — written by run_target, left standing
        # (never cleared) on a run that did not fail, so these always answer
        # "what was the last failure and when." NULL means no failure has
        # ever been recorded.
        "last_failure_kind", "last_failure_at",
        # plans/260813_dormant-vs-broken-targets.md §A (migration 0038):
        # whether the posts stream's most recent answer was the DORMANT
        # signature — recomputed and overwritten on every run, never
        # sticky like `last_failure_kind` (see the migration's own
        # docstring for why neither existing column could carry this).
        "posts_dormant",
        # plans/260814_seeded-state-and-config-validation.md §A (migration
        # 0041): whether the reels stream has reached its one-time seed to
        # COMPLETION — success, or a genuine empty result, both. Distinct
        # from `cursor_reels_at` (the newest reel actually reached, still
        # NULL when nothing was reached): an empty-but-successful seed has
        # no timestamp to write there, so gating "seeded" on the cursor
        # alone re-buys the seed forever on an empty account. See
        # `app.services.instagram_crawl_service.reels_already_seeded`.
        "reels_seeded_at",
    )
    _CRAWL_TARGET_SELECT = (
        "SELECT handle, kind, enabled, cron, timezone, crawl_reels, "
        "classify_images, initial_lookback, results_limit, seed_results_limit, "
        "reels_results_limit, reels_seed_results_limit, "
        "cursor_posts_at, cursor_reels_at, last_run_at, last_run_results, "
        "last_run_cost_usd, consecutive_failures, notes, "
        "last_run_reels_fetched, last_run_reels_new, "
        "last_failure_kind, last_failure_at, posts_dormant, reels_seeded_at, "
        "created_at, updated_at "
        "FROM events.crawl_target"
    )

    def get_crawl_target(self, handle: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"{self._CRAWL_TARGET_SELECT} WHERE handle=:h"), {"h": handle},
            ).mappings().first()
            return dict(row) if row else None

    def list_crawl_targets(
        self, *, enabled: Optional[bool] = None, kind: Optional[str] = None,
    ) -> list[dict]:
        sql = self._CRAWL_TARGET_SELECT
        clauses = []
        params: dict = {}
        if enabled is not None:
            clauses.append("enabled=:enabled")
            params["enabled"] = enabled
        if kind is not None:
            clauses.append("kind=:kind")
            params["kind"] = kind
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY handle"
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def upsert_crawl_target(self, handle: str, fields: dict) -> dict:
        """CREATE-only: INSERT ... ON CONFLICT (handle) DO UPDATE. Reserved
        for the admin router's POST (create) endpoint, whose `CrawlTargetCreate`
        model always supplies every NOT NULL column — `kind` defaults to
        `"venue"`, `cron` is required — so the INSERT branch is always safe.

        DO NOT call this for a partial update of a row that may already
        exist: `kind` and `cron` are NOT NULL with no database default
        (migration 0030_crawl_target), and Postgres validates NOT NULL
        constraints against the FULLY CONSTRUCTED insert tuple — every column
        the statement's column list omits defaults to NULL — BEFORE it ever
        evaluates `ON CONFLICT`. That check runs on every call, regardless of
        whether the row already has `kind`/`cron` from a previous insert, so
        a partial `fields` dict fails with `NotNullViolation` even though the
        row certainly exists. This is not hypothetical: a production
        incident (2026-08-09) hit exactly this from `run_target`'s post-run
        bookkeeping write, which passed only cursor/last_run fields — the
        crawl was billed and scraped successfully, then this call raised and
        silently discarded every result. That write now goes through
        `update_crawl_target` below, a plain UPDATE with no INSERT branch to
        trip this on; the admin router's PATCH endpoint does too.

        Enforced here in Python (raising `ValueError` before any SQL runs)
        rather than left to Postgres, so the mistake is loud and immediate
        wherever this is called from, not just in production against a real
        database.
        """
        missing = [c for c in ("kind", "cron") if not fields.get(c)]
        if missing:
            raise ValueError(
                f"upsert_crawl_target requires {missing} (NOT NULL, no database "
                "default) on every call — it is CREATE-only. For a partial update "
                "of a row known to already exist, use update_crawl_target instead."
            )
        cols = [c for c in self._CRAWL_TARGET_COLUMNS if c in fields and c != "handle"]
        assign = {c: fields[c] for c in cols}
        assign["handle"] = handle
        insert_cols = ["handle"] + cols
        col_list = ", ".join(insert_cols)
        val_list = ", ".join(f":{c}" for c in insert_cols)
        update_clause = ", ".join(f"{c}=:{c}" for c in cols) + ", updated_at=now()"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO events.crawl_target ({col_list}) "
                    f"VALUES ({val_list}) "
                    f"ON CONFLICT (handle) DO UPDATE SET {update_clause}"
                ),
                assign,
            )
        return self.get_crawl_target(handle)

    def update_crawl_target(self, handle: str, fields: dict) -> Optional[dict]:
        """Plain `UPDATE ... WHERE handle=:handle` — the honest statement for
        every write that KNOWS the row already exists (the post-run
        bookkeeping write in `instagram_crawl_service.py` and the admin
        router's PATCH endpoint, both of which fetch/confirm the row before
        ever calling this). A plain UPDATE has no INSERT branch, so it can
        never trip the NOT NULL-on-insert-attempt failure `upsert_crawl_target`
        is restricted to guard against (see its docstring for the production
        incident that forced this split).

        Only the keys present in `fields` are set; everything else is left
        untouched. An empty `fields` (there is no valid empty SQL SET clause)
        or a `handle` that doesn't exist are both safe no-ops — the latter
        returns None, exactly like the UPDATE affecting zero rows.
        """
        cols = [c for c in self._CRAWL_TARGET_COLUMNS if c in fields and c != "handle"]
        if not cols:
            return self.get_crawl_target(handle)
        assign = {c: fields[c] for c in cols}
        assign["handle"] = handle
        set_clause = ", ".join(f"{c}=:{c}" for c in cols) + ", updated_at=now()"
        with self.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE events.crawl_target SET {set_clause} WHERE handle=:handle"),
                assign,
            )
        return self.get_crawl_target(handle)

    def delete_crawl_target(self, handle: str) -> bool:
        """Hard delete — there is no soft-delete column on this table (see
        migration 0030's downgrade note: losing a target's cursor only means
        its next crawl re-seeds from a lookback window, never a destructive
        loss of event data). Returns True iff a row existed."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM events.crawl_target WHERE handle=:h"), {"h": handle},
            )
            return result.rowcount > 0
