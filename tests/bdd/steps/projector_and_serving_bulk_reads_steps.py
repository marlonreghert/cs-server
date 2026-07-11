"""Behave steps for tests/bdd/persistence/projector-and-serving-bulk-reads.feature.

Covers the P1-P5 bulk-reads refactor (plans/260710_projector-and-serving-bulk-reads.md):
the projector rebuild moves from ~18 per-venue SQL queries to ~12 bulk queries
per cycle, and `/v1/venues/nearby` moves from N GETs-per-venue to bounded
per-key-family MGETs — with the Redis projection content and the nearby
response body required to stay byte-equivalent.

Two equivalence techniques are used, deliberately different in what they prove:
- The "per-key reads" comparison (`_PerKeyDaoProxy`) reconstructs the OLD
  fetch pattern using the still-present, unmodified single-item DAO getters,
  and asserts VenueHandler produces the same output through them as it does
  through the new bulk getters. This isolates "bulk fetch vs per-key fetch"
  as the only variable — it does NOT re-verify `_transform`'s field-composition
  logic (unchanged by this plan, exercised elsewhere).
- The "as the RDS state dictates" comparison independently reconstructs
  expected Redis values straight from the RDS payloads (via the same model
  classes the projector uses), not by re-running production code.

Bounded round-trips are proven by wrapping the fake stores with counting
adapters and comparing counts across two different venue-count magnitudes
within the same scenario, not by asserting a magic number alone.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from behave import given, when, then  # type: ignore[import-untyped]

from app.dao.redis_venue_dao import RedisVenueDAO
from app.db.geo_redis_client import GeoRedisClient
from app.handlers.venue_handler import VenueHandler
from app.metrics import VENUE_SERVE_LIVE_BUSYNESS_TOTAL
from app.models import Venue
from app.models.instagram import InstagramPost, VenueInstagram, VenueInstagramPosts
from app.models.menu import VenueMenuData, VenueMenuPhotos
from app.models.opening_hours import OpeningHours
from app.models.venue import DayInfo, DayInfoV2, OpenCloseDetail
from app.models.venue_review import VenueReviews
from app.models.vibe_attributes import VibeAttributes
from app.models.vibe_profile import VenueVibeProfile
from app.models.week_raw import WeekRawDay
from app.services.redis_projection_service import RedisProjectionService, _REBUILD_MODELS

_LAT, _LNG, _RADIUS = -8.05, -34.88, 5.0

_FULL_VENUE_IDS = ["v1", "v2", "v3"]
_NO_HOURS_VENUE_ID = "v4"
_INELIGIBLE_VENUE_ID = "v5"
_SOFT_DELETED_VIBE_VENUE_ID = "v6"


def _venue(vid: str, name: str = "Bar X", vtype: str = "BAR") -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=name,
        venue_address=f"{vid} Rua X, 100", venue_lat=_LAT, venue_lng=_LNG,
        venue_type=vtype, price_level=2, rating=4.5, reviews=321,
    )


def _weekly_day(day_int: int, with_hours: bool = True) -> WeekRawDay:
    day_info = None
    if with_hours:
        day_info = DayInfo(
            day_int=day_int, day_max=80, day_mean=40,
            venue_open_close_v2=DayInfoV2(h24=[
                OpenCloseDetail(opens=11, closes=0, opens_minutes=0, closes_minutes=0)
            ]),
        )
    return WeekRawDay(day_int=day_int, day_raw=[40] * 24, day_info=day_info)


def _seed_full_venue(context, vid: str, name: str) -> None:
    """A servable venue with EVERY enrichment family populated, weekly
    forecasts for all 7 days, and a live forecast — the Background's
    "3 servable venues carrying every enrichment family" fixture."""
    store = context.rds_store
    store.upsert_venue(_venue(vid, name))

    va = VibeAttributes(venue_id=vid, google_place_id=f"place_{vid}", google_primary_type="bar")
    store.upsert_enrichment(
        "google_places.vibe_attributes", vid, va.model_dump(mode="json"),
        history=False, promoted={"google_primary_type": "bar", "google_place_id": f"place_{vid}"},
    )
    oh = OpeningHours(venue_id=vid, weekday_descriptions=[f"{d}: 11:00-00:00" for d in range(7)])
    store.upsert_enrichment("google_places.opening_hours", vid, oh.model_dump(mode="json"), history=False)
    store.upsert_enrichment(
        "google_places.photos", vid,
        {"photos": [{"url": f"https://p/{vid}_1.jpg", "author_name": "A"}]}, history=False,
    )
    reviews = VenueReviews(venue_id=vid, reviews=[
        {"author_name": "A", "rating": 5, "text": "great", "relative_time": "today"}
    ])
    store.upsert_enrichment("google_places.reviews", vid, reviews.model_dump(mode="json"), history=False)
    ig = VenueInstagram(venue_id=vid, instagram_handle=f"ig_{vid}", instagram_url=f"https://ig/{vid}",
                         status="found", confidence_score=0.9)
    store.upsert_enrichment("instagram.handle", vid, ig.model_dump(mode="json"),
                             history=False, promoted={"instagram_handle": f"ig_{vid}"})
    # Built via the model class (not a raw dict) so `scraped_at` (a
    # `default_factory=datetime.utcnow` field) is baked into the stored
    # payload once — a raw dict missing that field would make every
    # independent `model_validate(payload)` call (the projector's, and this
    # scenario's own "as RDS dictates" comparison) regenerate a DIFFERENT
    # timestamp, a false equivalence mismatch unrelated to the refactor.
    posts = VenueInstagramPosts(venue_id=vid, instagram_handle=f"ig_{vid}",
                                 posts=[InstagramPost(caption="hi")])
    store.upsert_enrichment("instagram.posts", vid, posts.model_dump(mode="json"), history=False)
    mp = VenueMenuPhotos(venue_id=vid, photos=[
        {"photo_id": "p1", "s3_url": "https://s3/m.jpg", "s3_key": "m.jpg"}
    ])
    store.upsert_enrichment("venues.menu_photos", vid, mp.model_dump(mode="json"), history=False)
    md = VenueMenuData(venue_id=vid, sections=[
        {"name": "Drinks", "items": [{"name": "Beer", "prices": [{"price": 12}]}]}
    ])
    store.upsert_enrichment("venues.menu_data", vid, md.model_dump(mode="json"), history=False)
    vp = VenueVibeProfile(venue_id=vid, top_vibes=["animado"], overall_confidence=0.9)
    store.upsert_enrichment("venues.vibe_profile", vid, vp.model_dump(mode="json"), history=False)

    for day_int in range(7):
        wk = _weekly_day(day_int, with_hours=True)
        store.upsert_enrichment("besttime.weekly_forecast", f"{vid}#{day_int}", wk.model_dump(mode="json"),
                                 history=False)

    store.upsert_live_forecast(vid, {
        "status": "OK",
        "venue_info": {"venue_id": vid, "venue_current_gmttime": datetime.now(timezone.utc).isoformat()},
        "analysis": {"venue_live_busyness": 55, "venue_live_busyness_available": True},
    })


def _run_rebuild(context):
    context.rebuild_summary = context.redis_projection_service.rebuild_redis_from_rds()
    context._projection_rebuilt = True
    return context.rebuild_summary


def _ensure_projected(context):
    if not getattr(context, "_projection_rebuilt", False):
        _run_rebuild(context)


# ── counting adapters (bounded query / round-trip proof) ──────────────────────
class _CountingRdsStore:
    """Wraps the RDS fake, counting calls to the bulk-read methods that replace
    the projector's former per-venue RDS loop (P1). The serving-view listing,
    the geo-excluded observability count, and the active/deprecated reconcile
    listing are pre-existing O(1) cycle-level queries, orthogonal to the
    per-venue N+1 this plan fixes, and are deliberately NOT counted here."""

    _BULK_METHODS = {"get_venues_by_ids", "get_enrichment_bulk", "get_weekly_bulk", "get_live_bulk"}

    def __init__(self, inner):
        self._inner = inner
        self.bulk_query_count = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in self._BULK_METHODS and callable(attr):
            def _wrapper(*args, **kwargs):
                self.bulk_query_count += 1
                return attr(*args, **kwargs)
            return _wrapper
        return attr


class _CountingPipeline:
    def __init__(self, inner_pipe, counter: "_CountingRedisClient"):
        self._inner = inner_pipe
        self._counter = counter

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name == "execute":
            def _wrapper(*args, **kwargs):
                self._counter.round_trips += 1
                return attr(*args, **kwargs)
            return _wrapper
        return attr


class _CountingRedisClient:
    """Wraps the raw fakeredis client, counting each individual command as one
    Redis round-trip. A `pipeline().execute()` counts as ONE round-trip for
    however many commands it batches (matching real Redis pipelining); queuing
    commands on the pipeline object itself does not round-trip."""

    _COMMANDS = {"get", "mget", "georadius", "set", "setex", "delete", "zrem", "keys", "scan", "ping"}

    def __init__(self, inner):
        self._inner = inner
        self.round_trips = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name == "pipeline":
            def _make_pipeline(*args, **kwargs):
                return _CountingPipeline(attr(*args, **kwargs), self)
            return _make_pipeline
        if name in self._COMMANDS and callable(attr):
            def _wrapper(*args, **kwargs):
                self.round_trips += 1
                return attr(*args, **kwargs)
            return _wrapper
        return attr


class _PerKeyDaoProxy:
    """Wraps a RedisVenueDAO so its BULK (P2) methods resolve via a per-item
    loop over the corresponding single-item getter — the "per-key reads" path
    the bulk methods replaced. VenueHandler run against this proxy reconstructs
    exactly what the pre-refactor per-venue GET loop produced, using the real
    (unmodified) single-item DAO methods as ground truth rather than a
    hand-rewritten reimplementation of `_transform`."""

    def __init__(self, dao):
        self._dao = dao

    def __getattr__(self, name):
        return getattr(self._dao, name)

    def get_live_forecasts_bulk(self, ids):
        return {vid: v for vid in ids if (v := self._dao.get_live_forecast(vid)) is not None}

    def get_week_raw_forecasts_bulk(self, ids, day_int):
        return {
            vid: v for vid in ids
            if (v := self._dao.get_week_raw_forecast(vid, day_int)) is not None
        }

    def get_vibe_attributes_bulk(self, ids):
        return {vid: v for vid in ids if (v := self._dao.get_vibe_attributes(vid)) is not None}

    def get_venue_photos_bulk(self, ids):
        return {vid: v for vid in ids if (v := self._dao.get_venue_photos(vid))}

    def get_opening_hours_bulk(self, ids):
        return {vid: v for vid in ids if (v := self._dao.get_opening_hours(vid)) is not None}

    def get_venue_instagram_bulk(self, ids):
        return {vid: v for vid in ids if (v := self._dao.get_venue_instagram(vid)) is not None}

    def get_venue_vibe_profile_bulk(self, ids):
        return {vid: v for vid in ids if (v := self._dao.get_venue_vibe_profile(vid)) is not None}


class _SlowVenueDao:
    """Wraps a real venue DAO, blocking `list_all_venues` on a threading.Event
    (signalling a second `started` event once inside the block) so a step can
    deterministically prove /health stays responsive while this call is still
    blocked — no fixed sleeps."""

    def __init__(self, inner, started_event: threading.Event, release_event: threading.Event):
        self._inner = inner
        self._started = started_event
        self._release = release_event

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_all_venues(self):
        self._started.set()
        self._release.wait(timeout=5)
        return self._inner.list_all_venues()


def _minified_by_id(context, dao) -> dict:
    result = VenueHandler(dao).get_venues_nearby(_LAT, _LNG, _RADIUS, verbose=False)
    return {v.venue_id: v.model_dump(mode="json", by_alias=True) for v in result}


# ── Background ──────────────────────────────────────────────────────────────
@given("an RDS state with 3 servable venues carrying every enrichment family, weekly forecasts for all 7 days, and a live forecast")
def step_seed_full_venues(context):
    for vid in _FULL_VENUE_IDS:
        _seed_full_venue(context, vid, f"Bar {vid}")


@given("1 servable venue with no Google opening hours and no live forecast")
def step_seed_no_hours_venue(context):
    vid = _NO_HOURS_VENUE_ID
    store = context.rds_store
    store.upsert_venue(_venue(vid, "Bar Sem Horario"))
    # Weekly forecast IS present (needed for the BestTime hours-derivation
    # fallback); opening_hours enrichment and live forecast are deliberately
    # absent.
    for day_int in range(7):
        wk = _weekly_day(day_int, with_hours=(day_int != 6))  # Sunday closed
        store.upsert_enrichment("besttime.weekly_forecast", f"{vid}#{day_int}", wk.model_dump(mode="json"),
                                 history=False)


@given("1 active venue excluded from the serving view by the eligibility block-list")
def step_seed_ineligible_venue(context):
    vid = _INELIGIBLE_VENUE_ID
    context.rds_store.upsert_venue(_venue(vid, "Igreja", vtype="CHURCH"))
    # A stale prior projection, so the rebuild's removal path (not just
    # "never projected") is exercised.
    context.redis_only_dao.upsert_venue(_venue(vid, "Igreja (stale copy)", vtype="CHURCH"))


@given("1 venue whose vibe-attributes enrichment is soft-deleted")
def step_seed_soft_deleted_vibe_venue(context):
    vid = _SOFT_DELETED_VIBE_VENUE_ID
    store = context.rds_store
    store.upsert_venue(_venue(vid, "Bar Parcialmente Deletado"))
    va = VibeAttributes(venue_id=vid, google_place_id=f"place_{vid}", google_primary_type="bar")
    store.upsert_enrichment(
        "google_places.vibe_attributes", vid, va.model_dump(mode="json"),
        history=False, promoted={"google_primary_type": "bar", "google_place_id": f"place_{vid}"},
    )
    store.soft_delete_enrichment("google_places.vibe_attributes", vid, history=False)


# ── When: rebuild ───────────────────────────────────────────────────────────
@when("the Redis projection rebuild runs")
def step_rebuild_runs(context):
    counting_store = _CountingRdsStore(context.rds_store)
    svc = RedisProjectionService(
        redis_only_dao=context.redis_only_dao,
        rds_store=counting_store,
        eligibility_rule_service=context.redis_projection_service.eligibility_rule_service,
    )
    context.rebuild_summary = svc.rebuild_redis_from_rds()
    context.rebuild_query_count = counting_store.bulk_query_count
    context._projection_rebuilt = True


# ── Then: projection equivalence ───────────────────────────────────────────
@then("every servable venue must be projected with the same Redis keys and serialized values as the RDS state dictates")
def step_projection_matches_rds(context):
    store = context.rds_store
    dao = context.redis_only_dao
    servable_ids = set(store.list_servable_venue_ids())
    assert _INELIGIBLE_VENUE_ID not in servable_ids

    for vid in servable_ids:
        row = store.get_venue(vid)
        assert row is not None
        projected = dao.get_venue(vid)
        assert projected is not None, f"{vid} was not projected"
        assert projected.venue_name == row["venue_name"]
        assert projected.venue_address == store.get_address(vid)["raw_text"]

    for vid in _FULL_VENUE_IDS:
        for table_key, (model_cls, _setter, _deleter) in _REBUILD_MODELS.items():
            rec = store.get_enrichment(table_key, vid)
            assert rec is not None and rec["deleted_at"] is None
            expected = model_cls.model_validate(rec["payload"])
            getter_name = {
                "google_places.vibe_attributes": "get_vibe_attributes",
                "google_places.opening_hours": "get_opening_hours",
                "google_places.reviews": "get_venue_reviews",
                "instagram.handle": "get_venue_instagram",
                "instagram.posts": "get_venue_ig_posts",
                "venues.menu_photos": "get_venue_menu_photos",
                "venues.menu_data": "get_venue_menu_data",
                "venues.vibe_profile": "get_venue_vibe_profile",
            }[table_key]
            actual = getattr(dao, getter_name)(vid)
            assert actual is not None, f"{table_key} missing in Redis for {vid}"
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json"), (
                f"{table_key} for {vid} does not match the RDS payload"
            )
        for day_int in range(7):
            expected_day = WeekRawDay.model_validate(
                store.get_enrichment("besttime.weekly_forecast", f"{vid}#{day_int}")["payload"]
            )
            actual_day = dao.get_week_raw_forecast(vid, day_int)
            assert actual_day is not None
            assert actual_day.model_dump(mode="json") == expected_day.model_dump(mode="json")
        expected_live = store.get_live_forecast(vid)["payload"]
        actual_live = dao.get_live_forecast(vid)
        assert actual_live is not None
        assert actual_live.analysis.venue_live_busyness == expected_live["analysis"]["venue_live_busyness"]


@then("the soft-deleted enrichment must not be projected")
def step_soft_deleted_not_projected(context):
    assert context.redis_only_dao.get_vibe_attributes(_SOFT_DELETED_VIBE_VENUE_ID) is None


@then("the ineligible venue must be removed from the serving projection")
def step_ineligible_removed(context):
    assert context.redis_only_dao.get_venue(_INELIGIBLE_VENUE_ID) is None


@then("the rebuild summary must report the same venue, enrichment, live, removed, and error counts as before the change")
def step_summary_counts(context):
    summary = context.rebuild_summary
    # 5 servable venues projected: the 3 full ones + the no-hours venue + the
    # soft-deleted-vibe venue (ineligible v5 excluded).
    assert summary["venues"] == 5, summary
    # 8 enrichment tables x 3 fully-enriched venues; the no-hours venue has
    # none, the soft-deleted-vibe venue's only enrichment row is soft-deleted
    # (not counted).
    assert summary["enrichment"] == 24, summary
    # Live forecasts: only the 3 fully-enriched venues have one.
    assert summary["live"] == 3, summary
    # The stale pre-seeded ineligible-venue copy is reconciled out.
    assert summary["removed"] == 1, summary
    assert summary["errors"] == 0, summary


# ── Then: bounded projector queries ────────────────────────────────────────
@then("the number of RDS queries issued must not exceed 12")
def step_bounded_rds_queries(context):
    assert context.rebuild_query_count <= 12, context.rebuild_query_count


@then("the number of RDS queries must be the same regardless of how many servable venues exist")
def step_rds_query_count_invariant(context):
    first_count = context.rebuild_query_count
    for i in range(50):
        context.rds_store.upsert_venue(_venue(f"scale_rds_{i}", f"Scale Bar {i}"))

    counting_store = _CountingRdsStore(context.rds_store)
    svc = RedisProjectionService(
        redis_only_dao=context.redis_only_dao,
        rds_store=counting_store,
        eligibility_rule_service=context.redis_projection_service.eligibility_rule_service,
    )
    svc.rebuild_redis_from_rds()

    assert counting_store.bulk_query_count == first_count, (
        f"RDS query count grew with venue count: {first_count} -> {counting_store.bulk_query_count}"
    )


# ── One bad venue row ───────────────────────────────────────────────────────
@given("one servable venue whose RDS row cannot be parsed")
def step_corrupt_one_venue(context):
    # Corrupt `rating` (not read by eligibility/geo-fence evaluation, so the
    # serving-view listing itself stays healthy) so Venue.model_validate fails
    # for exactly this one venue during reconstruction.
    context.rds_store.venues[_FULL_VENUE_IDS[0]]["rating"] = "not-a-number"
    context._corrupted_venue_id = _FULL_VENUE_IDS[0]


@then("the rebuild summary must count 1 error")
def step_summary_one_error(context):
    assert context.rebuild_summary["errors"] == 1, context.rebuild_summary


@then("every other servable venue must still be projected")
def step_other_venues_still_projected(context):
    servable_ids = set(context.rds_store.list_servable_venue_ids())
    corrupted = context._corrupted_venue_id
    assert corrupted in servable_ids
    assert context.redis_only_dao.get_venue(corrupted) is None, (
        "the corrupted venue must NOT be projected"
    )
    for vid in servable_ids - {corrupted}:
        assert context.redis_only_dao.get_venue(vid) is not None, (
            f"{vid} should still be projected despite {corrupted}'s parse failure"
        )


# ── Nearby serving ──────────────────────────────────────────────────────────
@given("the serving projection has been rebuilt")
def step_projection_rebuilt_given(context):
    _run_rebuild(context)


@given("the serving projection holds a live forecast older than the freshness window for one venue")
def step_stale_live_projection(context):
    vid = _FULL_VENUE_IDS[0]
    stale_gmttime = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    context.rds_store.upsert_live_forecast(vid, {
        "status": "OK",
        "venue_info": {"venue_id": vid, "venue_current_gmttime": stale_gmttime},
        "analysis": {"venue_live_busyness": 80, "venue_live_busyness_available": True},
    })
    _run_rebuild(context)
    context.stale_live_venue_id = vid


@when("a client requests nearby venues around the seeded coordinates")
def step_request_nearby(context):
    _ensure_projected(context)
    context.metric_baseline = {
        outcome: VENUE_SERVE_LIVE_BUSYNESS_TOTAL.labels(outcome=outcome)._value.get()
        for outcome in ("served", "suppressed_stale", "suppressed_unparseable")
    }
    context.nearby_minified_actual = VenueHandler(context.redis_only_dao).get_venues_nearby(
        _LAT, _LNG, _RADIUS, verbose=False
    )
    context.nearby_minified_by_id = {v.venue_id: v for v in context.nearby_minified_actual}


@then("the minified response body must be byte-identical to the response produced by per-key reads")
def step_nearby_byte_identical(context):
    actual = {vid: v.model_dump(mode="json", by_alias=True) for vid, v in context.nearby_minified_by_id.items()}
    reference = _minified_by_id(context, _PerKeyDaoProxy(context.redis_only_dao))
    assert actual == reference, "bulk-read nearby response diverges from the per-key-read reference"


@then('the venue without Google hours must carry hours derived from its BestTime weekly forecast with hours source "besttime"')
def step_besttime_hours_fallback(context):
    venue = context.nearby_minified_by_id[_NO_HOURS_VENUE_ID]
    assert venue.hours_source == "besttime", venue.hours_source
    assert venue.opening_hours, "expected non-empty derived opening hours"


@then("that venue must be served without a live busyness value")
def step_stale_live_suppressed(context):
    venue = context.nearby_minified_by_id[context.stale_live_venue_id]
    assert venue.venue_live_busyness is None, venue.venue_live_busyness


@then('the metric "venue_serve_live_busyness_total" outcome "{outcome}" must be incremented')
def step_metric_incremented(context, outcome):
    current = VENUE_SERVE_LIVE_BUSYNESS_TOTAL.labels(outcome=outcome)._value.get()
    delta = current - context.metric_baseline[outcome]
    assert delta >= 1, f"expected {outcome} to increase by >=1, got +{delta}"


@then("the number of Redis round-trips for the request must not grow with the number of venues returned")
def step_bounded_redis_round_trips(context):
    _ensure_projected(context)

    def _count_round_trips() -> int:
        counting = _CountingRedisClient(context.fake_redis)
        geo = GeoRedisClient(counting)
        dao = RedisVenueDAO(geo)
        VenueHandler(dao).get_venues_nearby(_LAT, _LNG, _RADIUS, verbose=False)
        return counting.round_trips

    small_n = _count_round_trips()

    for i in range(20):
        vid = f"scale_nearby_{i}"
        context.redis_only_dao.upsert_venue(_venue(vid, f"Scale Bar {i}"))

    large_n = _count_round_trips()

    assert large_n == small_n, (
        f"Redis round-trips grew with venue count: {small_n} (small N) -> {large_n} (large N)"
    )


# ── Event-loop responsiveness ───────────────────────────────────────────────
@given("an admin venue-inventory listing is executing against a slow store")
def step_slow_admin_listing(context):
    # main.py registers /health directly on the production app; the BDD
    # harness's ad-hoc FastAPI app only mounts the routers, so add the same
    # trivial route here.
    if not any(r.path == "/health" for r in context.app.routes):
        context.app.get("/health")(lambda: {"status": "healthy"})

    started = threading.Event()
    release = threading.Event()
    slow_dao = _SlowVenueDao(context.venue_dao, started, release)
    context.container.venue_dao = slow_dao
    context.container.pipeline_repository = slow_dao

    def _call_inventory():
        context.client.get("/admin/venues/inventory")

    thread = threading.Thread(target=_call_inventory, daemon=True)
    thread.start()
    assert started.wait(timeout=2), "slow inventory listing never started"
    context._slow_listing_thread = thread
    context._slow_listing_release = release


@when("a client requests the health endpoint during the listing")
def step_request_health_during_listing(context):
    t0 = time.monotonic()
    context.health_response = context.client.get("/health")
    context.health_elapsed = time.monotonic() - t0


@then("the health endpoint must respond without waiting for the listing to finish")
def step_health_responsive(context):
    try:
        assert context.health_response.status_code == 200
        assert context.health_elapsed < 1.0, (
            f"/health took {context.health_elapsed:.2f}s — appears to have waited "
            f"on the blocking listing"
        )
    finally:
        context._slow_listing_release.set()
        context._slow_listing_thread.join(timeout=5)
