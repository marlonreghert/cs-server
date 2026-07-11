"""Contract test shared by the in-memory fake AND the real RdsVenueStore.

The fake runs always (proves the contract the repository relies on). The real
SQLAlchemy store runs ONLY when RDS_TEST_URL points at a scratch Postgres whose
schema has been migrated (`alembic upgrade head`) — there is no local Postgres
in CI/dev. This is the post-provisioning validation step:

    RDS_TEST_URL=postgresql+psycopg://user:pass@host:5432/db \
        .venv/bin/python -m pytest tests/test_rds_store_contract.py -v

Run it against the scratch DB BEFORE the production backfill so the real store's
SQL (upserts, ON CONFLICT, jsonb cast, composite weekly key) is proven first.
"""
import os
import uuid

import pytest

from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo
from app.models.vibe_attributes import VibeAttributes
from tests.rds_fake import InMemoryRdsVenueStore

_VA = "google_places.vibe_attributes"
_WEEKLY = "besttime.weekly_forecast"


def _store_kinds():
    kinds = ["fake"]
    if os.environ.get("RDS_TEST_URL"):
        kinds.append("rds")
    return kinds


@pytest.fixture(params=_store_kinds())
def store(request):
    if request.param == "fake":
        return InMemoryRdsVenueStore()
    from app.dao.rds_venue_store import RdsVenueStore
    return RdsVenueStore(os.environ["RDS_TEST_URL"])


def _vid() -> str:
    return f"ct_{uuid.uuid4().hex[:12]}"


def _venue(vid, name="Bar X", **kw):
    return Venue(venue_id=vid, venue_name=name, venue_address="a",
                 venue_lat=-8.05, venue_lng=-34.88, venue_type="BAR", **kw)


def test_venue_upsert_and_soft_delete(store):
    vid = _vid()
    store.upsert_venue(_venue(vid, "Boteco"))
    row = store.get_venue(vid)
    assert row is not None and row["venue_name"] == "Boteco"
    assert row["lifecycle_status"] == "active"
    assert vid in store.list_active_venue_ids()

    store.soft_delete_venue(vid, "ineligible_google_type", "eligibility_filter")
    row = store.get_venue(vid)
    assert row["lifecycle_status"] == "deprecated"
    assert row["deprecated_reason"] == "ineligible_google_type"
    assert vid not in store.list_active_venue_ids()


def test_servable_view_excludes_deprecated_and_ineligible(store):
    # The serving view (serving.eligible_venue / fake mirror) = active AND eligible.
    # Both store kinds must agree. The real store reads the SQL view, which requires
    # the migration's seeded default rules; the fake derives defaults from empty rows.
    bar = _vid()
    store.upsert_venue(_venue(bar, "Boteco"))          # active + eligible
    assert bar in store.list_servable_venue_ids()

    store.soft_delete_venue(bar, "ineligible_google_type", "eligibility_filter")
    assert bar not in store.list_servable_venue_ids()  # deprecated -> not served

    church = _vid()                                     # active but blocked besttime type
    store.upsert_venue(Venue(
        venue_id=church, venue_name="Some Parish", venue_address="a",
        venue_lat=-8.05, venue_lng=-34.88, venue_type="CHURCH",
    ))
    assert church not in store.list_servable_venue_ids()


def test_active_readd_does_not_resurrect_deprecated(store):
    # Once serving reads RDS via the projector, an active re-add (catalog refresh
    # re-finding a deprecated venue) must NOT resurrect it. The guard lives in the
    # RDS write path; this asserts both stores honour it identically.
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.soft_delete_venue(vid, "ineligible_google_type", "eligibility_filter")
    assert store.get_venue(vid)["lifecycle_status"] == "deprecated"

    store.upsert_venue(_venue(vid, "Re-added Active"))  # incoming lifecycle=active
    row = store.get_venue(vid)
    assert row["lifecycle_status"] == "deprecated"          # stayed deprecated
    assert row["deprecated_reason"] == "ineligible_google_type"
    assert vid not in store.list_active_venue_ids()
    assert vid in store.list_deprecated_venue_ids()


def test_geo_link_undo_source_allows_reactivation(store):
    # The one resurrect-block exemption: a venue deprecated by an undo
    # (source="admin_geo_link_undo") IS reactivated by an active re-add, clearing
    # the deprecation fields — otherwise a re-add after an undo would be poisoned.
    # Both stores must honour it identically.
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.soft_delete_venue(vid, "geo_link_undone", "admin_geo_link_undo")
    assert store.get_venue(vid)["lifecycle_status"] == "deprecated"

    store.upsert_venue(_venue(vid, "Re-added Active"))  # incoming lifecycle=active
    row = store.get_venue(vid)
    assert row["lifecycle_status"] == "active"           # reactivated
    assert row["deprecated_reason"] is None
    assert row["deprecated_source"] is None
    assert vid in store.list_active_venue_ids()
    assert vid not in store.list_deprecated_venue_ids()


def test_enrichment_upsert_history_and_soft_delete(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))  # FK parent
    attrs = VibeAttributes(venue_id=vid, google_place_id="p", google_primary_type="bar")
    store.upsert_enrichment(_VA, vid, attrs.model_dump(mode="json"),
                            history=True, promoted={"google_primary_type": "bar",
                                                    "google_place_id": "p"})
    rec = store.get_enrichment(_VA, vid)
    assert rec is not None and rec["deleted_at"] is None
    assert rec["payload"]["google_primary_type"] == "bar"

    store.soft_delete_enrichment(_VA, vid, history=True)
    assert store.get_enrichment(_VA, vid)["deleted_at"] is not None


def test_weekly_composite_key(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_enrichment(_WEEKLY, f"{vid}#0", {"day_int": 0, "day_raw": [1] * 24},
                            history=False)
    rec = store.get_enrichment(_WEEKLY, f"{vid}#0")
    assert rec is not None and rec["payload"]["day_int"] == 0


def test_bulk_venue_reader_matches_single_reader(store):
    """get_venues_by_ids (P1) must return the same row shape as get_venue for
    every id in the set, and simply omit ids with no row (no KeyError, no
    placeholder). Empty input returns an empty dict without a query."""
    a, b = _vid(), _vid()
    store.upsert_venue(_venue(a, "Bar A"))
    store.upsert_venue(_venue(b, "Bar B"))
    missing = _vid()  # never upserted

    bulk = store.get_venues_by_ids([a, b, missing])
    assert set(bulk) == {a, b}
    assert bulk[a] == store.get_venue(a)
    assert bulk[b] == store.get_venue(b)
    assert store.get_venues_by_ids([]) == {}


def test_bulk_enrichment_reader_matches_single_reader_and_excludes_deleted(store):
    """get_enrichment_bulk (P1) must match get_enrichment's row shape
    (payload/deleted_at/updated_at) for present ids, and exclude soft-deleted
    rows entirely (not include them with deleted_at set) — the single-row
    reader's "rec.get('deleted_at') is None" gate has no bulk-side counterpart
    to check, so the bulk map itself must already be filtered."""
    a, b, deleted = _vid(), _vid(), _vid()
    for vid in (a, b, deleted):
        store.upsert_venue(_venue(vid))
    attrs = VibeAttributes(venue_id=a, google_place_id="p", google_primary_type="bar")
    store.upsert_enrichment(_VA, a, attrs.model_dump(mode="json"), history=False,
                            promoted={"google_primary_type": "bar", "google_place_id": "p"})
    store.upsert_enrichment(_VA, deleted, attrs.model_dump(mode="json"), history=False,
                            promoted={"google_primary_type": "bar", "google_place_id": "p"})
    store.soft_delete_enrichment(_VA, deleted, history=False)

    bulk = store.get_enrichment_bulk(_VA, [a, b, deleted])
    assert set(bulk) == {a}  # b has no row; deleted is excluded
    single = store.get_enrichment(_VA, a)
    assert bulk[a]["payload"] == single["payload"]
    assert bulk[a]["deleted_at"] is None
    assert store.get_enrichment_bulk(_VA, []) == {}


def test_bulk_weekly_reader_nests_by_day_and_excludes_deleted(store):
    """get_weekly_bulk (P1) must return {venue_id: {day_int: row}} for every
    non-deleted weekly row across the id set — the bulk counterpart of 7x
    get_enrichment(besttime.weekly_forecast, "id#day")."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_enrichment(_WEEKLY, f"{vid}#0", {"day_int": 0, "day_raw": [1] * 24}, history=False)
    store.upsert_enrichment(_WEEKLY, f"{vid}#3", {"day_int": 3, "day_raw": [2] * 24}, history=False)
    store.upsert_enrichment(_WEEKLY, f"{vid}#5", {"day_int": 5, "day_raw": [3] * 24}, history=False)
    store.soft_delete_enrichment(_WEEKLY, f"{vid}#5", history=False)

    bulk = store.get_weekly_bulk([vid])
    assert set(bulk[vid]) == {0, 3}  # day 5 excluded (soft-deleted)
    assert bulk[vid][0]["payload"] == store.get_enrichment(_WEEKLY, f"{vid}#0")["payload"]
    assert bulk[vid][3]["payload"]["day_raw"] == [2] * 24
    assert store.get_weekly_bulk([]) == {}


def test_bulk_live_reader_matches_single_reader(store):
    """get_live_bulk (P1) must match get_live_forecast's payload for present
    ids and omit ids with no live row."""
    a, b = _vid(), _vid()
    store.upsert_venue(_venue(a))
    store.upsert_venue(_venue(b))
    store.upsert_live_forecast(a, {"status": "OK"})

    bulk = store.get_live_bulk([a, b])
    assert set(bulk) == {a}
    assert bulk[a]["payload"] == store.get_live_forecast(a)["payload"]
    assert store.get_live_bulk([]) == {}


def test_fresh_enrichment_gating(store):
    # Executes the real list_fresh_enrichment_venue_ids SQL (incl. make_interval).
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_enrichment("venues.vibe_profile", vid,
                            {"venue_id": vid, "top_vibes": []}, history=False)
    assert vid in store.list_fresh_enrichment_venue_ids("venues.vibe_profile")  # presence
    assert vid in store.list_fresh_enrichment_venue_ids(
        "venues.vibe_profile", max_age_seconds=10 ** 9)                          # within window
    store.soft_delete_enrichment("venues.vibe_profile", vid, history=False)
    assert vid not in store.list_fresh_enrichment_venue_ids("venues.vibe_profile")  # excluded


def test_fresh_instagram_status_aware_gating(store):
    # Executes list_fresh_instagram_venue_ids (payload->>'status' + COALESCE).
    found, nf = _vid(), _vid()
    store.upsert_venue(_venue(found))
    store.upsert_venue(_venue(nf))
    store.upsert_enrichment("instagram.handle", found,
                            {"venue_id": found, "status": "found"}, history=False)
    store.upsert_enrichment("instagram.handle", nf,
                            {"venue_id": nf, "status": "not_found"}, history=False)
    # found window wide, not_found window zero -> only the found row is fresh
    fresh = set(store.list_fresh_instagram_venue_ids(
        found_max_age_seconds=10 ** 9, not_found_max_age_seconds=0))
    assert found in fresh and nf not in fresh


def test_delete_live_forecast_round_trip(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_live_forecast(vid, {"status": "OK"})
    assert store.get_live_forecast(vid) is not None
    store.delete_live_forecast(vid)
    assert store.get_live_forecast(vid) is None


def test_live_forecast_current_state(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    lf = LiveForecastResponse(status="OK", venue_info=VenueInfo(venue_id=vid),
                              analysis=Analysis(venue_live_busyness=7, venue_live_busyness_available=True))
    store.upsert_live_forecast(vid, lf.model_dump(mode="json"))
    assert store.get_live_forecast(vid) is not None


def test_favorite_and_hot_like_event(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_favorite("pseudo-abc", vid)
    store.soft_delete_favorite("pseudo-abc", vid)  # un-favorite -> soft delete
    store.add_hot_like_event("pseudo-abc", vid)    # append-only, no error


def test_app_session_idempotent_and_window_counts(store):
    from datetime import date, timedelta

    today = date.today()
    d7 = today - timedelta(days=6)    # 7d window start (inclusive of today)
    d30 = today - timedelta(days=29)  # 30d window start
    # Unique pseudonyms so deltas hold even against a non-empty scratch DB.
    u = uuid.uuid4().hex[:8]
    pa, pb, pc = f"ps_a_{u}", f"ps_b_{u}", f"ps_c_{u}"

    base_total = store.count_users()
    base_1d = store.count_users(today)
    base_7d = store.count_users(d7)
    base_30d = store.count_users(d30)

    store.record_app_session(pa, today)
    store.record_app_session(pa, today)  # idempotent: ON CONFLICT DO NOTHING
    store.record_app_session(pb, today)
    store.record_app_session(pc, today - timedelta(days=10))  # only in the 30d window

    assert store.count_users() - base_total == 3       # distinct users all-time
    assert store.count_users(today) - base_1d == 2      # active today
    assert store.count_users(d7) - base_7d == 2         # 10d-ago user excluded
    assert store.count_users(d30) - base_30d == 3       # 10d-ago user included


def test_admin_config_round_trip(store):
    # Test-only key so it never clobbers real config; cleaned up at the end.
    key = f"_contract_test_cfg_{uuid.uuid4().hex[:8]}"
    payload = {"a": 1, "nested": {"x": [1, 2]}, "flag": True}
    store.upsert_admin_config(key, payload, "contract-test")
    row = store.get_admin_config(key)
    assert row is not None and row["key"] == key
    assert row["value"] == payload  # jsonb round-trip (dict in / dict out)

    store.upsert_admin_config(key, {"a": 2}, "contract-test")  # ON CONFLICT upsert
    assert store.get_admin_config(key)["value"] == {"a": 2}

    assert key in {r["key"] for r in store.list_admin_config()}

    store.delete_admin_config(key)
    assert store.get_admin_config(key) is None


def test_geo_fence_round_trip(store):
    # Default fence is seeded (fake in __init__; real by migration 0015).
    fence = store.get_geo_fence()
    assert set(fence) == {"enabled", "cities"}
    assert isinstance(fence["cities"], list)

    # Replace the circle list whole (two cities, disabled) and read it back —
    # coordinates round-trip and circles come back sorted by name.
    from app.services.venue_eligibility import CAPITALS_BY_SLUG, default_geo_fence

    new_fence = {
        "enabled": False,
        "cities": [
            {**CAPITALS_BY_SLUG["salvador"], "radius_km": 25.0},
            {**CAPITALS_BY_SLUG["recife"], "radius_km": 30.0},
        ],
    }
    store.set_geo_fence(new_fence, updated_by="contract-test")
    got = store.get_geo_fence()
    assert got["enabled"] is False
    by_slug = {c["slug"]: c for c in got["cities"]}
    assert set(by_slug) == {"recife", "salvador"}
    assert by_slug["recife"]["radius_km"] == 30.0
    assert by_slug["salvador"]["radius_km"] == 25.0
    assert by_slug["recife"]["lat"] == CAPITALS_BY_SLUG["recife"]["lat"]

    # Replace again with a single circle: the transactional full-list replace
    # must drop salvador, not accumulate.
    store.set_geo_fence(default_geo_fence(), updated_by="contract-test")
    got = store.get_geo_fence()
    assert [c["slug"] for c in got["cities"]] == ["recife"]
    assert got["enabled"] is True  # also restores a shared scratch DB to sane

    # count_geo_excluded_active_venues() is a separate hand-written SQL predicate
    # (observability only); pin it to the circle semantics. Seed one in-circle and
    # one out-of-circle active venue and assert the count rises by exactly one.
    # Uses a delta (the shared scratch DB may hold other venues) so it is
    # order-independent.
    before = store.count_geo_excluded_active_venues()
    in_vid, out_vid = _vid(), _vid()
    store.upsert_venue(_venue(in_vid))  # _venue defaults to -8.05,-34.88 (in-circle)
    store.upsert_venue(Venue(
        venue_id=out_vid, venue_name="Bar Paulista", venue_address="a",
        venue_lat=-23.55, venue_lng=-46.63, venue_type="BAR",  # São Paulo, outside
    ))
    after = store.count_geo_excluded_active_venues()
    assert after - before == 1, (before, after)
    # Soft-deleting the out-of-circle venue drops it from the count (active-only).
    store.soft_delete_venue(out_vid, "test_cleanup", "contract-test")
    assert store.count_geo_excluded_active_venues() == before


def test_outside_circles_count_ignores_enabled_and_fails_open_on_empty(store):
    # count_active_venues_outside_circles() is the admin panel's warning number:
    # it must keep counting while the fence is DISABLED (that's exactly when the
    # operator needs to see what re-entered serving), and an empty circle list
    # counts zero on both counters (no circles = no restriction — the fail-open
    # the view, geo_excluded(), and both counts share). Deltas keep it
    # order-independent on a shared scratch DB.
    from app.services.venue_eligibility import default_geo_fence

    store.set_geo_fence(default_geo_fence(), updated_by="contract-test")
    before = store.count_active_venues_outside_circles()
    out_vid = _vid()
    store.upsert_venue(Venue(
        venue_id=out_vid, venue_name="Bar Paulista", venue_address="a",
        venue_lat=-23.55, venue_lng=-46.63, venue_type="BAR",  # São Paulo, outside
    ))
    assert store.count_active_venues_outside_circles() - before == 1

    # Disabled fence: the gauge count goes fail-open (0) but the warning number
    # still reports what sits outside the configured circles.
    disabled = default_geo_fence()
    disabled["enabled"] = False
    store.set_geo_fence(disabled, updated_by="contract-test")
    assert store.count_geo_excluded_active_venues() == 0
    assert store.count_active_venues_outside_circles() - before == 1

    # Empty circle list (reachable only by hand — the API requires ≥1 city when
    # enabled): both counts are zero, matching the view's fail-open geo term.
    store.set_geo_fence({"enabled": True, "cities": []}, updated_by="contract-test")
    assert store.count_geo_excluded_active_venues() == 0
    assert store.count_active_venues_outside_circles() == 0

    # Restore the seeded default so later tests see a sane shared scratch DB.
    store.soft_delete_venue(out_vid, "test_cleanup", "contract-test")
    store.set_geo_fence(default_geo_fence(), updated_by="contract-test")
