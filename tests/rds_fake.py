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
    # events.crawl_target's ck_crawl_target_kind CHECK constraint
    # (migration 0030_crawl_target) — enforced below, not just documented.
    _CRAWL_TARGET_KINDS = ("venue", "promoter")

    def __init__(self) -> None:
        self.venues: dict[str, dict] = {}
        # Ex3: venues.address (1:1) — address raw text + structured components +
        # lat/lng. Dual-written on every venue upsert; the source for reconstruction.
        self.addresses: dict[str, dict] = {}
        # table_key ("schema.table") -> venue_id -> row dict
        self.enrichment: dict[str, dict[str, dict]] = {}
        # admin.venue_closure_signal: venue_id -> signal row. Evidence-derived and
        # reversible — it gates serving without touching lifecycle_status.
        self.closure_signals: dict[str, dict] = {}
        self.live_forecast: dict[str, dict] = {}
        self.favorites: dict[tuple[str, str], dict] = {}
        # engagement.blocked_venue — same PK/soft-delete shape as favorites.
        self.blocked_venues: dict[tuple[str, str], dict] = {}
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
        # events.venue_event_profile: venue_id -> profile row (see
        # plans/260804_event-venue-targeting.md).
        self.event_profiles: dict[str, dict] = {}
        # events.event: event_id -> row of MERGED fields only (see
        # plans/260804_instagram-event-extraction.md, restructured by
        # plans/260807_one-event-many-posts.md). Per-post provenance
        # (source_kind/handle/shortcode/permalink/source_event_key/
        # source_event_index/cover_photo_key/raw_extraction/first_seen_at/
        # last_seen_at) lives in `self.event_sources` — see that attribute's
        # comment for why the split matters.
        self.events: dict[str, dict] = {}
        # events.event_source: id -> row (event_id, source_kind,
        # source_handle, source_shortcode, source_permalink,
        # source_event_key, source_event_index, cover_photo_key,
        # raw_extraction, first_seen_at, last_seen_at). One row per POST that
        # announced an event; several can share one event_id once a
        # cross-post merge recognises them as the same real-world night
        # (plans/260807_one-event-many-posts.md). The UNIQUE (source_handle,
        # source_shortcode, source_event_key) idempotency guarantee (moved
        # here from events.event by migration 0026, replacing 0025's
        # two-table-away version) is enforced in `insert_event`, mirroring
        # the real constraint rather than adding a second index.
        self.event_sources: dict[str, dict] = {}
        self._event_source_seq = 0
        # events.promoter_account: handle -> row (see
        # plans/260804_instagram-promoter-events.md).
        self.promoter_accounts: dict[str, dict] = {}
        # events.event_venue_link_candidate: event_id -> ranked candidate rows.
        self.event_link_candidates: dict[str, list[dict]] = {}
        # events.crawl_target: handle -> row (see plans/260809_scheduled-
        # incremental-instagram-crawl.md, migration 0030_crawl_target).
        self.crawl_targets: dict[str, dict] = {}
        # events.event_merge_suggestion: suggestion_id -> row (plans/260812_
        # event-dedup-fuzzy-title.md §C/§E, migration 0038).
        self.event_merge_suggestions: dict[str, dict] = {}
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
        sort last). A non-positive limit selects nothing.

        Excludes venue_source='google_only' — mirrors RdsVenueStore: those venues
        carry no BestTime id to query."""
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
            and row.get("venue_source", "besttime") != "google_only"
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
            # Closure is a further reversible predicate, alongside the geo-fence:
            # a venue whose newest review reports it permanently closed leaves the
            # serving view without any lifecycle change, and returns on its own
            # once newer evidence clears the signal. Only high confidence excludes.
            signal = self.closure_signals.get(vid)
            if signal and signal.get("closed") and signal.get("confidence") == "high":
                continue
            out.append(vid)
        return out

    def is_venue_servable(self, venue_id: str) -> bool:
        """Single-venue counterpart of `list_servable_venue_ids()`. Reuses it
        rather than re-deriving the eligibility predicate, so the fake never
        drifts into a second copy of that logic."""
        return venue_id in set(self.list_servable_venue_ids())

    # ── admin.venue_closure_signal ────────────────────────────────────────────
    def set_closure_signal(self, venue_id: str, signal: dict) -> None:
        self._guard()
        self.closure_signals[venue_id] = dict(signal)

    def clear_closure_signal(self, venue_id: str) -> None:
        self._guard()
        self.closure_signals.pop(venue_id, None)

    def get_closure_signal(self, venue_id: str) -> Optional[dict]:
        self._guard()
        row = self.closure_signals.get(venue_id)
        return dict(row) if row else None

    def list_closure_signals(self) -> list[dict]:
        self._guard()
        return [dict(row) for row in self.closure_signals.values()]

    def list_servable_venue_ids_by_priority(self, limit: int) -> list[str]:
        """Mirror RdsVenueStore: the top-`limit` servable (active AND eligible)
        venue ids ordered by priority asc, reviews desc, rating desc, venue_id asc.
        Reuses list_servable_venue_ids() (the eligibility serving view, the single
        source of truth) and applies the same ordering keys as
        list_active_venue_ids_by_priority. A non-positive limit selects nothing.

        Excludes venue_source='google_only' — these venues stay servable (they
        ARE in list_servable_venue_ids()); only bounded refresh selection skips
        them, mirroring RdsVenueStore."""
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

        rows = [
            (vid, row) for vid, row in self.venues.items()
            if vid in servable and row.get("venue_source", "besttime") != "google_only"
        ]
        rows.sort(key=_key)
        return [vid for vid, _ in rows[:limit]]

    def list_event_candidate_ids_by_priority(self, limit: int) -> list[str]:
        """Mirror RdsVenueStore: the top-`limit` servable venues by priority,
        WITHOUT the venue_source='google_only' exclusion that
        list_servable_venue_ids_by_priority applies. A google_only venue must
        be eligible here — see plans/260804_event-venue-targeting.md."""
        if limit <= 0:
            return []
        servable = set(self.list_servable_venue_ids())

        def _key(item):
            vid, row = item
            # A google_only venue may genuinely carry a NULL priority (never
            # BestTime-tiered); mirror Postgres's ASC-default NULLS LAST so it
            # tie-breaks on reviews/rating instead of raising or sorting
            # arbitrarily against a real int priority.
            priority = row.get("priority")
            priority_key = priority if priority is not None else float("inf")
            reviews = row.get("reviews")
            rating = row.get("rating")
            reviews_key = -(reviews if reviews is not None else float("-inf"))
            rating_key = -(rating if rating is not None else float("-inf"))
            return (priority_key, reviews_key, rating_key, vid)

        rows = [(vid, row) for vid, row in self.venues.items() if vid in servable]
        rows.sort(key=_key)
        return [vid for vid, _ in rows[:limit]]

    # ── events.venue_event_profile ─────────────────────────────────────────────
    def upsert_venue_event_profile(
        self, venue_id, *, tier, category_pass, category_reason=None,
        evidence_score=None, evidence_sample=None, evaluated_at=None,
    ) -> None:
        self._guard()
        self.event_profiles[venue_id] = {
            "venue_id": venue_id,
            "tier": tier,
            "category_pass": category_pass,
            "category_reason": category_reason,
            "evidence_score": evidence_score,
            "evidence_sample": copy.deepcopy(evidence_sample),
            "evaluated_at": evaluated_at,
            "updated_at": _now(),
        }

    def get_venue_event_profile(self, venue_id) -> Optional[dict]:
        row = self.event_profiles.get(venue_id)
        return copy.deepcopy(row) if row else None

    def list_venue_event_profiles(self, tiers: Optional[list[str]] = None) -> list[dict]:
        tier_set = set(tiers) if tiers else None
        out = []
        for row in self.event_profiles.values():
            if tier_set is not None and row.get("tier") not in tier_set:
                continue
            venue_row = self.venues.get(row["venue_id"], {})
            merged = dict(row)
            merged["venue_source"] = venue_row.get("venue_source", "besttime")
            out.append(merged)
        return out

    # ── events.event / events.event_source (plans/260804_instagram-event-
    # extraction.md, restructured by plans/260807_one-event-many-posts.md) ──
    # `self.events` holds MERGED fields only; `self.event_sources` holds one
    # row per announcing post. Every event-returning method below joins the
    # two back into the SAME flat shape callers relied on before the split —
    # `source_kind`/`source_handle`/`source_shortcode`/`source_permalink`/
    # `cover_photo_key`/`first_seen_at`/`last_seen_at` are DERIVED, never
    # stored on `self.events` directly, so a caller cannot silently drift
    # from the real schema by writing to a column that no longer exists
    # there.
    _EVENT_SOURCE_FIELDS = (
        "source_kind", "source_handle", "source_shortcode", "source_permalink",
        "source_event_key", "source_event_index", "cover_photo_key",
        "raw_extraction", "first_seen_at", "last_seen_at",
        # plans/260812_crawl-error-visibility.md §C/§D (migration 0036):
        # Apify's own media type, the post's own upload timestamp (distinct
        # from first_seen_at/last_seen_at above, which are CRAWL times), and
        # whether the per-post event cap dropped trailing entries from this
        # post's own extraction.
        "source_media_type", "source_uploaded_at", "source_events_truncated",
        # plans/260812_event-attribution-and-dates.md §C (migration 0037):
        # the model's structured date-interpretation fallback, persisted
        # NEXT TO raw_extraction (its own column) — the determinism guard's
        # own store.
        "date_interpretation",
    )

    # A source's first/last_seen_at can be an ISO string (the fake's own
    # `_now()` default, or a caller-supplied fixture like tests/
    # test_review_queue_completeness.py's) OR a real datetime (every real
    # reconciliation call) — DIFFERENT sources on the SAME event can now mix
    # the two once several posts share one event (plans/260807_one-event-
    # many-posts.md), which a plain `<`/`min`/`max` cannot compare. Coerce
    # through `_coerce_dt` (already used elsewhere in this file for the
    # identical reason) before ever comparing two of these.
    _EPOCH = datetime.min.replace(tzinfo=timezone.utc)

    def _sort_dt(self, value):
        return _coerce_dt(value) or self._EPOCH

    def _sources_for(self, event_id: str) -> list[dict]:
        return [s for s in self.event_sources.values() if s["event_id"] == event_id]

    def _primary_source(self, event_id: str) -> Optional[dict]:
        """The source EventOut's four legacy scalars (source_permalink,
        source_handle, source_shortcode, cover_photo_key) are derived from —
        the MOST RECENTLY SEEN source, so the deployed console's flyer
        viewer keeps working unchanged across a merge
        (plans/260807_one-event-many-posts.md's compatibility guarantee)."""
        sources = self._sources_for(event_id)
        if not sources:
            return None
        return max(sources, key=lambda s: (self._sort_dt(s.get("last_seen_at")), s["id"]))

    def _seen_bounds(self, event_id: str) -> tuple:
        sources = self._sources_for(event_id)
        firsts = [self._sort_dt(s.get("first_seen_at")) for s in sources if s.get("first_seen_at") is not None]
        lasts = [self._sort_dt(s.get("last_seen_at")) for s in sources if s.get("last_seen_at") is not None]
        return (min(firsts) if firsts else None, max(lasts) if lasts else None)

    def _merged_view(self, row: dict) -> dict:
        """`events.event` row -> the full backward-compatible flat dict:
        + venue_name (LEFT JOIN), + the legacy per-source scalars derived
        from the PRIMARY (most recently seen) source — source_permalink,
        source_handle, source_shortcode and cover_photo_key are the four the
        API compatibility guarantee names explicitly
        (plans/260807_one-event-many-posts.md); source_kind/
        source_event_key/source_event_index/raw_extraction ride along for
        the same reason (existing single-source callers read them off
        get_event/list_events, and "the primary source's own value" is the
        only non-arbitrary answer once several sources exist) — +
        first_seen_at/last_seen_at aggregated across EVERY source (earliest
        first-seen, latest last-seen). The columns leave events.event, the
        response shape does not."""
        out = copy.deepcopy(row)
        venue_id = out.get("venue_id")
        venue = self.venues.get(venue_id) if venue_id else None
        out["venue_name"] = venue.get("venue_name") if venue else None
        primary = self._primary_source(row["event_id"])
        for field in self._EVENT_SOURCE_FIELDS:
            if field in ("first_seen_at", "last_seen_at"):
                continue
            out[field] = primary.get(field) if primary else None
        first_seen, last_seen = self._seen_bounds(row["event_id"])
        out["first_seen_at"] = first_seen
        out["last_seen_at"] = last_seen
        return out

    def _view_for_source(self, source_row: dict) -> Optional[dict]:
        """`events.event` row + event_source row -> the flat dict for THIS
        SPECIFIC post — used by list_events_by_source, where reconciliation
        needs the post's OWN source_event_key/raw_extraction/timestamps, not
        the merged event's primary/aggregate values (those could belong to a
        DIFFERENT source once several posts share one event)."""
        event = self.events.get(source_row["event_id"])
        if event is None:
            return None
        out = copy.deepcopy(event)
        venue_id = out.get("venue_id")
        venue = self.venues.get(venue_id) if venue_id else None
        out["venue_name"] = venue.get("venue_name") if venue else None
        for field in self._EVENT_SOURCE_FIELDS:
            out[field] = source_row.get(field)
        return out

    def get_event(self, event_id: str) -> Optional[dict]:
        row = self.events.get(event_id)
        return self._merged_view(row) if row else None

    def get_event_by_source(self, source_handle: str, source_shortcode: str) -> Optional[dict]:
        rows = self.list_events_by_source(source_handle, source_shortcode)
        return rows[0] if rows else None

    def list_events_by_source(self, source_handle: str, source_shortcode: str) -> list[dict]:
        """Every event row sharing one post (plans/260806_multi-event-posts.md)
        — get_event_by_source returns at most one row and is unsafe once a
        post can hold several. Post-merge, each returned row's event_id may
        be shared with OTHER sources too (plans/260807_one-event-many-
        posts.md) — that is exactly the point: the same merged event, seen
        through this one post's own provenance."""
        matches = [
            s for s in self.event_sources.values()
            if s.get("source_handle") == source_handle
            and s.get("source_shortcode") == source_shortcode
        ]
        out = [v for v in (self._view_for_source(s) for s in matches) if v is not None]
        out.sort(key=lambda r: (
            r.get("source_event_index") if r.get("source_event_index") is not None else 0,
            r["event_id"],
        ))
        return out

    def list_event_sources(self, event_id: str) -> list[dict]:
        """Every source (announcing post) attached to one event, oldest
        first-seen first — the admin API's `sources[]`
        (plans/260807_one-event-many-posts.md)."""
        rows = [copy.deepcopy(s) for s in self._sources_for(event_id)]
        rows.sort(key=lambda s: (self._sort_dt(s.get("first_seen_at")), s["id"]))
        return rows

    def list_all_event_sources(self) -> list[dict]:
        """Every `events.post_item_source` row, mirroring RdsVenueStore's
        whole-table listing — `list_event_sources(event_id)` only ever
        returns one event's rows, which `scripts.backfill_source_provenance`
        cannot use (plans/260813_backfill-source-provenance.md)."""
        rows = [copy.deepcopy(s) for s in self.event_sources.values()]
        rows.sort(key=lambda s: (self._sort_dt(s.get("first_seen_at")), s["id"]))
        return rows

    def list_all_event_sources_with_context(self) -> list[dict]:
        """Mirrors RdsVenueStore.list_all_event_sources_with_context: every
        source row's `_view_for_source` (post_item content + this source's
        own fields), widened with the source's own `id` — `_view_for_source`
        does not carry it (see that method's own docstring: it exists to
        look like an ordinary "event" dict, keyed by `event_id`)."""
        rows = []
        for source_row in self.event_sources.values():
            view = self._view_for_source(source_row)
            if view is None:
                continue
            view["id"] = source_row["id"]
            rows.append(view)
        rows.sort(key=lambda r: (self._sort_dt(r.get("first_seen_at")), r["id"]))
        return rows

    def update_event_source_provenance(
        self, source_id: str, *, source_uploaded_at=None, source_media_type: Optional[str] = None,
    ) -> bool:
        """Mirrors the real store's COALESCE-guarded UPDATE: fills only a
        currently-NULL column, on the ONE source row named by `source_id` —
        never overwrites a value already present, regardless of what the
        caller passes."""
        self._guard()
        row = self.event_sources.get(source_id)
        if row is None:
            return False
        if row.get("source_uploaded_at") is None:
            row["source_uploaded_at"] = source_uploaded_at
        if row.get("source_media_type") is None:
            row["source_media_type"] = source_media_type
        return True

    def list_events_by_handle(self, source_handle: str) -> list[dict]:
        """Every event with AT LEAST ONE source posted under `source_handle`
        — plans/260811_merge-unresolved-into-resolved-sibling.md's handle
        identity needs to find a resolved sibling regardless of which of an
        event's (possibly several, post-merge) sources carries the matching
        handle, not just its primary (most-recently-seen) one, or a merged
        event whose most recent post came from a DIFFERENT handle would be
        silently unfindable by its earlier, still-real handle."""
        event_ids = {
            s["event_id"] for s in self.event_sources.values()
            if s.get("source_handle") == source_handle
        }
        rows = [self._merged_view(self.events[eid]) for eid in event_ids if eid in self.events]
        rows.sort(key=lambda r: (r.get("starts_at") is None, r.get("starts_at"), r["event_id"]))
        return rows

    def reattach_event_sources(self, from_event_id: str, to_event_id: str) -> None:
        """Re-point every source currently on `from_event_id` at
        `to_event_id` — step 3 of the merge (plans/260807_one-event-many-
        posts.md): provenance must survive the collapse even though the
        duplicate event row does not."""
        self._guard()
        if to_event_id not in self.events:
            raise ValueError(
                f"reattach_event_sources: target event {to_event_id!r} does not exist"
            )
        for s in self.event_sources.values():
            if s["event_id"] == from_event_id:
                s["event_id"] = to_event_id

    def reattach_event_source_by_id(self, source_id: str, to_event_id: str) -> None:
        """Re-point ONE specific source row (by its own `id`) at
        `to_event_id` — mirrors RdsVenueStore.reattach_event_source_by_id,
        plans/260812_event-dedup-fuzzy-title.md §E's reversal primitive."""
        self._guard()
        source = self.event_sources.get(source_id)
        if source is not None:
            source["event_id"] = to_event_id

    def delete_event(self, event_id: str) -> None:
        """Hard delete — ONLY ever correct for a now-SOURCELESS duplicate
        collapsed into a canonical event (reattach_event_sources must run
        first). Mirrors the real FK from event_source to event: raises if
        any source still references this event, exactly as a real DELETE
        would fail instead of silently orphaning provenance."""
        self._guard()
        if event_id not in self.events:
            return
        remaining = self._sources_for(event_id)
        if remaining:
            raise ValueError(
                f"delete_event: {event_id!r} still has {len(remaining)} "
                f"event_source row(s) — reattach them before deleting"
            )
        del self.events[event_id]
        self.event_link_candidates.pop(event_id, None)

    def insert_event(self, fields: dict) -> dict:
        """Splits `fields` into the events.event row and its FIRST
        events.event_source row. Mirrors the real UNIQUE (source_handle,
        source_shortcode, source_event_key) constraint (migration 0026,
        moved here from events.event by migration 0025's two-column
        predecessor): raises rather than silently inserting a duplicate for
        a post/event already extracted. A NULL `source_event_key` never
        collides with another NULL — the same semantics the real Postgres
        UNIQUE constraint gives NULLs — which is what lets several events
        share one post AND lets an extraction_failed placeholder (no content
        to key by) coexist with a confirmed event's row. The service is
        expected to check list_events_by_source/get_event_by_source first
        (the same pattern as every ON-CONFLICT-free write elsewhere in this
        fake) — this is the last-resort guard, not the primary mechanism."""
        self._guard()
        event_id = fields["event_id"]
        source_handle = fields.get("source_handle")
        source_shortcode = fields.get("source_shortcode")
        key = fields.get("source_event_key")
        if key is not None:
            for s in self.event_sources.values():
                if (
                    s.get("source_handle") == source_handle
                    and s.get("source_shortcode") == source_shortcode
                    and s.get("source_event_key") == key
                ):
                    raise ValueError(
                        f"duplicate (source_handle, source_shortcode, source_event_key): "
                        f"{source_handle!r}, {source_shortcode!r}, {key!r}"
                    )
        now = _now()
        event_row = {k: v for k, v in fields.items() if k not in self._EVENT_SOURCE_FIELDS}
        event_row["updated_at"] = now
        # plans/260811_post-items-and-categories.md (migration 0034): the
        # real `events.post_item.post_type` column is `NOT NULL DEFAULT
        # 'event'` — mirrored here for a caller that omits it entirely
        # (every REAL extraction call site sets it explicitly; this default
        # only matters for a pre-existing fixture/test that inserts a bare
        # event dict, which is EXACTLY what the migration's own back-fill
        # does for every row that existed before this column did).
        event_row.setdefault("post_type", "event")
        # plans/260811_expose-time-known.md (migration 0035): the real
        # `events.post_item.time_known` column is `NOT NULL DEFAULT false` —
        # mirrored here for the same reason as post_type's setdefault above
        # (every real extraction call site sets it explicitly; this only
        # matters for a fixture/test that omits it, which must read back
        # False, never a missing key that happens to only work because
        # EventOut's own field default papers over it).
        event_row.setdefault("time_known", False)
        self.events[event_id] = event_row

        source_fields = {k: v for k, v in fields.items() if k in self._EVENT_SOURCE_FIELDS}
        self._event_source_seq += 1
        source_row = {
            "id": f"evsrc_{self._event_source_seq}",
            "event_id": event_id,
            "source_kind": source_fields.get("source_kind", "venue_post"),
            "source_handle": source_handle,
            "source_shortcode": source_shortcode,
            "source_permalink": source_fields.get("source_permalink"),
            "source_event_key": key,
            "source_event_index": source_fields.get("source_event_index"),
            "cover_photo_key": source_fields.get("cover_photo_key"),
            "raw_extraction": source_fields.get("raw_extraction"),
            "first_seen_at": source_fields.get("first_seen_at", now),
            "last_seen_at": source_fields.get("last_seen_at", now),
            # plans/260812_crawl-error-visibility.md §C/§D (migration 0036):
            # nullable/false-defaulted, mirroring the real column defaults.
            "source_media_type": source_fields.get("source_media_type"),
            "source_uploaded_at": source_fields.get("source_uploaded_at"),
            "source_events_truncated": source_fields.get("source_events_truncated", False),
            # plans/260812_event-attribution-and-dates.md §C (migration
            # 0037): nullable, mirroring the real column default.
            "date_interpretation": source_fields.get("date_interpretation"),
        }
        self.event_sources[source_row["id"]] = source_row
        return self.get_event(event_id)

    def _resolve_source_for_update(self, event_id: str, fields: dict, source_fields: dict) -> dict:
        """Which event_source row a partial update's source-level keys
        apply to. Explicit `source_handle`+`source_shortcode` in `fields`
        (every reconciliation write supplies them) resolves unambiguously
        even once an event carries several sources
        (plans/260807_one-event-many-posts.md). Absent both, falls back to
        "the only source" for every PRE-merge call site (every existing
        direct `update_event({"last_seen_at": ...})` poke in this repo's
        tests and routers) — and RAISES rather than guessing when that is
        ambiguous, per this repo's fakes-must-raise convention."""
        handle = fields.get("source_handle")
        shortcode = fields.get("source_shortcode")
        candidates = self._sources_for(event_id)
        if handle is not None and shortcode is not None:
            matches = [
                s for s in candidates
                if s.get("source_handle") == handle and s.get("source_shortcode") == shortcode
            ]
            if not matches:
                raise ValueError(
                    f"update_event: no event_source row for event_id={event_id!r} "
                    f"source_handle={handle!r} source_shortcode={shortcode!r}"
                )
            if len(matches) > 1:
                raise ValueError(
                    f"update_event: ambiguous event_source rows for event_id={event_id!r} "
                    f"source_handle={handle!r} source_shortcode={shortcode!r}"
                )
            return matches[0]
        if len(candidates) == 1:
            return candidates[0]
        raise ValueError(
            f"update_event: cannot route source-level fields {sorted(source_fields)} "
            f"for event_id={event_id!r} without source_handle/source_shortcode — "
            f"{len(candidates)} source(s) exist"
        )

    def update_event(self, event_id: str, fields: dict) -> Optional[dict]:
        """Partial update: only the keys in `fields` change. Returns None
        (rather than raising) when the event does not exist, so a caller
        racing a delete degrades the same way the real UPDATE...WHERE would
        (zero rows affected). Source-level keys (see _EVENT_SOURCE_FIELDS)
        route to the specific event_source row identified by
        `_resolve_source_for_update`; everything else updates the merged
        events.event row directly. Always bumps `updated_at`, matching the
        real store's unconditional `updated_at=now()`."""
        self._guard()
        row = self.events.get(event_id)
        if row is None:
            return None
        event_fields = {k: v for k, v in fields.items() if k not in self._EVENT_SOURCE_FIELDS}
        source_fields = {k: v for k, v in fields.items() if k in self._EVENT_SOURCE_FIELDS}

        if event_fields:
            row.update(event_fields)
        row["updated_at"] = _now()
        self.events[event_id] = row

        if source_fields:
            target = self._resolve_source_for_update(event_id, fields, source_fields)
            target.update(source_fields)

        return self.get_event(event_id)

    def list_events(
        self, *, venue_id: Optional[str] = None, status: Optional[str] = None,
        since=None, until=None,
    ) -> list[dict]:
        out = []
        for row in self.events.values():
            if venue_id is not None and row.get("venue_id") != venue_id:
                continue
            if status is not None and row.get("status") != status:
                continue
            starts_at = row.get("starts_at")
            if since is not None and (starts_at is None or starts_at < since):
                continue
            if until is not None and (starts_at is None or starts_at > until):
                continue
            out.append(self._merged_view(row))
        out.sort(key=lambda r: (r.get("starts_at") is None, r.get("starts_at"), r["event_id"]))
        return out

    def list_events_awaiting_decision(self) -> list[dict]:
        """Every event still awaiting a human decision — mirrors
        RdsVenueStore.list_events_awaiting_decision (plans/260807_review-
        queue-completeness-and-venue-names.md), replacing the old, narrower
        `list_events_pending_location` predicate that excluded every
        venue-post event by construction.

        Union of:
          - `status == "pending_review"` (nobody has confirmed the data), or
          - `venue_id is None and status not in ("rejected", "superseded")`
            (nobody has decided where it happens, and this isn't an event an
            operator already finished with). plans/260810_date-correctness-
            review-reasons-and-path-parity.md §D widened this from
            `source_kind == "promoter_post" and location_resolution is
            None` — see the real DAO's docstring for the full rationale:
            `venue_id` is the real "unresolved" signal regardless of source
            kind, and the old `source_kind`-gated condition was the ONLY
            reason a shared-handle venue post mislabelled `promoter_post`
            ever reached this queue at all, or
          - `status == "extraction_failed"` (plans/260807_date-resolution-
            correctness.md, defect 3) — explicit for BOTH source kinds. An
            extraction-failure placeholder for a venue post always carries a
            real `venue_id` (the venue being crawled), so it never matches
            the second clause on its own.

        `venue_id`/`source_kind` are read off the MERGED view (the primary
        source's kind, plans/260807_one-event-many-posts.md) rather than the
        raw event row for `source_kind`, which no longer stores it at all;
        `venue_id` itself lives on the event row directly, same as the real
        table.
        """
        out = []
        for row in self.events.values():
            view = self._merged_view(row)
            status = view.get("status")
            if status == "pending_review":
                out.append(view)
                continue
            if status == "extraction_failed":
                out.append(view)
                continue
            if view.get("venue_id") is None and status not in ("rejected", "superseded"):
                out.append(view)
        out.sort(key=lambda r: (r.get("first_seen_at"), r["event_id"]))
        return out

    # ── instagram.handle reverse index (plans/260804_instagram-promoter-events.md) ─
    def list_instagram_handles(self) -> dict[str, str]:
        """venue_id -> instagram_handle for every venue with a confirmed
        handle. Mirrors the real store's read of instagram.handle."""
        out = {}
        for vid, row in self.enrichment.get("instagram.handle", {}).items():
            if row.get("deleted_at") is not None:
                continue
            handle = row.get("instagram_handle")
            if handle:
                out[vid] = handle
        return out

    # ── events.promoter_account (plans/260804_instagram-promoter-events.md) ──
    def get_promoter_account(self, handle: str) -> Optional[dict]:
        row = self.promoter_accounts.get(handle)
        return copy.deepcopy(row) if row else None

    def list_promoter_accounts(self, status: Optional[str] = None) -> list[dict]:
        rows = [
            copy.deepcopy(r) for r in self.promoter_accounts.values()
            if status is None or r.get("status") == status
        ]
        rows.sort(key=lambda r: r["handle"])
        return rows

    def upsert_promoter_account(self, handle: str, fields: dict) -> dict:
        """INSERT-or-update by handle: only the keys present in `fields`
        change on an existing row (mirrors update_event's partial-update
        contract); a fresh row gets house defaults for anything omitted, the
        same shape the real store's column defaults provide."""
        self._guard()
        existing = self.promoter_accounts.get(handle)
        now = _now()
        if existing is None:
            row = {
                "handle": handle, "display_name": None, "status": "candidate",
                "discovery_source": "manual", "discovered_from_event_id": None,
                "mention_count": 0, "notes": None, "added_by": None,
                "last_crawled_at": None, "posts_crawled": 0, "events_extracted": 0,
                "created_at": now, "updated_at": now,
            }
            row.update(fields)
        else:
            row = dict(existing)
            row.update(fields)
            row["updated_at"] = now
        self.promoter_accounts[handle] = row
        return copy.deepcopy(row)

    # ── events.crawl_target (plans/260809_scheduled-incremental-instagram-crawl.md) ──
    def get_crawl_target(self, handle: str) -> Optional[dict]:
        row = self.crawl_targets.get(handle)
        return copy.deepcopy(row) if row else None

    def list_crawl_targets(
        self, *, enabled: Optional[bool] = None, kind: Optional[str] = None,
    ) -> list[dict]:
        rows = [
            copy.deepcopy(r) for r in self.crawl_targets.values()
            if (enabled is None or r.get("enabled") == enabled)
            and (kind is None or r.get("kind") == kind)
        ]
        rows.sort(key=lambda r: r["handle"])
        return rows

    def upsert_crawl_target(self, handle: str, fields: dict) -> dict:
        """CREATE-only, mirroring the real `RdsVenueStore.upsert_crawl_target`
        (see its docstring for the production incident that restricted this):
        `kind` and `cron` are NOT NULL with no database default (migration
        0030_crawl_target), and a real `INSERT ... ON CONFLICT DO UPDATE`
        validates NOT NULL against the fully-constructed insert tuple BEFORE
        it ever evaluates `ON CONFLICT` — on EVERY call, not just when the row
        is genuinely new. A bare dict-update fake that only checked this on
        first insert would happily accept a partial "update" a real Postgres
        rejects, exactly the "models the happy path, not the constraints"
        trap this file has hit twice before (CLAUDE.md) — and did a third
        time here, in production, before this method was hardened to match.
        Use `update_crawl_target` for a partial update of a row known to
        already exist.

        Also enforces `ck_crawl_target_kind` (`kind IN ('venue', 'promoter')`)
        on every call, mirroring the real CHECK constraint.
        """
        self._guard()
        existing = self.crawl_targets.get(handle)
        now = _now()
        if "kind" in fields and fields["kind"] not in self._CRAWL_TARGET_KINDS:
            raise ValueError(
                f"crawl_target.kind must be one of {self._CRAWL_TARGET_KINDS}, "
                f"got {fields['kind']!r}"
            )
        if not fields.get("kind"):
            raise ValueError(
                "crawl_target.kind is NOT NULL (migration 0030_crawl_target) -- "
                "upsert_crawl_target is CREATE-only and requires it on every call, "
                "even against an existing row; use update_crawl_target for a "
                "partial update."
            )
        if not fields.get("cron"):
            raise ValueError(
                "crawl_target.cron is NOT NULL (migration 0030_crawl_target) -- "
                "upsert_crawl_target is CREATE-only and requires it on every call, "
                "even against an existing row; use update_crawl_target for a "
                "partial update."
            )
        if existing is None:
            kind = fields["kind"]
            cron = fields["cron"]
            row = {
                "handle": handle, "kind": kind, "enabled": True, "cron": cron,
                "timezone": "America/Recife", "crawl_reels": False,
                # Defaults TRUE — a scheduled crawl replaces a manual archive
                # run that already classifies; FALSE is an explicit per-target
                # opt-out, never a silent inherited cheap mode.
                "classify_images": True,
                "initial_lookback": None, "results_limit": None,
                # NULL means "use settings.crawl_default_seed_results_limit"
                # — the SEPARATE, larger cap applied only on a run whose
                # relevant cursor is still null (migration 0031).
                "seed_results_limit": None,
                # NULL means "fall through": reels_results_limit ->
                # results_limit -> the settings default; reels_seed_
                # results_limit -> seed_results_limit -> its default
                # (migration 0032). Posts never reads either column.
                "reels_results_limit": None, "reels_seed_results_limit": None,
                "cursor_posts_at": None, "cursor_reels_at": None,
                "last_run_at": None, "last_run_results": 0,
                "last_run_cost_usd": None, "consecutive_failures": 0,
                "notes": None,
                # plans/260810_stream-dedupe-and-venue-attribution.md §B
                # (migration 0033): NULL until a run whose reels stream
                # actually executed writes both, together, never zero by
                # default.
                "last_run_reels_fetched": None, "last_run_reels_new": None,
                # plans/260812_crawl-error-visibility.md §B (migration 0036):
                # NULL until a run actually fails — never invented.
                "last_failure_kind": None, "last_failure_at": None,
                # plans/260813_dormant-vs-broken-targets.md §A (migration
                # 0038): a target has no evidence of being dormant until
                # its first run — mirrors the real column's
                # `NOT NULL DEFAULT false`.
                "posts_dormant": False,
                # plans/260814_seeded-state-and-config-validation.md §A
                # (migration 0041): NULL until a run whose reels stream
                # reaches a trustworthy conclusion (success or empty) writes
                # it — never invented, mirrors `last_failure_kind`'s own
                # "NULL until something real happens" convention.
                "reels_seeded_at": None,
                "created_at": now, "updated_at": now,
            }
            row.update({k: v for k, v in fields.items() if k != "handle"})
        else:
            row = dict(existing)
            row.update(fields)
            row["updated_at"] = now
        self.crawl_targets[handle] = row
        return copy.deepcopy(row)

    def update_crawl_target(self, handle: str, fields: dict) -> Optional[dict]:
        """Plain partial update, mirroring the real store's method of the
        same name: NEVER creates a row (a missing handle is a safe no-op
        returning None, exactly like `UPDATE ... WHERE handle=:h` affecting
        zero rows), so it carries none of `upsert_crawl_target`'s NOT NULL
        requirements. Still enforces the CHECK constraint on `kind` if the
        caller is changing it."""
        self._guard()
        existing = self.crawl_targets.get(handle)
        if existing is None:
            return None
        if "kind" in fields and fields["kind"] not in self._CRAWL_TARGET_KINDS:
            raise ValueError(
                f"crawl_target.kind must be one of {self._CRAWL_TARGET_KINDS}, "
                f"got {fields['kind']!r}"
            )
        row = dict(existing)
        row.update(fields)
        row["updated_at"] = _now()
        self.crawl_targets[handle] = row
        return copy.deepcopy(row)

    def delete_crawl_target(self, handle: str) -> bool:
        """Hard delete — no soft-delete column on this table (see migration
        0030's downgrade note). Returns True iff a row existed."""
        self._guard()
        return self.crawl_targets.pop(handle, None) is not None

    # ── events.event_venue_link_candidate (plans/260804_instagram-promoter-events.md) ─
    def replace_event_venue_link_candidates(self, event_id: str, candidates: list[dict]) -> None:
        """Replace the whole ranked list for one event — a later crawl of the
        same post recomputes the ladder from scratch, so the old ranking must
        not linger alongside a new one.

        Enforces the REAL, non-deferrable FK migration 0024_promoter_accounts
        declares (`event_id text NOT NULL REFERENCES events.event(event_id)`):
        raises if `event_id` is not already a row in `self.events`. Without
        this the fake had zero referential integrity and could not catch a
        caller writing candidates before the event row it references is
        committed — exactly the bug plans/260806_venue-post-multi-event.md's
        review caught (a real Postgres ForeignKeyViolation on every
        first-time QUEUED/auto-linked event)."""
        self._guard()
        if event_id not in self.events:
            raise ValueError(
                f"event_venue_link_candidate FK violation: event_id {event_id!r} "
                f"does not exist in events.event yet"
            )
        self.event_link_candidates[event_id] = [dict(c) for c in candidates]

    def list_event_venue_link_candidates(self, event_id: str) -> list[dict]:
        rows = self.event_link_candidates.get(event_id, [])
        return [copy.deepcopy(r) for r in sorted(rows, key=lambda r: r["rank"])]

    # ── events.event_merge_suggestion (plans/260812_event-dedup-fuzzy-title.md
    # §C/§E, migration 0038) ────────────────────────────────────────────────
    def create_event_merge_suggestion(self, fields: dict) -> dict:
        row = copy.deepcopy(fields)
        row.setdefault("decision", "pending")
        row.setdefault("moved_source_ids", None)
        row.setdefault("absorbed_status_before", None)
        row.setdefault("decided_at", None)
        row.setdefault("decided_by", None)
        self.event_merge_suggestions[row["suggestion_id"]] = row
        return copy.deepcopy(row)

    def get_event_merge_suggestion(self, suggestion_id: str) -> Optional[dict]:
        row = self.event_merge_suggestions.get(suggestion_id)
        return copy.deepcopy(row) if row else None

    def list_event_merge_suggestions(
        self, *, event_id: Optional[str] = None, candidate_event_id: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> list[dict]:
        out = []
        for row in self.event_merge_suggestions.values():
            if event_id is not None and event_id not in (row.get("event_id"), row.get("candidate_event_id")):
                continue
            if candidate_event_id is not None and row.get("candidate_event_id") != candidate_event_id:
                continue
            if decision is not None and row.get("decision") != decision:
                continue
            out.append(copy.deepcopy(row))
        out.sort(key=lambda r: (r.get("created_at") or "", r["suggestion_id"]))
        return out

    def update_event_merge_suggestion(self, suggestion_id: str, fields: dict) -> Optional[dict]:
        row = self.event_merge_suggestions.get(suggestion_id)
        if row is None:
            return None
        row.update(fields)
        return copy.deepcopy(row)

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

    def list_instagram_sources(self):
        out = {}
        for row in self.enrichment.get("instagram.handle", {}).values():
            if row.get("deleted_at"):
                continue
            out[row.get("source")] = out.get(row.get("source"), 0) + 1
        return list(out.items())

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

    def block_venue(self, user_pseudo, venue_id) -> bool:
        """Mirrors the real store's single-transaction block_venue: upserts the
        block row and, if an active favorite exists for the same pair,
        soft-deletes it — returning whether that happened."""
        self._guard()
        self.blocked_venues[(user_pseudo, venue_id)] = {"deleted_at": None, "updated_at": _now()}
        fav = self.favorites.get((user_pseudo, venue_id))
        if fav is not None and fav.get("deleted_at") is None:
            fav["deleted_at"] = _now()
            fav["updated_at"] = _now()
            return True
        return False

    def soft_delete_block(self, user_pseudo, venue_id) -> None:
        self._guard()
        row = self.blocked_venues.get((user_pseudo, venue_id))
        if row is not None:
            row["deleted_at"] = _now()

    def get_block(self, user_pseudo, venue_id) -> Optional[dict]:
        return self.blocked_venues.get((user_pseudo, venue_id))

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

    # ── erasure (account deletion) ────────────────────────────────────────────
    def list_user_hot_like_venue_ids(self, user_pseudo) -> list[str]:
        """DISTINCT venues this user hot-liked. One user legitimately holds
        several event rows per venue (the unique index is per business period),
        so this must dedupe or the caller would srem the same set repeatedly."""
        self._guard()
        return sorted({
            e["venue_id"] for e in self.hot_like_events
            if e.get("user_pseudo") == user_pseudo
        })

    def purge_user_engagement(self, user_pseudo) -> dict:
        """HARD-delete every row bearing this pseudonym, across all three
        engagement stores. Unlike `soft_delete_favorite`, nothing is left behind:
        a surviving pseudonymized row would make this a deactivation."""
        self._guard()
        fav_keys = [k for k in self.favorites if k[0] == user_pseudo]
        for key in fav_keys:
            del self.favorites[key]

        hot_before = len(self.hot_like_events)
        self.hot_like_events = [
            e for e in self.hot_like_events if e.get("user_pseudo") != user_pseudo
        ]
        # Keep the dedup index consistent with the rows, or a later re-like of the
        # same (user, venue, day) would be silently suppressed as a retry.
        self._hot_like_keys = {
            k for k in self._hot_like_keys if k[0] != user_pseudo
        }

        session_rows = {s for s in self.app_sessions if s[0] == user_pseudo}
        self.app_sessions -= session_rows

        block_keys = [k for k in self.blocked_venues if k[0] == user_pseudo]
        for key in block_keys:
            del self.blocked_venues[key]

        return {
            "favorites": len(fav_keys),
            "hot_like_events": hot_before - len(self.hot_like_events),
            "app_sessions": len(session_rows),
            "blocked_venues": len(block_keys),
        }

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
            "blocked": list(self.blocked_venues.keys()),
            "hot": self.hot_like_events,
            "act": [[up, str(d)] for up, d in self.app_sessions],
        }, default=str)  # business_period / dates aren't natively JSON-serializable
        return raw in blob
