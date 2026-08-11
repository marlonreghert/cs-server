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
from datetime import datetime, timezone

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
    assert store.upsert_live_forecast(vid, {"status": "OK"}) is True
    assert store.get_live_forecast(vid) is not None
    store.delete_live_forecast(vid)
    assert store.get_live_forecast(vid) is None


def test_live_forecast_skipped_when_venue_absent_from_catalog(store):
    """live_forecast.venue_id FK-references venues.venue(venue_id). BestTime can
    echo a venue_id (used as the write key) that no longer matches our catalog —
    e.g. a stale/mismatched id in the live-forecast payload, or the row having
    been removed out-of-band. Either way this must be a benign no-op: no
    exception, no row written, and the caller can tell it was skipped (return
    False) rather than mistake it for a real write."""
    ghost_vid = _vid()  # never upserted via store.upsert_venue
    assert store.upsert_live_forecast(ghost_vid, {"status": "OK"}) is False
    assert store.get_live_forecast(ghost_vid) is None


def test_live_forecast_current_state(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    lf = LiveForecastResponse(status="OK", venue_info=VenueInfo(venue_id=vid),
                              analysis=Analysis(venue_live_busyness=7, venue_live_busyness_available=True))
    store.upsert_live_forecast(vid, lf.model_dump(mode="json"))
    assert store.get_live_forecast(vid) is not None


def test_live_forecast_reupsert_via_on_conflict_still_reports_written(store):
    """The steady-state refresh path: re-upserting an already-cached venue's
    live forecast hits ON CONFLICT DO UPDATE, not the bare INSERT. This must
    still be reported as written (True), not miscounted as the absent-venue
    skip — a regression here would silently reclassify nearly every routine
    refresh as 'skipped_venue_absent'."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    assert store.upsert_live_forecast(vid, {"status": "OK", "n": 1}) is True
    assert store.upsert_live_forecast(vid, {"status": "OK", "n": 2}) is True  # re-upsert
    assert store.get_live_forecast(vid)["payload"]["n"] == 2


def test_favorite_and_hot_like_event(store):
    from datetime import date

    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_favorite("pseudo-abc", vid)
    store.soft_delete_favorite("pseudo-abc", vid)  # un-favorite -> soft delete
    assert store.add_hot_like_event("pseudo-abc", vid, date.today()) is True  # first write


def test_block_venue_reports_no_favorite_removed_when_none_existed(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    assert store.block_venue("pseudo-blk1", vid) is False
    row = store.get_block("pseudo-blk1", vid)
    assert row is not None and row["deleted_at"] is None


def test_block_venue_atomically_clears_an_active_favorite(store):
    """The critical cross-repo contract: blocking a favorited venue clears the
    favorite in the SAME call, and reports that it did."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_favorite("pseudo-blk2", vid)

    assert store.block_venue("pseudo-blk2", vid) is True

    fav = store.get_favorite("pseudo-blk2", vid)
    assert fav is not None and fav["deleted_at"] is not None, "favorite must be soft-deleted"
    block = store.get_block("pseudo-blk2", vid)
    assert block is not None and block["deleted_at"] is None, "block must be active"


def test_block_venue_does_not_report_removal_for_an_already_removed_favorite(store):
    """An inactive (already unfavorited) favorite must not be reported as
    removed a second time -- the DAO's UPDATE ... WHERE deleted_at IS NULL
    guard, not just presence of a row."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_favorite("pseudo-blk3", vid)
    store.soft_delete_favorite("pseudo-blk3", vid)

    assert store.block_venue("pseudo-blk3", vid) is False


def test_block_venue_is_idempotent(store):
    """Blocking the same venue twice must not create a second row or flip any
    state -- ON CONFLICT DO UPDATE onto the same active row."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    assert store.block_venue("pseudo-blk4", vid) is False
    assert store.block_venue("pseudo-blk4", vid) is False
    row = store.get_block("pseudo-blk4", vid)
    assert row is not None and row["deleted_at"] is None


def test_unblock_never_restores_a_favorite(store):
    """Locked product decision: soft_delete_block only touches the block row,
    never engagement.favorite."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.upsert_favorite("pseudo-blk5", vid)
    store.block_venue("pseudo-blk5", vid)  # clears the favorite

    store.soft_delete_block("pseudo-blk5", vid)

    block = store.get_block("pseudo-blk5", vid)
    assert block is not None and block["deleted_at"] is not None
    fav = store.get_favorite("pseudo-blk5", vid)
    assert fav is not None and fav["deleted_at"] is not None, "unblock must not resurrect the favorite"


def test_unblock_a_never_blocked_venue_is_a_safe_no_op(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.soft_delete_block("pseudo-blk6", vid)  # no row exists yet
    assert store.get_block("pseudo-blk6", vid) is None


def test_purge_user_engagement_includes_blocked_venues_count(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    store.block_venue("pseudo-blk7", vid)

    removed = store.purge_user_engagement("pseudo-blk7")
    assert removed["blocked_venues"] == 1
    assert store.get_block("pseudo-blk7", vid) is None, "purge must hard-delete, not soft-delete"


@pytest.mark.skipif(
    not os.environ.get("RDS_TEST_URL"),
    reason="requires a real Postgres (RDS_TEST_URL) -- the in-memory fake has no transactions to roll back",
)
def test_block_venue_transaction_rolls_back_atomically_on_mid_transaction_failure():
    """Forces a failure AFTER the block-row INSERT but BEFORE the favorite
    UPDATE, inside block_venue's single `engine.begin()` block, and proves the
    whole transaction rolled back: the block row must not exist afterwards,
    and the favorite must remain exactly as it was. This is the one part of
    the "no window where a venue is both blocked and favorited" contract the
    in-memory fake (non-transactional) cannot prove."""
    from unittest.mock import patch

    from sqlalchemy.engine import Connection

    from app.dao.rds_venue_store import RdsVenueStore

    real_store = RdsVenueStore(os.environ["RDS_TEST_URL"])
    vid = _vid()
    real_store.upsert_venue(_venue(vid))
    real_store.upsert_favorite("pseudo-fk2", vid)

    original_execute = Connection.execute
    calls = {"n": 0}

    def _boom_on_second_call(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("forced failure mid-transaction")
        return original_execute(self, *args, **kwargs)

    with patch.object(Connection, "execute", _boom_on_second_call):
        with pytest.raises(RuntimeError):
            real_store.block_venue("pseudo-fk2", vid)

    assert real_store.get_block("pseudo-fk2", vid) is None, "the block INSERT must have rolled back"
    fav = real_store.get_favorite("pseudo-fk2", vid)
    assert fav is not None and fav["deleted_at"] is None, "the favorite must be untouched by the rollback"


def test_hot_like_event_idempotent_per_business_period(store):
    """A retried write (vibes_bot retries on a 5xx per the engagement_router
    contract) for the same (user, venue, Recife day) must not persist a second
    row -- unique index (migration 0016) + ON CONFLICT DO NOTHING. A genuinely
    different business_period (a different calendar day) is a new event."""
    from datetime import date, timedelta

    vid = _vid()
    store.upsert_venue(_venue(vid))
    today = date.today()
    yesterday = today - timedelta(days=1)

    # The boolean return is the shared contract (both fake and real store):
    # True on a genuinely new (user, venue, business_period) row, False when
    # an existing row already covers it (conflict-suppressed retry).
    assert store.add_hot_like_event("pseudo-xyz", vid, today) is True      # new row
    assert store.add_hot_like_event("pseudo-xyz", vid, today) is False     # retried: suppressed
    assert store.add_hot_like_event("pseudo-xyz", vid, yesterday) is True  # different day: new row
    assert store.add_hot_like_event("pseudo-xyz", vid, today) is False     # still suppressed


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


# ── events.event (plans/260804_instagram-event-extraction.md) ────────────────
def _event_fields(vid, shortcode, **overrides):
    from app.services.pipeline_run_registry import new_run_id

    fields = {
        "event_id": f"evt_{new_run_id()}",
        "venue_id": vid,
        "source_kind": "venue_post",
        "source_handle": "contract_handle",
        "source_shortcode": shortcode,
        "source_permalink": f"https://instagram.com/p/{shortcode}",
        "starts_at": datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc),
        "is_recurring": False,
        "title": "Festa",
        "lineup": ["DJ A", "DJ B"],
        "confidence": 0.9,
        "status": "pending_review",
        "raw_extraction": {"title": "Festa"},
    }
    fields.update(overrides)
    return fields


def test_event_insert_get_and_source_lookup(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    fields = _event_fields(vid, f"abc_{vid}")
    inserted = store.insert_event(fields)
    assert inserted["event_id"] == fields["event_id"]
    assert inserted["title"] == "Festa"

    by_id = store.get_event(fields["event_id"])
    assert by_id["source_shortcode"] == fields["source_shortcode"]

    by_source = store.get_event_by_source("contract_handle", fields["source_shortcode"])
    assert by_source["event_id"] == fields["event_id"]

    assert store.get_event_by_source("contract_handle", "no_such_shortcode") is None


def test_event_update_is_partial(store):
    vid = _vid()
    store.upsert_venue(_venue(vid))
    fields = _event_fields(vid, f"upd_{vid}")
    store.insert_event(fields)

    updated = store.update_event(
        fields["event_id"], {"status": "confirmed", "title": "Corrected"},
    )
    assert updated["status"] == "confirmed"
    assert updated["title"] == "Corrected"
    # A field NOT in the partial update survives untouched.
    assert updated["source_permalink"] == fields["source_permalink"]


def test_event_update_missing_id_returns_none(store):
    assert store.update_event("no-such-event-id", {"status": "confirmed"}) is None


def test_event_unique_source_constraint_rejects_duplicate(store):
    """UNIQUE (source_handle, source_shortcode, source_event_key) —
    replacing 0023's two-column constraint (migration 0025,
    plans/260806_multi-event-posts.md): a post can now hold several events,
    so idempotency is keyed by CONTENT, not just the post. A duplicate SAME
    key for the same post is still rejected, proven here on both the fake
    and the real store rather than only in application code. See
    test_event_different_source_event_key_is_not_a_duplicate below for the
    other half of the contract this migration changed on purpose."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    shortcode = f"dup_{vid}"
    store.insert_event(_event_fields(vid, shortcode, source_event_key="k1"))
    with pytest.raises(Exception):
        store.insert_event(
            _event_fields(vid, shortcode, event_id=f"evt_other_{vid}", source_event_key="k1")
        )


def test_event_different_source_event_key_is_not_a_duplicate(store):
    """The whole point of migration 0025: several events CAN share one post
    as long as their content-derived keys differ — a roundup post at three
    venues must persist three rows, not collide on the first insert."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    shortcode = f"multi_{vid}"
    store.insert_event(_event_fields(vid, shortcode, source_event_key="k1"))
    store.insert_event(
        _event_fields(vid, shortcode, event_id=f"evt_other_{vid}", source_event_key="k2")
    )
    rows = store.list_events_by_source("contract_handle", shortcode)
    assert len(rows) == 2
    assert {r["source_event_key"] for r in rows} == {"k1", "k2"}


def test_list_events_by_handle_finds_only_events_sharing_that_handle(store):
    """plans/260811_merge-unresolved-into-resolved-sibling.md's handle-
    identity merge needs this lookup to find a resolved sibling — proven on
    the real store's `EXISTS` correlated subquery, not just the fake's dict
    scan, since a JOIN condition is exactly the kind of thing that is easy
    to get subtly wrong (e.g. matching against the wrong alias)."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    handle = f"leh_{vid}"
    other_handle = f"leh_other_{vid}"
    a_id = f"evt_leh_a_{vid}"
    b_id = f"evt_leh_b_{vid}"
    c_id = f"evt_leh_c_{vid}"
    store.insert_event(_event_fields(
        vid, f"leh_a_{vid}", event_id=a_id, source_handle=handle, source_event_key="leh_a_key",
    ))
    store.insert_event(_event_fields(
        vid, f"leh_b_{vid}", event_id=b_id, source_handle=handle, source_event_key="leh_b_key",
    ))
    store.insert_event(_event_fields(
        vid, f"leh_c_{vid}", event_id=c_id, source_handle=other_handle, source_event_key="leh_c_key",
    ))

    rows = store.list_events_by_handle(handle)
    assert {r["event_id"] for r in rows} == {a_id, b_id}

    assert store.list_events_by_handle(f"leh_nonexistent_{vid}") == []


def test_list_events_by_handle_matches_any_source_after_reattachment(store):
    """A merged event can carry sources from DIFFERENT handles (a venue-
    identity merge does not require matching handles) — this lookup must
    find it via EITHER source's own `source_handle`, not just the ONE the
    primary-source LATERAL of `_EVENT_SELECT` would pick, or a handle-
    identity search would silently miss an event whose most-recently-seen
    post came from a different handle than the one being searched for."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    handle_a = f"leh_ra_{vid}"
    handle_b = f"leh_rb_{vid}"
    canonical_id = f"evt_leh_ra_{vid}"
    duplicate_id = f"evt_leh_rb_{vid}"
    store.insert_event(_event_fields(
        vid, f"leh_ra_{vid}", event_id=canonical_id, source_handle=handle_a,
        source_event_key="leh_ra_key",
    ))
    store.insert_event(_event_fields(
        vid, f"leh_rb_{vid}", event_id=duplicate_id, source_handle=handle_b,
        source_event_key="leh_rb_key",
    ))
    store.reattach_event_sources(duplicate_id, canonical_id)
    store.delete_event(duplicate_id)

    assert {r["event_id"] for r in store.list_events_by_handle(handle_a)} == {canonical_id}
    assert {r["event_id"] for r in store.list_events_by_handle(handle_b)} == {canonical_id}


def test_event_ticket_info_and_attractions_round_trip(store):
    """plans/260808_event-ticket-info-and-attractions.md: `attractions`
    binds through the SAME jsonb CAST(:col AS jsonb) + json.dumps() path
    `lineup` already proves here on the real store when RDS_TEST_URL is
    set — the fake alone has twice missed a real defect (a missing NOT NULL
    primary key, an FK write-order bug), so this fixture is parametrized
    across both rather than trusting the fake's shape."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    shortcode = f"tia_{vid}"
    attractions = [
        {"name": "DJ Ramon", "type": "dj", "stage": "Pista NY", "styles": ["Pop"]},
        {"name": "Eliza Mello", "type": "live", "stage": None, "styles": []},
    ]
    fields = _event_fields(
        vid, shortcode, ticket_info="\U0001f3ab TICKETS", attractions=attractions,
    )
    inserted = store.insert_event(fields)
    assert inserted["ticket_info"] == "\U0001f3ab TICKETS"
    assert inserted["attractions"] == attractions

    by_id = store.get_event(fields["event_id"])
    assert by_id["ticket_info"] == "\U0001f3ab TICKETS"
    assert by_id["attractions"] == attractions
    assert isinstance(by_id["attractions"], list)

    by_source = store.get_event_by_source("contract_handle", shortcode)
    assert by_source["attractions"] == attractions

    updated = store.update_event(fields["event_id"], {
        "ticket_info": "Ingressos na bilheteria",
        "attractions": attractions + [
            {"name": "DJ New", "type": "dj", "stage": None, "styles": []},
        ],
    })
    assert updated["ticket_info"] == "Ingressos na bilheteria"
    assert len(updated["attractions"]) == 3
    assert isinstance(updated["attractions"], list)


def test_event_ticket_info_and_attractions_default_to_null_and_empty(store):
    """No back-fill: an event inserted without either field must read back
    ticket_info=None and attractions as empty/None, never an invented
    value."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    fields = _event_fields(vid, f"tia_none_{vid}")
    inserted = store.insert_event(fields)
    assert inserted.get("ticket_info") is None
    assert inserted.get("attractions") in (None, [])


def test_event_post_type_and_category_round_trip(store):
    """plans/260811_post-items-and-categories.md (migration 0034): the real
    store's `_EVENT_COLUMNS` allow-list is exactly the trap this contract
    file exists to catch — a column the SQL insert/update list forgets is
    silently dropped, invisible on the fake (which stores whatever dict key
    it is handed). Both `post_type` and `category` must survive insert,
    get, get_by_source, and a partial update, on BOTH store kinds."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    shortcode = f"ptc_{vid}"
    fields = _event_fields(vid, shortcode, post_type="menu", category="samba / pagode")
    inserted = store.insert_event(fields)
    assert inserted["post_type"] == "menu"
    assert inserted["category"] == "samba / pagode"

    by_id = store.get_event(fields["event_id"])
    assert by_id["post_type"] == "menu"
    assert by_id["category"] == "samba / pagode"

    by_source = store.get_event_by_source("contract_handle", shortcode)
    assert by_source["post_type"] == "menu"
    assert by_source["category"] == "samba / pagode"

    updated = store.update_event(fields["event_id"], {
        "post_type": "event", "category": "rock",
    })
    assert updated["post_type"] == "event"
    assert updated["category"] == "rock"
    # A field NOT in the partial update survives untouched.
    assert updated["title"] == fields["title"]


def test_event_post_type_defaults_and_category_defaults_to_null(store):
    """An event inserted without `post_type` must read back "event" — the
    real column's NOT NULL DEFAULT, mirrored by the fake (see
    InMemoryRdsVenueStore.insert_event) so a caller that forgets it (every
    pre-existing test fixture that predates this plan) still gets the SAME
    historically-accurate value on both store kinds. `category` defaults to
    None — it is never invented."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    fields = _event_fields(vid, f"ptc_none_{vid}")
    inserted = store.insert_event(fields)
    assert inserted.get("post_type") == "event"
    assert inserted.get("category") is None


def test_event_time_known_round_trip(store):
    """plans/260811_expose-time-known.md (migration 0035): `time_known` is
    an ordinary real column, sourced from the SAME resolver output as
    `starts_at` — not read out of the `raw_extraction` blob at serve time.
    Must survive insert, get, get_by_source, and a partial update, on BOTH
    store kinds, exactly like post_type/category above."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    shortcode = f"tk_{vid}"
    fields = _event_fields(vid, shortcode, time_known=True)
    inserted = store.insert_event(fields)
    assert inserted["time_known"] is True

    by_id = store.get_event(fields["event_id"])
    assert by_id["time_known"] is True

    by_source = store.get_event_by_source("contract_handle", shortcode)
    assert by_source["time_known"] is True

    updated = store.update_event(fields["event_id"], {"time_known": False})
    assert updated["time_known"] is False
    # A field NOT in the partial update survives untouched.
    assert updated["title"] == fields["title"]


def test_event_time_known_defaults_to_false(store):
    """An event inserted without `time_known` must read back False — the
    real column's NOT NULL DEFAULT, mirrored by the fake (see
    InMemoryRdsVenueStore.insert_event) so a caller that omits it (every
    pre-existing test fixture, and every row written before migration 0035)
    still gets the SAME honest "unknown" on both store kinds. Never True:
    an item with no recorded flag is one whose time cannot be vouched for."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    fields = _event_fields(vid, f"tk_none_{vid}")
    inserted = store.insert_event(fields)
    assert inserted.get("time_known") is False


def test_list_events_filters_by_venue_and_status(store):
    vid_a, vid_b = _vid(), _vid()
    store.upsert_venue(_venue(vid_a))
    store.upsert_venue(_venue(vid_b))
    store.insert_event(_event_fields(vid_a, f"la_{vid_a}", status="confirmed"))
    store.insert_event(_event_fields(vid_a, f"lb_{vid_a}", status="pending_review"))
    store.insert_event(_event_fields(vid_b, f"lc_{vid_b}", status="confirmed"))

    only_a = {e["source_shortcode"] for e in store.list_events(venue_id=vid_a)}
    assert only_a == {f"la_{vid_a}", f"lb_{vid_a}"}

    confirmed = {
        e["source_shortcode"] for e in store.list_events(status="confirmed")
        if e["venue_id"] in (vid_a, vid_b)
    }
    assert confirmed == {f"la_{vid_a}", f"lc_{vid_b}"}


# ── review queue predicate widening (plans/260810_date-correctness-review-
# reasons-and-path-parity.md §D) ─────────────────────────────────────────────
# `list_events_awaiting_decision`'s unresolved-venue clause used to key on
# `source_kind = 'promoter_post'`, which is exactly what made the shared-
# handle crawl path's provenance bug (stamping a venue's own post
# `promoter_post`) the ONLY reason those events ever reached the queue.
# Widened to `venue_id IS NULL` — proven here against BOTH the fake and real
# Postgres (tests/test_review_queue_completeness.py already covers the full
# (source_kind, status, location_resolution) matrix against the fake alone;
# this is the "only place real SQL runs" case CLAUDE.md requires for any DAO
# predicate this plan touches).
def test_confirmed_event_with_no_venue_is_queued_regardless_of_source_kind(store):
    """The trap plans/260810_date-correctness-review-reasons-and-path-
    parity.md §D names explicitly: correcting `source_kind` to `venue_post`
    for a shared-handle post must NOT silently empty the queue. A confirmed
    event with venue_id=None — a shared-handle post that could not be
    attributed to either of its handle's venues, `/confirm`ed anyway — must
    still surface for a venue decision, whether it is labelled `venue_post`
    OR `promoter_post`."""
    for source_kind in ("venue_post", "promoter_post"):
        shortcode = f"unresolved_{source_kind}_{uuid.uuid4().hex[:8]}"
        fields = _event_fields(
            None, shortcode, source_kind=source_kind, status="confirmed", venue_id=None,
        )
        inserted = store.insert_event(fields)
        ids = {r["event_id"] for r in store.list_events_awaiting_decision()}
        assert inserted["event_id"] in ids, (
            f"a confirmed, venue-less {source_kind} event must still be queued"
        )


def test_confirmed_event_with_a_venue_is_not_queued(store):
    """The other direction of the same widening, so the predicate is proven
    both ways (plans/260810's own "enumerated assertions create a false
    green" lesson): a confirmed event that HAS a venue must not be swept
    into the queue just because it is confirmed."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    shortcode = f"resolved_{uuid.uuid4().hex[:8]}"
    inserted = store.insert_event(_event_fields(vid, shortcode, status="confirmed"))
    ids = {r["event_id"] for r in store.list_events_awaiting_decision()}
    assert inserted["event_id"] not in ids


def test_queue_predicate_ignores_post_type_in_both_directions(store):
    """plans/260811_post-items-and-categories.md: what needs a decision is a
    property of the row's STATE, never its type — a clean item of ANY type
    (post_type is not even accepted/confirmed, just pending) stays out, a
    flagged item of ANY type comes back. Proven for every post_type this
    plan names, both ways, so a regression that adds a type filter to the
    predicate (the bug the plan explicitly warns against) fails here."""
    vid = _vid()
    store.upsert_venue(_venue(vid))
    for post_type in ("event", "promotion", "menu", "food", "other", "giveaway"):
        clean_shortcode = f"queue_clean_{post_type}_{uuid.uuid4().hex[:8]}"
        clean = store.insert_event(_event_fields(
            vid, clean_shortcode, status="confirmed", post_type=post_type,
        ))
        flagged_shortcode = f"queue_flagged_{post_type}_{uuid.uuid4().hex[:8]}"
        flagged = store.insert_event(_event_fields(
            vid, flagged_shortcode, status="pending_review", post_type=post_type,
        ))
        ids = {r["event_id"] for r in store.list_events_awaiting_decision()}
        assert clean["event_id"] not in ids, (post_type, "clean item was queued")
        assert flagged["event_id"] in ids, (post_type, "flagged item was not queued")


# ── events.crawl_target write shape (production incident, 2026-08-09) ────────
# `upsert_crawl_target(handle, updates)` used to be the ONLY write for both
# create AND the post-run bookkeeping update. That bookkeeping call passes a
# partial field set (cursor/last_run/consecutive_failures — never kind/cron).
# The in-memory fake accepted it happily (a bare dict-update has no concept
# of NOT NULL), so nothing caught it until a real Postgres did, in
# production: `kind`/`cron` are NOT NULL with no default (migration
# 0030_crawl_target), and Postgres validates NOT NULL against the fully
# constructed INSERT tuple BEFORE it ever evaluates ON CONFLICT — so the
# write raised NotNullViolation even though the row certainly already
# existed, AFTER the scrape had already been billed. This is the third time
# this fake has been more permissive than the database (CLAUDE.md already
# names the other two), so — per that pattern — this is parametrized across
# both the fake and a real Postgres (RDS_TEST_URL) rather than trusted to the
# fake's shape alone.
def _crawl_handle() -> str:
    return f"ct_{uuid.uuid4().hex[:12]}"


def test_crawl_target_bookkeeping_write_succeeds_against_an_existing_row(store):
    """The fix: `update_crawl_target` is a plain UPDATE with no INSERT
    branch, so it cannot trip the NOT NULL-on-insert-attempt failure. This is
    the literal shape of the incident's write — cursor/last_run/consecutive_
    failures only, no kind/cron — proven to succeed against a REAL row when
    RDS_TEST_URL is set, not just the fake."""
    handle = _crawl_handle()
    store.upsert_crawl_target(handle, {"kind": "venue", "cron": "0 3 * * 2,5"})

    updated = store.update_crawl_target(handle, {
        "cursor_posts_at": datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
        "last_run_at": datetime(2026, 8, 9, 12, 5, tzinfo=timezone.utc),
        "last_run_results": 32,
        "last_run_cost_usd": 0.0864,
        "consecutive_failures": 0,
    })

    assert updated["kind"] == "venue"           # untouched NOT NULL column survives
    assert updated["cron"] == "0 3 * * 2,5"      # untouched NOT NULL column survives
    assert updated["last_run_results"] == 32
    assert updated["last_run_cost_usd"] == 0.0864

    reread = store.get_crawl_target(handle)
    assert reread["last_run_results"] == 32


def test_crawl_target_upsert_is_create_only_and_refuses_the_incidents_exact_shape(store):
    """Confirms the OLD call would fail before ever reaching this fix: calling
    `upsert_crawl_target` (now CREATE-only) with the incident's exact partial
    field set against a row that already exists must refuse loudly (raises
    here, in Python) rather than reach a real Postgres NotNullViolation."""
    handle = _crawl_handle()
    store.upsert_crawl_target(handle, {"kind": "venue", "cron": "0 3 * * 2,5"})

    with pytest.raises(Exception):
        store.upsert_crawl_target(handle, {
            "cursor_posts_at": datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
            "last_run_at": datetime(2026, 8, 9, 12, 5, tzinfo=timezone.utc),
            "last_run_results": 32,
            "last_run_cost_usd": 0.0864,
            "consecutive_failures": 0,
        })


def test_crawl_target_update_on_a_missing_handle_is_a_safe_no_op(store):
    assert store.update_crawl_target(_crawl_handle(), {"enabled": False}) is None


def test_crawl_target_update_with_no_fields_returns_the_row_unchanged(store):
    handle = _crawl_handle()
    store.upsert_crawl_target(handle, {"kind": "venue", "cron": "0 22 * * *"})
    before = store.get_crawl_target(handle)

    after = store.update_crawl_target(handle, {})

    assert after["kind"] == before["kind"]
    assert after["cron"] == before["cron"]


def test_crawl_target_reels_overlap_counts_round_trip(store):
    """migration 0033_crawl_target_reels_overlap: a fresh target reads both
    counts as NULL ("not yet measured"), and a bookkeeping write that sets
    them (mirroring `run_target`'s own partial update — no `kind`/`cron`)
    persists and round-trips through both the fake and a real Postgres."""
    handle = _crawl_handle()
    store.upsert_crawl_target(handle, {"kind": "venue", "cron": "0 22 * * 5,6"})
    fresh = store.get_crawl_target(handle)
    assert fresh.get("last_run_reels_fetched") is None
    assert fresh.get("last_run_reels_new") is None

    updated = store.update_crawl_target(handle, {
        "last_run_reels_fetched": 16, "last_run_reels_new": 3,
    })
    assert updated["last_run_reels_fetched"] == 16
    assert updated["last_run_reels_new"] == 3

    reread = store.get_crawl_target(handle)
    assert reread["last_run_reels_fetched"] == 16
    assert reread["last_run_reels_new"] == 3
